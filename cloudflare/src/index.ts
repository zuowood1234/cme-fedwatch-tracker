import { launch, type BrowserWorker } from "@cloudflare/playwright";

const CME_URL = "https://www.cmegroup.cn/fed-watch/";
const QUIKSTRIKE_URL = "https://cmegroup-tools.quikstrike.net/User/QuikStrikeTools.aspx?viewitemid=IntegratedFedWatchTool&userId=lwolf";
const DASHBOARD_URL = "https://zuowood1234-cme-fedwatch-tracker.streamlit.app";
const MAX_ATTEMPTS = 3;

interface Env {
  BROWSER: BrowserWorker;
  SERVERCHAN_SENDKEY: string;
  GITHUB_TOKEN: string;
  GITHUB_REPOSITORY?: string;
  MANUAL_TRIGGER_TOKEN?: string;
}

interface RateProbability {
  range: string;
  now: number;
  day1: number;
  week1: number;
  month1: number;
}

interface Meeting {
  tabLabel: string;
  meetingDate: string;
  contract: string;
  midPrice: string;
  currentTarget: string;
  timestamp: string;
  rates: RateProbability[];
}

interface DomMeeting {
  meetingDate: string;
  contract: string;
  midPrice: string;
  currentTarget: string;
  timestamp: string;
  rates: RateProbability[];
}

const CSV_HEADER = [
  "snapshot_date", "meeting_date", "rate_range", "prob_now", "prob_1d",
  "prob_1w", "prob_1m", "is_most_likely", "contract", "mid_price",
  "current_target_rate",
];

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseMeetingDate(value: string): string {
  const cn = value.match(/(\d{1,2})\s*(\d{1,2})月\s*(\d{4})/);
  if (cn) {
    return `${cn[3]}-${cn[2].padStart(2, "0")}-${cn[1].padStart(2, "0")}`;
  }
  const en = value.match(/(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})/);
  if (!en) return value.trim();
  const months: Record<string, string> = {
    jan: "01", feb: "02", mar: "03", apr: "04", may: "05", jun: "06",
    jul: "07", aug: "08", sep: "09", oct: "10", nov: "11", dec: "12",
  };
  const month = months[en[2].toLowerCase()];
  return month ? `${en[3]}-${month}-${en[1].padStart(2, "0")}` : value.trim();
}

async function scrape(env: Env): Promise<Meeting[]> {
  const browser = await launch(env.BROWSER);
  try {
    const context = await browser.newContext({
      viewport: { width: 1920, height: 1080 },
      userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    });
    const page = await context.newPage();
    // Navigate to the same official QuikStrike component embedded by CME China.
    // Loading it directly avoids waiting for a cross-origin nested iframe.
    await page.goto(QUIKSTRIKE_URL, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await page.getByText("EASE", { exact: true }).waitFor({ state: "visible", timeout: 75_000 });

    const tabLinks = await page.locator('a[id*="lbMeeting"]').evaluateAll((links) =>
      links.map((link) => ({
        id: (link as HTMLElement).id,
        text: (link.textContent || "").trim(),
      })),
    );
    if (tabLinks.length === 0) throw new Error("No FOMC meeting tabs found");

    const meetings: Meeting[] = [];
    let previousDate = "";
    for (const tab of tabLinks) {
      await page.locator(`#${CSS.escape(tab.id)}`).click({ timeout: 20_000 });
      await page.locator("body").evaluate(
        async (body, prev) => {
          const deadline = Date.now() + 15_000;
          while (Date.now() < deadline) {
            const loading = body.querySelector('.throbber, [class*="loading"]') as HTMLElement | null;
            const tables = Array.from(body.querySelectorAll("table.grid-thm"));
            const info = tables.find((table) => (table.textContent || "").includes("Meeting Date"));
            const current = info?.querySelector("td")?.textContent?.trim() || "";
            if ((!loading || loading.offsetParent === null) && current && (!prev || current !== prev)) return;
            await new Promise((resolve) => setTimeout(resolve, 250));
          }
        },
        previousDate,
      );

      const data = await page.locator("body").evaluate<DomMeeting>((body) => {
        const parsePct = (text: string | null | undefined): number => {
          const match = (text || "").replace(/[%<>≈\u200b]/g, "").match(/[\d.]+/);
          return match ? Number.parseFloat(match[0]) : 0;
        };
        const tables = Array.from(body.querySelectorAll("table.grid-thm"));
        const findTable = (keyword: string) =>
          tables.find((table) => (table.textContent || "").includes(keyword));

        const infoCells = Array.from(findTable("Meeting Date")?.querySelectorAll("td") || []);
        const rateRows = Array.from(findTable("Target Rate (bps)")?.querySelectorAll("tr") || []);
        const rates: RateProbability[] = [];
        for (const row of rateRows) {
          if (row.classList.contains("hide")) continue;
          const cells = Array.from(row.querySelectorAll("td"));
          const range = cells[0]?.textContent?.trim() || "";
          if (!/^\d+-\d+/.test(range)) continue;
          rates.push({
            range,
            now: parsePct(cells[1]?.textContent),
            day1: parsePct(cells[2]?.textContent),
            week1: parsePct(cells[3]?.textContent),
            month1: parsePct(cells[4]?.textContent),
          });
        }

        const bodyText = (body as HTMLElement).innerText;
        return {
          meetingDate: infoCells[0]?.textContent?.trim() || "",
          contract: infoCells[1]?.textContent?.trim() || "",
          midPrice: infoCells[3]?.textContent?.trim() || "",
          currentTarget: bodyText.match(/Current target rate is (\d+-\d+)/i)?.[1] || "",
          timestamp: bodyText.match(/Data as of (.+?)\s*CT/i)?.[1]?.trim() || "",
          rates,
        };
      });

      if (data.rates.length === 0) throw new Error(`No probabilities for ${tab.text}`);
      previousDate = data.meetingDate;
      meetings.push({
        tabLabel: tab.text,
        meetingDate: parseMeetingDate(data.meetingDate),
        contract: data.contract,
        midPrice: data.midPrice,
        currentTarget: data.currentTarget,
        timestamp: data.timestamp,
        rates: data.rates,
      });
    }

    if (meetings.length < 2) throw new Error(`Only ${meetings.length} meeting extracted`);
    return meetings;
  } finally {
    await browser.close();
  }
}

function mostLikely(rates: RateProbability[], field: "now" | "day1"): RateProbability | undefined {
  return rates.filter((rate) => rate[field] > 0).sort((a, b) => b[field] - a[field])[0];
}

function formatDate(date: Date): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date).replaceAll("/", "-");
}

function csvCell(value: string | number): string {
  const text = String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function buildCsvRows(meetings: Meeting[], snapshotDate: string): string[] {
  const rows: string[] = [];
  for (const meeting of meetings) {
    const highest = Math.max(...meeting.rates.map((rate) => rate.now), 0);
    for (const rate of meeting.rates) {
      rows.push([
        snapshotDate, meeting.meetingDate, rate.range, rate.now, rate.day1,
        rate.week1, rate.month1, rate.now > 0 && rate.now >= highest ? 1 : 0,
        meeting.contract, meeting.midPrice, meeting.currentTarget,
      ].map(csvCell).join(","));
    }
  }
  return rows;
}

function toBase64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function fromBase64(value: string): string {
  const binary = atob(value.replace(/\s/g, ""));
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

interface GitHubContent {
  sha: string;
  content: string;
}

async function githubRequest(env: Env, path: string, init: RequestInit = {}): Promise<Response> {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN is not configured");
  const repository = env.GITHUB_REPOSITORY || "zuowood1234/cme-fedwatch-tracker";
  return fetch(`https://api.github.com/repos/${repository}/contents/${path}`, {
    ...init,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "user-agent": "cme-fedwatch-cloudflare-worker",
      "x-github-api-version": "2022-11-28",
      ...init.headers,
    },
  });
}

async function dispatchGitHubWorkflow(env: Env): Promise<void> {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN is not configured");
  const repository = env.GITHUB_REPOSITORY || "zuowood1234/cme-fedwatch-tracker";
  const response = await fetch(`https://api.github.com/repos/${repository}/dispatches`, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "content-type": "application/json",
      "user-agent": "cme-fedwatch-cloudflare-worker",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({
      event_type: "cloudflare-fedwatch-update",
      client_payload: { triggered_at: new Date().toISOString() },
    }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`GitHub dispatch failed: HTTP ${response.status} ${detail.slice(0, 300)}`);
  }
}

async function getGitHubContent(env: Env, path: string): Promise<GitHubContent | null> {
  const response = await githubRequest(env, `${path}?ref=main`);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`GitHub read ${path} failed: HTTP ${response.status}`);
  return response.json<GitHubContent>();
}

async function putGitHubContent(
  env: Env, path: string, content: string, message: string, sha?: string,
): Promise<void> {
  const response = await githubRequest(env, path, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message, content: toBase64(content), branch: "main", ...(sha ? { sha } : {}) }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`GitHub write ${path} failed: HTTP ${response.status} ${detail.slice(0, 300)}`);
  }
}

async function persistToGitHub(env: Env, meetings: Meeting[], now: Date): Promise<void> {
  const snapshotDate = formatDate(now);
  const scrapeTime = now.toISOString();
  const snapshot = {
    snapshot_date: snapshotDate,
    scrape_time: scrapeTime,
    source_url: CME_URL,
    num_meetings: meetings.length,
    meetings: meetings.map((meeting) => ({
      tab_label: meeting.tabLabel,
      meeting_date: meeting.meetingDate,
      contract: meeting.contract,
      mid_price: meeting.midPrice,
      current_target: meeting.currentTarget,
      timestamp: meeting.timestamp,
      summary: {},
      rate_probabilities: meeting.rates.map((rate) => ({
        range: rate.range, now: rate.now, day1: rate.day1,
        week1: rate.week1, month1: rate.month1,
      })),
    })),
  };

  const dailyPath = `data/daily/${snapshotDate}.json`;
  const existingDaily = await getGitHubContent(env, dailyPath);
  await putGitHubContent(
    env, dailyPath, `${JSON.stringify(snapshot, null, 2)}\n`,
    `chore: update FedWatch snapshot ${snapshotDate}`, existingDaily?.sha,
  );

  const csvPath = "data/fedwatch_history.csv";
  const existingCsv = await getGitHubContent(env, csvPath);
  const oldText = existingCsv ? fromBase64(existingCsv.content) : `${CSV_HEADER.join(",")}\n`;
  const oldLines = oldText.replace(/\r\n/g, "\n").split("\n").filter(Boolean);
  const header = oldLines[0] || CSV_HEADER.join(",");
  // Replace today's rows so manual retries remain idempotent.
  const retained = oldLines.slice(1).filter((line) => !line.startsWith(`${snapshotDate},`));
  const updatedCsv = [header, ...retained, ...buildCsvRows(meetings, snapshotDate)].join("\n") + "\n";
  await putGitHubContent(
    env, csvPath, updatedCsv, `chore: update FedWatch history ${snapshotDate}`, existingCsv?.sha,
  );
}

function buildMessage(meetings: Meeting[], now: Date): { title: string; desp: string } {
  const date = formatDate(now);
  const lines = [
    `**抓取时间**：${new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", hour12: false, year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(now)}`,
    `**CME数据时间**：${meetings[0]?.timestamp || "未提供"} CT`,
    "",
  ];

  const pathChanges: string[] = [];
  for (const meeting of meetings) {
    const current = mostLikely(meeting.rates, "now");
    const prior = mostLikely(meeting.rates, "day1");
    if (current && prior && current.range !== prior.range) {
      pathChanges.push(`- **${meeting.meetingDate}**：\`${prior.range}\` → \`${current.range}\`（${current.now.toFixed(1)}%）`);
    }
  }
  if (pathChanges.length) {
    lines.push("#### 🔴 利率预期路径变化（vs 1 Day Ago）", ...pathChanges, "");
  } else {
    lines.push("#### ✅ 利率预期路径稳定", "最可能利率区间较1日前没有变化。", "");
  }

  const alerts: string[] = [];
  for (const meeting of meetings) {
    for (const rate of meeting.rates) {
      const d1 = rate.day1 > 0 ? rate.now - rate.day1 : null;
      const w1 = rate.week1 > 0 ? rate.now - rate.week1 : null;
      if ((d1 !== null && Math.abs(d1) >= 5) || (w1 !== null && Math.abs(w1) >= 5)) {
        const changes = [
          d1 !== null && Math.abs(d1) >= 5 ? `1日 ${d1 >= 0 ? "+" : ""}${d1.toFixed(1)}%` : "",
          w1 !== null && Math.abs(w1) >= 5 ? `1周 ${w1 >= 0 ? "+" : ""}${w1.toFixed(1)}%` : "",
        ].filter(Boolean).join("；");
        alerts.push(`- **${meeting.meetingDate}** ${rate.range}：${rate.now.toFixed(1)}%（${changes}）`);
      }
    }
  }
  if (alerts.length) lines.push("#### 概率显著变化", ...alerts, "");
  else lines.push("#### ✅ 概率变动平稳", "各区间相较1日前及1周前的变化均小于5%。", "");

  lines.push(`[查看完整仪表盘](${DASHBOARD_URL})`);
  return { title: `CME FedWatch 日报 - ${date}`, desp: lines.join("\n") };
}

async function sendServerChan(env: Env, title: string, desp: string): Promise<void> {
  if (!env.SERVERCHAN_SENDKEY) throw new Error("SERVERCHAN_SENDKEY is not configured");
  const body = new URLSearchParams({ title, desp });
  const response = await fetch(`https://sctapi.ftqq.com/${env.SERVERCHAN_SENDKEY}.send`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded;charset=UTF-8" },
    body,
  });
  const result = await response.json() as { code?: number; message?: string };
  if (!response.ok || result.code !== 0) {
    throw new Error(`ServerChan failed: HTTP ${response.status}, ${JSON.stringify(result)}`);
  }
}

async function run(
  env: Env, push: boolean, persist: boolean, maxAttempts = MAX_ATTEMPTS,
): Promise<Record<string, unknown>> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const meetings = await scrape(env);
      const now = new Date();
      const message = buildMessage(meetings, now);
      if (persist) await persistToGitHub(env, meetings, now);
      if (push) await sendServerChan(env, message.title, message.desp);
      return { ok: true, persisted: persist, pushed: push, attempt, meetings: meetings.length, message };
    } catch (error) {
      lastError = error;
      console.error(`Attempt ${attempt}/${maxAttempts} failed`, error);
      if (attempt < maxAttempts) await sleep(15_000);
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

export default {
  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(dispatchGitHubWorkflow(env).then(() => console.log("GitHub workflow dispatched")));
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") return Response.json({ ok: true });

    const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
    if (!env.MANUAL_TRIGGER_TOKEN || token !== env.MANUAL_TRIGGER_TOKEN) {
      return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
    }

    try {
      if (url.searchParams.get("dispatch") === "1") {
        await dispatchGitHubWorkflow(env);
        return Response.json({ ok: true, dispatched: true });
      }
      const shouldPush = url.searchParams.get("push") === "1";
      const shouldPersist = url.searchParams.get("persist") === "1";
      return Response.json(await run(env, shouldPush, shouldPersist, 1));
    } catch (error) {
      return Response.json({ ok: false, error: error instanceof Error ? error.message : String(error) }, { status: 500 });
    }
  },
} satisfies ExportedHandler<Env>;
