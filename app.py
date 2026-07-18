"""
CME FedWatch Tracker — Streamlit Dashboard (Self-Scraping Mode)

Runs on Streamlit Community Cloud:
  1. On load: check data freshness, auto-scrape if stale (>6h)
  2. Manual [Refresh Data] button for on-demand updates
  3. Scrape via Playwright + xvfb (virtual display)
  4. Push new data to GitHub repo for persistence
  5. Display 4-panel dashboard

Secrets required (set in Streamlit Cloud > Settings):
  - GITHUB_TOKEN: GitHub PAT with repo scope
  - GITHUB_USERNAME: your GitHub username
"""

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

AUTO_SCRAPE_INTERVAL_HOURS = 6  # Auto-scrape if data older than this


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

def github_push_data(data_dir):
    """
    Push local data files to GitHub repo using Contents API.
    Uses GITHUB_TOKEN from secrets.
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


def load_csv_from_github(url):
    """Load CSV from GitHub raw URL with fallback."""
    try:
        df = pd.read_csv(url)
        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
        df["meeting_date"] = pd.to_datetime(df["meeting_date"]).dt.date
        df["prob_now"] = pd.to_numeric(df["prob_now"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


def load_local_csv():
    """Load CSV from local filesystem."""
    csv_path = HISTORY_CSV
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
            df["meeting_date"] = pd.to_datetime(df["meeting_date"]).dt.date
            df["prob_now"] = pd.to_numeric(df["prob_now"], errors="coerce")
            return df
        except Exception:
            pass
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
    """Run scraper in a thread with a hard timeout. UI updates happen only from the main thread."""
    log_lines = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_scraper, log_lines=log_lines)
        start = time.time()

        while True:
            # Update UI from the main thread only
            if log_container and log_lines:
                log_container.code("\n".join(log_lines[-8:]), language="text")

            try:
                return future.result(timeout=1)
            except concurrent.futures.TimeoutError:
                if time.time() - start > timeout:
                    return False, f"⏱️ Scraper timed out after {timeout}s.\n\nLatest logs:\n" + "\n".join(log_lines[-10:]), 0
                # otherwise continue loop and keep updating UI


# ── Load data (local first, fallback to GitHub) ─────────────────────────────

# Try local CSV first (from last scrape), then GitHub
df_local = load_local_csv()
df_github = load_csv_from_github(CSV_URL) if df_local.empty or (not df_local.empty and df_local['snapshot_date'].max() < (datetime.now(timezone.utc).date() - timedelta(days=1))) else pd.DataFrame()

# Use whichever is newer
if not df_local.empty and not df_github.empty:
    local_max = df_local['snapshot_date'].max()
    github_max = df_github['snapshot_date'].max()
    df = df_local if local_max >= github_max else df_github
elif not df_local.empty:
    df = df_local
else:
    df = df_github

# ── Auto-scrape check ───────────────────────────────────────────────────────
show_scrape_button = True
auto_scrape_needed = False

if df.empty:
    auto_scrape_needed = True
    st.info("🔍 No data found yet. Starting initial scrape...")
    show_scrape_button = False
else:
    all_dates = sorted(df["snapshot_date"].unique())
    latest_date = max(all_dates)
    hours_since_update = (datetime.now(timezone.utc) - datetime.combine(latest_date, datetime.min.time()).replace(tzinfo=timezone.utc)).total_seconds() / 3600

    if hours_since_update > AUTO_SCRAPE_INTERVAL_HOURS:
        auto_scrape_needed = True
        st.caption(f"⏰ Data is {hours_since_update:.0f}h old. Auto-scraping...")

if auto_scrape_needed:
    scrape_status = st.empty()
    scrape_logs = st.empty()

    with st.spinner("Scraping CME FedWatch data (this may take 2-4 minutes)..."):
        success, msg, count = run_scraper_with_timeout(timeout=240, log_container=scrape_logs)
        if success:
            scrape_status.success(msg)
            # Reload local data
            df = load_local_csv()
            if df.empty:
                df = load_csv_from_github(CSV_URL)
            st.rerun()
        else:
            scrape_status.error(msg)
            # Fallback: try GitHub CSV even if stale
            if not df.empty:
                st.warning("⚠️ Scraping failed. Showing cached data from GitHub/local storage.")
            else:
                st.warning("⚠️ Scraping failed and no cached data is available. Try the manual refresh button.")

# ── Manual refresh button ───────────────────────────────────────────────────
if show_scrape_button:
    c1, _, c2 = st.columns([1, 4, 1])
    with c1:
        if st.button("🔄 Refresh Data", help="Re-scrape CME FedWatch now"):
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
    with c2:
        last_updated = max(df["snapshot_date"].unique()) if not df.empty else "?"
        st.caption(f"Last: {last_updated}")

# ── Guard: no data ──────────────────────────────────────────────────────────
if df.empty:
    st.warning("No data available. Click 'Refresh Data' to scrape.")
    st.stop()


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
    max_row = sub.loc[sub["prob_now"].idxmax()]
    rng = max_row["rate_range"]
    m = re.match(r"(\d+)-(\d+)", str(rng))
    midpoint = (float(m.group(1)) + float(m.group(2))) / 200 if m else 0
    path_data.append({
        "meeting_date": md,
        "meeting_label": md.strftime("%b %d, %Y"),
        "rate_range": rng,
        "midpoint": midpoint,
        "probability": max_row["prob_now"],
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
        yaxis_title="Target rate (%)", yaxis_tickformat=".2f",
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
# Panel 3: Past Week Daily Changes
# ════════════════════════════════════════════════════════════════════════════
st.header("③ Past Week - Daily Changes")
st.caption(
    "How the most likely rate for each meeting changed over the past 7 days."
)

past_week_dates = [d for d in all_dates if d >= latest_date - timedelta(days=7)]
past_week = df[df["snapshot_date"].isin(past_week_dates)].copy()

if len(past_week_dates) >= 2:
    weekly_path = []
    for d in past_week_dates:
        day_data = past_week[past_week["snapshot_date"] == d]
        for md in meetings_sorted:
            sub = day_data[day_data["meeting_date"] == md]
            if sub.empty:
                continue
            max_row = sub.loc[sub["prob_now"].idxmax()]
            rng = max_row["rate_range"]
            rm = re.match(r"(\d+)-(\d+)", str(rng))
            midpoint = (float(rm.group(1)) + float(rm.group(2))) / 200 if rm else 0
            weekly_path.append({
                "date": d, "meeting_date": md,
                "meeting_label": md.strftime("%b %d, %Y"),
                "midpoint": midpoint, "rate_range": rng,
            })

    if weekly_path:
        wp_df = pd.DataFrame(weekly_path)
        fig3 = px.line(wp_df, x="date", y="midpoint", color="meeting_label",
                       markers=True, labels={"date": "", "midpoint": "Rate (%)", "meeting_label": "Meeting"})
        fig3.update_layout(height=400, hovermode="x unified", yaxis_tickformat=".2f")
        st.plotly_chart(fig3, use_container_width=True)

        wt = wp_df.pivot_table(index="meeting_label", columns="date", values="rate_range", aggfunc="first").sort_index()
        wt.columns = [d.strftime("%m/%d") if hasattr(d, "strftime") else str(d) for d in wt.columns]
        st.dataframe(wt, use_container_width=True)
    else:
        st.info("Not enough data for weekly changes.")
else:
    st.info(f"Need ≥2 days of data (have {len(past_week_dates)}). Check back soon.")


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

    alerts = []
    for md in meetings_sorted:
        curr_sub = upcoming[upcoming["meeting_date"] == md]
        prev_sub = prev_data[prev_data["meeting_date"] == md]

        if curr_sub.empty or prev_sub.empty:
            continue

        # Merge on rate_range for same-range comparison
        merged = curr_sub.merge(prev_sub, on="rate_range", suffixes=("_now", "_prev"))
        for _, row in merged.iterrows():
            delta = row["prob_now"] - row["prob_prev"]
            if abs(delta) >= 5:
                alerts.append({
                    "meeting": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
                    "range": row["rate_range"],
                    "prev": row["prob_prev"],
                    "now": row["prob_now"],
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
