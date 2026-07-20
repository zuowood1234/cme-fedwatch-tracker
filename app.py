"""
CME FedWatch Tracker — Streamlit Dashboard

Runs on Streamlit Community Cloud:
  1. On load: load latest data from GitHub (or local fallback)
  2. If today's data already exists, skip scraping
  3. Manual [Refresh Data] button for on-demand updates
  4. Scrape via Playwright + xvfb (virtual display) when needed
  5. Push new data to GitHub repo for persistence
  6. Display 4-panel dashboard

For daily automated scraping, use the separate scheduled automation
(e.g. WorkBuddy automation or GitHub Actions) instead of relying on
Streamlit Cloud page views.

Secrets required (set in Streamlit Cloud > Settings):
  - GITHUB_TOKEN: GitHub PAT with repo scope
  - GITHUB_USERNAME: your GitHub username
"""

import base64
import concurrent.futures
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Config ──────────────────────────────────────────────────────────────────
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "zuowood1234")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_NAME = "cme-fedwatch-tracker"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_USERNAME}/{REPO_NAME}/main"
CSV_URL = f"{GITHUB_RAW_BASE}/data/fedwatch_history.csv"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
HISTORY_CSV = os.path.join(DATA_DIR, "fedwatch_history.csv")


# ── Page setup ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CME FedWatch Tracker",
    page_icon="📊",
    layout="wide",
)

st.title("📊 CME FedWatch Tracker")
st.caption(
    "Official CME FedWatch probabilities for every upcoming FOMC meeting. "
    "Data scraped directly from CME. Auto-refreshed."
)


# ── GitHub Sync ──────────────────────────────────────────────────────────────

def github_push_data(data_dir, extra_files=None):
    """
    Push local data files to GitHub repo using Contents API.
    Uses GITHUB_TOKEN from secrets.

    extra_files: optional list of (git_path, local_path) tuples to push additionally.
    """
    import base64
    import requests

    token = GITHUB_TOKEN
    if not token:
        st.warning("⚠️ GITHUB_TOKEN not set. Data won't be persisted to GitHub.")
        return False

    api_base = f"https://api.github.com/repos/{GITHUB_USERNAME}/{REPO_NAME}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Files to push: fedwatch_history.csv + latest daily JSON
    files_to_push = []
    history_csv_path = os.path.join(data_dir, "fedwatch_history.csv")
    if os.path.exists(history_csv_path):
        files_to_push.append(("data/fedwatch_history.csv", history_csv_path))

    # Push today's daily snapshot
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_json_path = os.path.join(data_dir, "daily", f"{today_str}.json")
    if os.path.exists(daily_json_path):
        files_to_push.append((f"data/daily/{today_str}.json", daily_json_path))

    if extra_files:
        files_to_push.extend(extra_files)

    if not files_to_push:
        return True

    pushed = 0
    for git_path, local_path in files_to_push:
        try:
            with open(local_path, 'rb') as f:
                content_b64 = base64.b64encode(f.read()).decode()

            # Get current file SHA (for update)
            sha = None
            resp = requests.get(
                f"{api_base}/contents/{git_path}",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                sha = resp.json().get("sha")

            commit_msg = f"Auto-update {git_path} ({today_str})"
            payload = {
                "message": commit_msg,
                "content": content_b64,
            }
            if sha:
                payload["sha"] = sha

            resp = requests.put(
                f"{api_base}/contents/{git_path}",
                headers=headers,
                json=payload,
                timeout=30,
            )

            if resp.status_code in (200, 201):
                pushed += 1
            else:
                st.warning(f"GitHub API error for {git_path}: {resp.status_code} {resp.text[:100]}")

        except Exception as e:
            st.warning(f"Failed to push {git_path}: {e}")

    return pushed == len(files_to_push)


def _normalize_csv(df):
    """Normalize legacy column names and dtypes."""
    if "date" in df.columns and "snapshot_date" not in df.columns:
        df = df.rename(columns={"date": "snapshot_date"})
    if "probability" in df.columns and "prob_now" not in df.columns:
        df = df.rename(columns={"probability": "prob_now"})
    if "current_target" in df.columns and "current_target_rate" not in df.columns:
        df = df.rename(columns={"current_target": "current_target_rate"})
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce").dt.date
    df["meeting_date"] = pd.to_datetime(df["meeting_date"], errors="coerce").dt.date
    df["prob_now"] = pd.to_numeric(df["prob_now"], errors="coerce")
    # Drop rows where dates couldn't be parsed
    df = df.dropna(subset=["snapshot_date", "meeting_date"])
    return df


def load_csv_from_github(url):
    """
    Load CSV from GitHub raw URL with retries and GitHub API fallback.
    Returns (df, error_message).
    """
    import requests

    last_error = None

    # Try raw URL a few times using requests + StringIO
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            df = pd.read_csv(pd.io.common.StringIO(resp.text))
            if df.empty or "meeting_date" not in df.columns:
                last_error = "GitHub raw CSV is empty or missing expected columns"
                time.sleep(0.5 * (attempt + 1))
                continue
            return _normalize_csv(df), None
        except Exception as e:
            last_error = f"Raw URL attempt {attempt + 1} failed: {e}"
            time.sleep(0.5 * (attempt + 1))

    # Fallback: GitHub Contents API
    if GITHUB_TOKEN:
        try:
            api_path = url.replace(GITHUB_RAW_BASE, "").lstrip("/")
            api_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{REPO_NAME}/contents/{api_path}"
            headers = {
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            }
            resp = requests.get(api_url, headers=headers, timeout=20)
            if resp.status_code == 200:
                content_b64 = resp.json().get("content", "")
                decoded = base64.b64decode(content_b64).decode("utf-8")
                df = pd.read_csv(pd.io.common.StringIO(decoded))
                if not df.empty and "meeting_date" in df.columns:
                    return _normalize_csv(df), None
            else:
                last_error += f" | API fallback status {resp.status_code}"
        except Exception as e:
            last_error += f" | API fallback error: {e}"

    return pd.DataFrame(), last_error or "Failed to load CSV from GitHub"


def load_local_csv():
    """Load CSV from local filesystem with column normalization."""
    csv_path = HISTORY_CSV
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            return _normalize_csv(df)
        except Exception as e:
            st.warning(f"Failed to load local CSV: {e}")
    return pd.DataFrame()


# ── Scraper integration ──────────────────────────────────────────────────────

@st.cache_resource
def _install_playwright():
    """Install Playwright browsers (runs once per app start)."""
    import subprocess
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, timeout=180,
        )
        subprocess.run(
            [sys.executable, "-m", "playwright", "install-deps", "chromium"],
            capture_output=True, timeout=300,
        )
        return True
    except Exception as e:
        print(f"Playwright install error: {e}")
        return False


def run_scraper(log_lines=None):
    """
    Run CME FedWatch scraper.
    Returns (success: bool, message: str, meetings_count: int)

    log_lines: optional list to append log messages to (thread-safe if caller manages it).
    """
    if log_lines is None:
        log_lines = []

    def log_to_ui(msg):
        log_lines.append(msg)

    # Ensure Playwright is installed
    log_to_ui("📦 Checking Playwright browsers...")
    _install_playwright()

    # Import scraper module
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        from scraper import scrape_all_meetings, save_results
    except ImportError as e:
        return False, f"Import error: {e}", 0

    # Set up virtual display if needed (for headed mode on headless server)
    use_xvfb = True
    display = None

    if use_xvfb:
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=0, size=(1920, 1080))
            display.start()
            log_to_ui("🖥️  Virtual display (xvfb) started")
        except ImportError:
            use_xvfb = False
        except Exception as e:
            log_to_ui(f"⚠️ xvfb setup issue: {e}, trying without...")
            use_xvfb = False

    try:
        log_to_ui("🌐 Opening CME FedWatch page...")
        meetings = scrape_all_meetings(headless=False, max_wait=90, log_func=log_to_ui)

        if display:
            try:
                display.stop()
            except Exception:
                pass

        # Handle diagnostic dict returned on failure
        if isinstance(meetings, dict) and meetings.get('error'):
            diag_dir = Path(DATA_DIR) / "debug"
            diag_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            extra_files = []

            screenshot_b64 = meetings.get('screenshot')
            if screenshot_b64:
                png_path = diag_dir / f"fail_{ts}.png"
                png_path.write_bytes(base64.b64decode(screenshot_b64))
                extra_files.append((f"data/debug/{png_path.name}", str(png_path)))
                log_to_ui(f"📸 Screenshot saved: {png_path.name}")

            main_html = meetings.get('main_html', '')
            if main_html:
                html_path = diag_dir / f"fail_{ts}_main.html"
                html_path.write_text(main_html, encoding='utf-8')
                extra_files.append((f"data/debug/{html_path.name}", str(html_path)))
                log_to_ui(f"📝 HTML saved: {html_path.name}")

            # Push diagnostics to GitHub so we can inspect them
            log_to_ui("☁️ Pushing diagnostics to GitHub...")
            try:
                github_push_data(DATA_DIR, extra_files=extra_files)
            except Exception as e:
                log_to_ui(f"⚠️ Diagnostic push failed: {e}")

            iframe_summary = []
            for fi in meetings.get('iframes', []):
                iframe_summary.append(
                    f"  iframe[{fi.get('index')}] {fi.get('url','')}: text={fi.get('text_len','?')} chars"
                )

            diag_msg = "\n".join([
                f"❌ {meetings['error']}",
                f"URL: {meetings.get('url', '')}",
                "Iframes:",
            ] + iframe_summary)
            return False, diag_msg + "\n\n" + "\n".join(log_lines[-10:]), 0

        if not meetings:
            return False, "No meetings extracted. QuikStrike may have failed to render.\n\n" + "\n".join(log_lines[-15:]), 0

        # Save locally
        log_to_ui("💾 Saving results locally...")
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(DAILY_DIR, exist_ok=True)
        snap_file, rows = save_results(meetings, DATA_DIR)

        # Push to GitHub
        log_to_ui("☁️ Pushing to GitHub...")
        ok = github_push_data(DATA_DIR)

        msg = (
            f"Scraped **{len(meetings)}** meetings, **{rows}** rows.\n\n"
            + "\n".join(log_lines[-10:])
            + (f"\n\n✅ Pushed to GitHub" if ok else "\n⚠️ GitHub push failed")
        )
        return True, msg, len(meetings)

    except Exception as e:
        if display:
            try:
                display.stop()
            except Exception:
                pass
        return False, f"Scraper error: {e}\n\n" + "\n".join(log_lines[-15:]), 0


def run_scraper_with_timeout(timeout=240, log_container=None):
    """Run scraper in a worker thread with a hard timeout. UI updates happen only from the main thread."""
    log_lines = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_scraper, log_lines=log_lines)
        start = time.time()

        while True:
            # Update UI from the main thread only
            if log_container and log_lines:
                log_container.code("\n".join(log_lines[-8:]), language="text")

            try:
                return future.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                if time.time() - start > timeout:
                    return False, f"⏱️ Scraper timed out after {timeout}s.\n\nLatest logs:\n" + "\n".join(log_lines[-10:]), 0
                # Continue looping and updating UI


# ── Load data (GitHub first for cloud, local as fallback) ───────────────────

# Load both sources and keep the newer one
df_github, github_error = load_csv_from_github(CSV_URL)
df_local = load_local_csv()

load_source = None
if not df_github.empty and not df_local.empty:
    github_max = df_github["snapshot_date"].max()
    local_max = df_local["snapshot_date"].max()
    df = df_github if github_max >= local_max else df_local
    load_source = "GitHub" if github_max >= local_max else "local"
elif not df_github.empty:
    df = df_github
    load_source = "GitHub"
elif not df_local.empty:
    df = df_local
    load_source = "local"
else:
    df = pd.DataFrame()
    load_source = None

# Determine freshness
today = datetime.now(timezone.utc).date()
has_today_data = not df.empty and df["snapshot_date"].max() == today
latest_date = df["snapshot_date"].max() if not df.empty else None

# ── Status banner ───────────────────────────────────────────────────────────
status_container = st.container()
if has_today_data:
    status_container.success(
        f"✅ Data is up to date ({today}). Source: {load_source}. No scrape needed."
    )
elif not df.empty:
    status_container.warning(
        f"⚠️ Showing cached data from **{latest_date}** (source: {load_source}). "
        f"Live CME FedWatch is currently unavailable. "
        f"QuikStrike reported: Could not find a current FedFund future for the meeting date 7/28/2027. "
        f"Daily automated scraping is paused until CME fixes the issue."
    )
    if github_error:
        with st.expander("GitHub load details"):
            st.code(github_error)
else:
    status_container.error("❌ No data available. Try manual refresh below.")
    if github_error:
        st.error(f"GitHub load error: {github_error}")

# ── Auto-scrape check (disabled on page load; use scheduled automation) ─────
show_scrape_button = True
auto_scrape_needed = False  # Do not auto-scrape when the page loads

if has_today_data:
    show_scrape_button = True  # Still allow manual refresh

# No automatic on-load scraping; keep the UI responsive when CME is down.
if df.empty:
    st.warning("No cached data available. Click 'Refresh Data' once CME FedWatch is accessible.")
    st.stop()

# ── Manual refresh button ───────────────────────────────────────────────────
if show_scrape_button:
    c1, _, c2 = st.columns([1, 4, 1])
    with c1:
        if has_today_data:
            st.button("🔄 Refresh Data", disabled=True, help=f"Today's data ({today}) already exists. No need to refresh.")
        elif st.button("🔄 Refresh Data", help="Re-scrape CME FedWatch now"):
            if has_today_data:
                st.success(f"✅ Today's data ({today}) already exists. Skipping scrape.")
            else:
                manual_status = st.empty()
                manual_logs = st.empty()

                with st.spinner("Scraping CME FedWatch (up to 4 min)..."):
                    success, msg, count = run_scraper_with_timeout(timeout=240, log_container=manual_logs)
                    if success:
                        manual_status.success(msg)
                        time.sleep(1)
                        st.rerun()
                    else:
                        manual_status.error(msg)
                        # If scraping fails, keep showing cached data instead of blank page
                        if not df.empty:
                            st.warning(f"⚠️ Refresh failed. Continuing to show cached data from {latest_date}.")
    with c2:
        last_updated = max(df["snapshot_date"].unique()) if not df.empty else "?"
        st.caption(f"Last: {last_updated}")


# ════════════════════════════════════════════════════════════════════════════
# Dashboard panels (same 4-panel layout as before)
# ════════════════════════════════════════════════════════════════════════════

all_dates = sorted(df["snapshot_date"].unique())
latest_date = max(all_dates)
today = datetime.now(timezone.utc).date()

latest = df[df["snapshot_date"] == latest_date].copy()
upcoming = latest[latest["meeting_date"] >= today].copy()
if upcoming.empty:
    upcoming = latest.copy()

current_target = (
    upcoming["current_target_rate"].dropna().iloc[0]
    if not upcoming.empty and upcoming["current_target_rate"].notna().any()
    else ""
)
meetings_sorted = sorted(upcoming["meeting_date"].unique())

# ── Sidebar ─────────────────────────────────────────────────────────────────
st.sidebar.success(f"Updated:\n{latest_date}")
st.sidebar.markdown("---")
st.sidebar.markdown(f"**Snapshots:** {len(all_dates)}")
st.sidebar.markdown(f"**Range:** {all_dates[0]} → {all_dates[-1]}")
st.sidebar.markdown("---")
st.sidebar.markdown(
    "**Source:** Official CME FedWatch Tool\n"
    "Scraped via Playwright (cmegroup.cn).\n"
    "Direct from CME — no calculations."
)
if current_target:
    st.sidebar.markdown(f"---\n**Current target:** `{current_target}`")


# ── Helper functions ────────────────────────────────────────────────────────

def show_comparison(curr_df, old_df, meetings):
    """Side-by-side comparison of most likely rate."""
    comp_data = []
    for md in meetings:
        c = curr_df[curr_df["meeting_date"] == md]
        o = old_df[old_df["meeting_date"] == md]
        if c.empty:
            continue
        c_max = c.loc[c["prob_now"].idxmax()]
        curr_rate = c_max["rate_range"]
        curr_prob = c_max["prob_now"]

        if o.empty:
            old_rate = "—"
            old_prob = 0
            change = 0
        else:
            o_max = o.loc[o["prob_now"].idxmax()]
            old_rate = o_max["rate_range"]
            old_prob = o_max["prob_now"]
            change = curr_prob - old_prob

        comp_data.append({
            "Meeting": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
            "Then": f"{old_rate} ({old_prob:.1f}%)" if old_rate != "—" else "—",
            "Now": f"{curr_rate} ({curr_prob:.1f}%)",
            "Change": f"{change:+.1f}%" if old_rate != "—" else "—",
            "Shift": "⚠" if old_rate != "—" and old_rate != curr_rate else "",
        })

    if comp_data:
        st.dataframe(pd.DataFrame(comp_data), use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# Panel 1: Rate Path Summary
# ════════════════════════════════════════════════════════════════════════════
st.header("① Rate Path Summary")
st.caption(
    "Most likely target rate for each upcoming FOMC meeting — market-implied trajectory."
)

path_data = []
for md in meetings_sorted:
    sub = upcoming[upcoming["meeting_date"] == md]
    if sub.empty:
        continue
    # Use the most likely (highest prob_now) rate range for this meeting
    best = sub.sort_values("prob_now", ascending=False).drop_duplicates("rate_range").head(1)
    if best.empty:
        continue
    rng = best["rate_range"].iloc[0]
    prob = best["prob_now"].iloc[0]
    m = re.match(r"(\d+)-(\d+)", str(rng))
    midpoint = (float(m.group(1)) + float(m.group(2))) / 200 if m else 0
    path_data.append({
        "meeting_date": md,
        "meeting_label": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
        "rate_range": rng,
        "midpoint": midpoint,
        "probability": prob,
    })

if path_data:
    path_df = pd.DataFrame(path_data)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=path_df["meeting_label"], y=path_df["midpoint"],
        mode="lines+markers+text",
        text=[f"{r}<br>{p:.0f}%" for r, p in zip(path_df["rate_range"], path_df["probability"])],
        textposition="top center", textfont=dict(size=10),
        line=dict(color="#534AB7", width=3),
        marker=dict(size=12, color="#534AB7"), name="Most likely rate",
        hovertemplate="<b>%{x}</b><br>Rate: %{text}<extra></extra>",
    ))

    if current_target:
        m = re.match(r"(\d+)-(\d+)", current_target)
        if m:
            current_mid = (float(m.group(1)) + float(m.group(2))) / 200
            fig.add_hline(y=current_mid, line_dash="dash", line_color="#e74c3c",
                          annotation_text=f"Current: {current_target}", annotation_position="top left")

    fig.update_layout(
        yaxis_title="Target rate midpoint (%)", yaxis_tickformat=".2f",
        xaxis_title="FOMC meeting", height=400, showlegend=False, hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

    cols = st.columns(min(len(path_data), 6))
    for i, row in enumerate(path_data):
        with cols[i % len(cols)]:
            st.metric(label=row["meeting_label"], value=row["rate_range"],
                      delta=f"{row['probability']:.1f}%")


# ════════════════════════════════════════════════════════════════════════════
# Panel 2: Current Probability Distribution
# ════════════════════════════════════════════════════════════════════════════
st.header("② Current Probability Distribution")
st.caption(
    "Complete probability breakdown: every meeting × every rate range. "
    "Purple = current target. Bold = highest probability in row."
)

pivot = upcoming.pivot_table(
    index="meeting_date", columns="rate_range", values="prob_now", aggfunc="first"
).sort_index()

def sort_key(col):
    m = re.match(r"(\d+)", str(col))
    return float(m.group(1)) if m else 0

if not pivot.empty:
    pivot = pivot.reindex(columns=sorted(pivot.columns, key=sort_key))

    def style_dist(row):
        styles = [""] * len(row)
        for i, col in enumerate(pivot.columns):
            val = row[col]
            if pd.isna(val):
                continue
            if str(col) == str(current_target):
                styles[i] = "background-color: #EEEDFE; font-weight: bold; color: #534AB7"
            elif val == row.max():
                styles[i] = "font-weight: bold; color: #D85A30"
        return styles

    disp_pivot = pivot.copy()
    disp_pivot.index = [
        d.strftime("%b %d, %Y") if hasattr(d, "strftime") else str(d)
        for d in disp_pivot.index
    ]
    styled = disp_pivot.style.format("{:.1f}%", na_rep="—").apply(style_dist, axis=1)
    st.dataframe(styled, use_container_width=True, height=400)


# ════════════════════════════════════════════════════════════════════════════
# Panel 3: Probability Evolution (Current vs 1D / 1W / 1M)
# ════════════════════════════════════════════════════════════════════════════
st.header("③ Probability Evolution: Current vs 1D / 1W / 1M")
st.caption(
    "CME's four standard horizons for the selected meeting. "
    "Compare the probability distribution across Current, 1 Day Ago, 1 Week Ago, and 1 Month Ago."
)

# Clean up duplicates so the same meeting × range appears only once
upcoming = upcoming.drop_duplicates(subset=["meeting_date", "rate_range"])

meeting_options = sorted(upcoming["meeting_date"].unique())
if len(meeting_options) > 0:
    selected_meeting = st.selectbox(
        "Select FOMC meeting",
        options=meeting_options,
        format_func=lambda d: d.strftime("%b %d, %Y") if hasattr(d, "strftime") else str(d),
        index=0,
    )

    sub = upcoming[upcoming["meeting_date"] == selected_meeting].copy()

    def _rate_sort_key(x):
        m = re.match(r"(\d+)", str(x))
        return float(m.group(1)) if m else 0

    sub = sub.sort_values("rate_range", key=lambda col: col.map(_rate_sort_key))

    horizons = {
        "prob_now": "Current",
        "prob_1d": "1 Day Ago",
        "prob_1w": "1 Week Ago",
        "prob_1m": "1 Month Ago",
    }

    # Only show horizons that actually have data in this snapshot
    available_horizons = {k: v for k, v in horizons.items() if sub[k].notna().any()}
    if len(available_horizons) >= 1:
        melted = sub.melt(
            id_vars=["rate_range"],
            value_vars=list(available_horizons.keys()),
            var_name="horizon",
            value_name="probability",
        )
        melted["horizon"] = melted["horizon"].map(available_horizons)
        horizon_order = [v for v in horizons.values() if v in melted["horizon"].values]
        melted["horizon"] = pd.Categorical(melted["horizon"], categories=horizon_order, ordered=True)

        fig3 = px.bar(
            melted,
            x="rate_range",
            y="probability",
            color="horizon",
            barmode="group",
            category_orders={"horizon": horizon_order},
            labels={
                "rate_range": "Target rate range",
                "probability": "Probability (%)",
                "horizon": "",
            },
            color_discrete_map={
                "Current": "#534AB7",
                "1 Day Ago": "#7B68EE",
                "1 Week Ago": "#00B0B9",
                "1 Month Ago": "#9E9E9E",
            },
        )
        fig3.update_layout(height=450, hovermode="x unified")
        st.plotly_chart(fig3, use_container_width=True)

        # Summary table
        table_df = sub[["rate_range"] + list(available_horizons.keys())].rename(
            columns=available_horizons
        ).set_index("rate_range")
        st.dataframe(
            table_df.style.format("{:.1f}%", na_rep="—"),
            use_container_width=True,
        )
    else:
        st.info("No horizon data available for the selected meeting.")
else:
    st.info("No upcoming meeting data available.")


# ════════════════════════════════════════════════════════════════════════════
# Panel 4: Change Alerts (|Δ| ≥ 5%)
# ════════════════════════════════════════════════════════════════════════════
st.header("④ Change Alerts")
st.caption(
    "Significant probability shifts vs yesterday (|Δ| ≥ 5%). Same rate-range comparison."
)

if len(all_dates) >= 2:
    prev_date = all_dates[-2]
    prev_data = df[df["snapshot_date"] == prev_date]

    # Deduplicate to avoid repeated alerts when data has duplicate rows
    upcoming_dedup = upcoming.drop_duplicates(subset=["meeting_date", "rate_range"])
    prev_data_dedup = prev_data.drop_duplicates(subset=["meeting_date", "rate_range"])

    alerts = []
    for md in meetings_sorted:
        curr_sub = upcoming_dedup[upcoming_dedup["meeting_date"] == md]
        prev_sub = prev_data_dedup[prev_data_dedup["meeting_date"] == md]

        if curr_sub.empty or prev_sub.empty:
            continue

        # Merge on rate_range for same-range comparison
        merged = curr_sub.merge(prev_sub, on="rate_range", suffixes=("_now", "_prev"))
        for _, row in merged.iterrows():
            delta = row["prob_now_now"] - row["prob_now_prev"]
            if abs(delta) >= 5:
                alerts.append({
                    "meeting": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
                    "range": row["rate_range"],
                    "prev": row["prob_now_prev"],
                    "now": row["prob_now_now"],
                    "delta": delta,
                })

    if alerts:
        alert_df = pd.DataFrame(alerts)
        for _, a in alert_df.iterrows():
            icon = "🔴" if a["delta"] < 0 else "🟢"
            dstr = f"+{a['delta']:.1f}%" if a["delta"] > 0 else f"{a['delta']:.1f}%"
            st.markdown(
                f"{icon} **{a['meeting']}** | `{a['range']}`  \n"
                f"{a['prev']:.1f}% → **{a['now']:.1f}%** (**{dstr}**)"
            )
    else:
        st.success("No significant changes (|Δ| < 5%). Market stable.")
else:
    st.info("Need ≥2 days of data for change detection.")


# ── Footer ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "Data: Official CME FedWatch Tool (cmegroup.cn) · Scraped via Playwright · "
    "Unofficial tracker for informational purposes only. Not financial advice."
)
