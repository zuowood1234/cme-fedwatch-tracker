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
import json
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

# ── Custom CSS for enhanced visual style ────────────────────────────────────
st.markdown("""
<style>
/* ── Financial Editorial Aesthetic ──────────────────────────────────── */
/* Deep ink-blue primary, warm amber accents, editorial typography      */

:root {
  --ink-900: #0A1628;
  --ink-800: #0F2847;
  --ink-700: #1565C0;
  --ink-600: #1976D2;
  --ink-50: #F5F8FC;
  --ink-100: #E8EEF7;
  --amber: #E8A93B;
  --amber-light: #F5D78A;
  --green: #1B7A3E;
  --green-light: #2E9B52;
  --red: #C0392B;
  --red-light: #E74C3C;
  --slate: #546E7A;
  --slate-light: #78909C;
}

/* Main title — ink-deep gradient banner with subtle texture */
h1 {
  background: linear-gradient(135deg, #0A1628 0%, #1565C0 100%);
  color: white !important;
  font-size: 2.0rem !important;
  font-weight: 800 !important;
  letter-spacing: -0.3px;
  padding: 20px 24px !important;
  border-radius: 14px;
  margin-bottom: 0.5rem !important;
  box-shadow: 0 4px 24px rgba(21, 101, 192, 0.18), 0 1px 3px rgba(0,0,0,0.08);
  position: relative;
  overflow: hidden;
}
h1::after {
  content: '';
  position: absolute;
  top: 0; right: 0;
  width: 180px; height: 100%;
  background: linear-gradient(135deg, transparent, rgba(232, 169, 59, 0.12));
  pointer-events: none;
}

/* Caption / subtitle */
.stCaption p, [data-testid="stCaptionContainer"] {
  color: #546E7A !important;
  font-size: 0.9rem !important;
  line-height: 1.5;
}

/* Section headers (① ② ③ ④) — refined bar with amber left accent */
h2 {
  background: linear-gradient(90deg, #F0F5FB 0%, #FAFCFE 100%);
  color: #0A1628 !important;
  font-weight: 700 !important;
  font-size: 1.3rem !important;
  padding: 12px 18px !important;
  border-radius: 10px;
  border-left: 4px solid #E8A93B;
  margin-top: 1.2rem !important;
  margin-bottom: 0.8rem !important;
  letter-spacing: -0.2px;
}

/* Sub-headers (####) */
h4 {
  color: #0F2847 !important;
  font-weight: 600 !important;
  border-left: 3px solid #1565C0;
  padding-left: 12px !important;
  margin-top: 1rem !important;
}

/* Metric cards — subtle with refined shadow and hover lift */
[data-testid="stMetric"], div[data-testid="stMetric"] {
  background: #FFFFFF;
  border-radius: 12px;
  padding: 14px 18px !important;
  border: 1px solid #E0E8F0;
  box-shadow: 0 1px 3px rgba(10, 22, 40, 0.04);
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
[data-testid="stMetric"]:hover, div[data-testid="stMetric"]:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(21, 101, 192, 0.12), 0 2px 6px rgba(10, 22, 40, 0.06);
  border-color: #1565C0;
}

/* Buttons — deep ink with amber accent border */
.stButton > button, button[kind="primary"] {
  background: linear-gradient(135deg, #0F2847 0%, #1565C0 100%) !important;
  color: white !important;
  border-radius: 10px !important;
  border: none !important;
  font-weight: 600 !important;
  padding: 8px 20px !important;
  transition: all 0.2s ease !important;
  box-shadow: 0 2px 8px rgba(21, 101, 192, 0.2) !important;
}
.stButton > button:hover, button[kind="primary"]:hover {
  background: linear-gradient(135deg, #0A1628 0%, #0F2847 100%) !important;
  transform: translateY(-1px) !important;
  box-shadow: 0 4px 14px rgba(21, 101, 192, 0.28) !important;
}

/* Status banners */
[data-testid="stAlert"] {
  border-radius: 10px !important;
  border: none !important;
  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}

/* Main content — max width and top spacing */
.block-container, [data-testid="stAppViewBlockContainer"] {
  padding-top: 1.5rem !important;
  max-width: 1280px;
}

/* Sidebar header — no background */
[data-testid="stSidebar"] h2 {
  background: none;
  border-left: none;
  color: #0F2847 !important;
}

/* ── Per-meeting summary cards ──────────────────────────────────────── */
.pm-card {
  background: #FFFFFF;
  border: 1px solid #E0E8F0;
  border-radius: 12px;
  padding: 14px 18px;
  margin-bottom: 10px;
  box-shadow: 0 1px 3px rgba(10, 22, 40, 0.04);
  transition: all 0.2s ease;
}
.pm-card:hover {
  border-color: #1565C0;
  box-shadow: 0 4px 14px rgba(21, 101, 192, 0.1);
}
.pm-meeting {
  font-weight: 700;
  color: #0A1628;
  font-size: 1.05rem;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.pm-label {
  font-size: 0.78rem;
  font-weight: 600;
  color: #546E7A;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-top: 8px;
  margin-bottom: 4px;
}
.pm-changes {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.pm-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 0.85rem;
  font-weight: 600;
  font-family: ui-monospace, 'SF Mono', Menlo, monospace;
}
.pm-chip-up {
  background: #E8F5E9;
  color: #1B7A3E;
  border: 1px solid #A5D6A7;
}
.pm-chip-down {
  background: #FFEBEE;
  color: #C0392B;
  border: 1px solid #EF9A9A;
}
.pm-chip-hot {
  background: linear-gradient(135deg, #FFF8E1 0%, #FFECB3 100%);
  color: #E65100 !important;
  border: 1px solid #FFB74D;
  font-weight: 700;
  box-shadow: 0 1px 4px rgba(230, 81, 0, 0.15);
  animation: pulse-hot 2s ease-in-out infinite;
}
@keyframes pulse-hot {
  0%, 100% { box-shadow: 0 1px 4px rgba(230, 81, 0, 0.15); }
  50% { box-shadow: 0 2px 10px rgba(230, 81, 0, 0.3); }
}
.pm-hot-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  background: linear-gradient(135deg, #E65100, #FF6F00);
  color: white;
  font-size: 0.7rem;
  font-weight: 800;
  padding: 1px 7px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
</style>
""", unsafe_allow_html=True)

st.title("📊 CME FedWatch Tracker")
st.caption(
    "Official CME FedWatch probabilities for every upcoming FOMC meeting. "
    "Data scraped directly from CME. Auto-refreshed. "
    "Source: [cmegroup.cn/fed-watch/](https://www.cmegroup.cn/fed-watch/)"
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


def _format_scrape_time(iso_str):
    """Convert ISO scrape time to a friendly CST (UTC+8) string."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cst = dt.astimezone(timezone(timedelta(hours=8)))
        return cst.strftime("%Y-%m-%d %H:%M CST")
    except Exception:
        return None


def get_scrape_time(snapshot_date):
    """
    Get the exact scrape time for a snapshot date.
    Tries local daily JSON first, then GitHub raw URL.
    Returns (time_str, source) or (None, None) on failure.
    """
    if snapshot_date is None:
        return None, None

    date_str = snapshot_date.strftime("%Y-%m-%d") if hasattr(snapshot_date, "strftime") else str(snapshot_date)
    local_json = os.path.join(DAILY_DIR, f"{date_str}.json")

    # Try local file first
    if os.path.exists(local_json):
        try:
            with open(local_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            iso = data.get("scrape_time") or data.get("date")
            return _format_scrape_time(iso), "local"
        except Exception:
            pass

    # Fallback: GitHub raw URL
    github_json_url = f"{GITHUB_RAW_BASE}/data/daily/{date_str}.json"
    try:
        import requests
        resp = requests.get(github_json_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            iso = data.get("scrape_time") or data.get("date")
            return _format_scrape_time(iso), "GitHub"
    except Exception:
        pass

    return None, None


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
latest_scrape_time, latest_scrape_source = get_scrape_time(latest_date) if latest_date else (None, None)

# ── Status banner ───────────────────────────────────────────────────────────
status_container = st.container()
scrape_time_str = f" (scraped at {latest_scrape_time})" if latest_scrape_time else ""
if has_today_data:
    status_container.success(
        f"✅ Data is up to date ({today}). Source: {load_source}. "
        f"Daily scrape runs around 09:50 CST.{scrape_time_str}"
    )
elif not df.empty:
    status_container.warning(
        f"⚠️ Showing cached data from **{latest_date}** (source: {load_source}). "
        f"Daily automated scrape runs around 09:50 CST.{scrape_time_str}"
    )
    if github_error:
        with st.expander("GitHub load details"):
            st.code(github_error)
else:
    status_container.error("❌ No data available. Try manual refresh below.")
    if github_error:
        st.error(f"GitHub load error: {github_error}")

# ── Manual refresh button (always available for force refresh) ────────────
if df.empty:
    st.warning("No cached data available. Click 'Refresh Data' once CME FedWatch is accessible.")
    st.stop()

# ── Auto-refresh safety net (when data is stale) ─────────────────────────
# If the latest snapshot is older than STALE_HOURS, auto-trigger a scrape
# so the dashboard never shows completely stale data when GitHub Actions
# daily workflow misses its schedule.
STALE_HOURS = 12
auto_scrape_triggered = False
if (
    not df.empty
    and latest_date is not None
    and latest_date < today
):
    if latest_scrape_time:
        try:
            from datetime import datetime as _dt
            scrape_dt = _dt.fromisoformat(
                latest_scrape_time.replace("CST", "+08:00")
                .replace(" ", "T")
            )
            age_hours = (datetime.now(timezone.utc) - scrape_dt).total_seconds() / 3600
            if age_hours > STALE_HOURS:
                auto_scrape_triggered = True
        except Exception:
            age_days = (today - latest_date).days
            if age_days >= 1:
                auto_scrape_triggered = True
    else:
        age_days = (today - latest_date).days
        if age_days >= 1:
            auto_scrape_triggered = True

if auto_scrape_triggered:
    st.warning(
        f"⚠️ Data is stale (last update: {latest_date}). "
        f"GitHub Actions daily workflow may not have run. "
        f"Auto-refreshing now..."
    )
    with st.spinner("Auto-refreshing CME FedWatch (up to 4 min)..."):
        auto_status = st.empty()
        auto_logs = st.empty()
        success, msg, count = run_scraper_with_timeout(timeout=240, log_container=auto_logs)
        if success:
            auto_status.success(msg)
            time.sleep(1)
            st.rerun()
        else:
            auto_status.error(f"Auto-refresh failed: {msg}")
            st.warning("Continuing to show cached data. Use Force Refresh button to retry.")

c1, _, c2 = st.columns([1, 4, 1])
with c1:
    button_label = "🔄 Force Refresh" if has_today_data else "🔄 Refresh Data"
    button_help = (
        f"Today's data ({today}) exists but you can force a re-scrape."
        if has_today_data else "Re-scrape CME FedWatch now"
    )
    if st.button(button_label, help=button_help):
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
                if not df.empty:
                    st.warning(f"⚠️ Refresh failed. Continuing to show cached data from {latest_date}.")
with c2:
    last_updated = max(df["snapshot_date"].unique()) if not df.empty else "?"
    if latest_scrape_time:
        st.caption(f"Last: {last_updated} @ {latest_scrape_time}")
    else:
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

def _clean_target_rate(value):
    """Extract a clean 'XXX-YYY' target rate string from a possibly noisy value."""
    if pd.isna(value):
        return ""
    m = re.search(r"(\d{1,3})-(\d{1,3})", str(value))
    return f"{m.group(1)}-{m.group(2)}" if m else ""


current_target = (
    _clean_target_rate(upcoming["current_target_rate"].dropna().iloc[0])
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
    "Most likely target rate for each upcoming FOMC meeting. "
    "Rate is the median-implied range: the highest range whose cumulative probability reaches ≥ 50%."
)


def _range_bounds(rng):
    """Return (lower_bps, upper_bps) from a '350-375' or '350-375 (Current)' string."""
    m = re.match(r"(\d+)-(\d+)", str(rng))
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def _range_midpoint(rng):
    lo, hi = _range_bounds(rng)
    if lo is None:
        return 0
    return (lo + hi) / 200  # bps -> %


def _most_likely_range_by_median(sub, prob_col="prob_now"):
    """
    Pick the median-implied rate range: sort ranges from high to low and
    accumulate probabilities until the cumulative sum reaches ≥ 50%.
    prob_col: which probability column to use ('prob_now', 'prob_1d', etc.)
    Returns (rate_range, cumulative_probability_at_threshold, rows_used).
    Returns (None, 0, sub) if the probability column has no valid data.

    Special case: if there are only 2 rate ranges, return the higher-prob range
    with its own prob (not cumulative — cumulative is meaningless when there
    are only 2 options).
    """
    if sub.empty:
        return None, 0, sub
    dedup = sub.drop_duplicates("rate_range").copy()
    if prob_col not in dedup.columns or dedup[prob_col].fillna(0).sum() == 0:
        return None, 0, sub

    # Special case: only 2 rate ranges → pick higher-prob range directly
    if len(dedup) == 2:
        best = dedup.loc[dedup[prob_col].idxmax()]
        return best["rate_range"], best[prob_col], dedup

    dedup["_lo"] = dedup["rate_range"].apply(lambda x: _range_bounds(x)[0])
    dedup = dedup.sort_values("_lo", ascending=False).reset_index(drop=True)  # high -> low
    cum = 0.0
    chosen_idx = None
    chosen_cum = 0.0
    for i, row in dedup.iterrows():
        cum += row[prob_col]
        if cum >= 50 and chosen_idx is None:
            chosen_idx = i
            chosen_cum = cum
    if chosen_idx is None:
        chosen_idx = len(dedup) - 1
        chosen_cum = cum
    chosen = dedup.iloc[chosen_idx]
    return chosen["rate_range"], chosen_cum, dedup.iloc[: chosen_idx + 1]


def compute_rate_path(df_snapshot, meetings_sorted, prob_col="prob_now"):
    """Compute the median-implied rate path for a given snapshot using the specified probability column."""
    path_data = []
    for md in meetings_sorted:
        sub = df_snapshot[df_snapshot["meeting_date"] == md]
        rng, prob, _ = _most_likely_range_by_median(sub, prob_col=prob_col)
        if rng is None:
            continue
        midpoint = _range_midpoint(rng)
        path_data.append({
            "meeting_date": md,
            "meeting_label": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
            "rate_range": rng,
            "midpoint": midpoint,
            "probability": prob,
        })
    return pd.DataFrame(path_data) if path_data else pd.DataFrame()


# Current rate path (using prob_now)
path_df = compute_rate_path(upcoming, meetings_sorted, prob_col="prob_now")

# 1-day-ago rate path (using prob_1d from the same snapshot — matches CME's "1 Day Ago" column)
prev_path_df = compute_rate_path(upcoming, meetings_sorted, prob_col="prob_1d")

# Identify meetings where the most-likely rate range changed vs 1 day ago (CME's own 1D column)
path_changes = []
if not path_df.empty and not prev_path_df.empty:
    merged_path = path_df.merge(
        prev_path_df[["meeting_date", "rate_range", "probability"]],
        on="meeting_date", how="left", suffixes=("", "_prev")
    )
    for _, row in merged_path.iterrows():
        prev_rng = row.get("rate_range_prev")
        if pd.notna(prev_rng) and prev_rng != row["rate_range"]:
            curr_rng = row["rate_range"]
            md_sub = upcoming[upcoming["meeting_date"] == row["meeting_date"]].drop_duplicates("rate_range")
            # Cumulative prob_now (today) and prob_1d (yesterday) for curr_rng and above
            md_sub_sorted = md_sub.copy()
            md_sub_sorted["_lo"] = md_sub_sorted["rate_range"].apply(lambda x: _range_bounds(x)[0])
            md_sub_sorted = md_sub_sorted.sort_values("_lo", ascending=False)
            cum_now = 0.0
            cum_1d = 0.0
            for _, r in md_sub_sorted.iterrows():
                cum_now += r["prob_now"]
                if "prob_1d" in md_sub_sorted.columns:
                    cum_1d += r["prob_1d"] if pd.notna(r["prob_1d"]) else 0
                if r["rate_range"] == curr_rng:
                    break
            path_changes.append({
                "meeting_date": row["meeting_date"],
                "meeting_label": row["meeting_label"],
                "prev_rate": prev_rng,
                "curr_rate": curr_rng,
                "cum_now": cum_now,
                "cum_1d": cum_1d,
            })
    path_df = merged_path  # keep merged data for plotting

if not path_df.empty:
    # Build y-axis tick labels that show the actual rate ranges
    tick_vals = sorted(path_df["midpoint"].unique())
    tick_text = []
    for v in tick_vals:
        rows = path_df[path_df["midpoint"] == v]
        tick_text.append(rows["rate_range"].iloc[0])

    # Color markers: highlight meetings where rate path changed
    marker_colors = []
    marker_sizes = []
    text_colors = []
    for _, row in path_df.iterrows():
        prev_rng = row.get("rate_range_prev")
        if pd.notna(prev_rng) and prev_rng != row["rate_range"]:
            marker_colors.append("#D85A30")  # 醒目橙色/红色
            marker_sizes.append(18)
            text_colors.append("#D85A30")
        else:
            marker_colors.append("#1565C0")
            marker_sizes.append(12)
            text_colors.append("#333333")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=path_df["meeting_label"], y=path_df["midpoint"],
        mode="lines+markers+text",
        text=[f"{p:.0f}%" for p in path_df["probability"]],
        textposition="top center", textfont=dict(size=11, color=text_colors, family="ui-monospace, 'SF Mono', Menlo, monospace"),
        line=dict(color="#1565C0", width=3.5, shape="spline", smoothing=0.3),
        marker=dict(size=marker_sizes, color=marker_colors, line=dict(color="white", width=2)), name="Most likely rate",
        hovertemplate="<b>%{x}</b><br>Rate: %{customdata}<br>Probability: %{text}<extra></extra>",
        customdata=path_df["rate_range"],
    ))

    if current_target:
        current_mid = _range_midpoint(current_target)
        if current_mid:
            fig.add_hline(y=current_mid, line_dash="dash", line_color="#e74c3c")

    fig.update_layout(
        yaxis=dict(
            title=dict(
                text="Target rate range (%)",
                font=dict(size=13, color="#546E7A"),
            ),
            tickmode="array",
            tickvals=tick_vals,
            ticktext=tick_text,
            tickfont=dict(size=12, color="#37474F"),
            gridcolor="#ECEFF1",
            gridwidth=1,
            zeroline=False,
        ),
        xaxis=dict(
            title=dict(
                text="FOMC meeting",
                font=dict(size=13, color="#546E7A"),
            ),
            tickfont=dict(size=12, color="#37474F"),
            gridcolor="#ECEFF1",
            gridwidth=1,
        ),
        height=440,
        showlegend=False,
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=20, r=20, t=20, b=20),
        hoverlabel=dict(
            bgcolor="#0A1628",
            font_size=13,
            font_color="white",
            bordercolor="#1565C0",
            borderwidth=1,
        ),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Show textual summary of rate-path changes
    if path_changes:
        st.markdown("#### ⚠️ Rate path changed vs 1 Day Ago (CME)")
        st.caption("*Cumulative probability = sum of this range and all higher ranges. ≥50% = median-implied most likely rate.*")
        for ch in sorted(path_changes, key=lambda x: x["meeting_date"]):
            curr_rng = ch["curr_rate"]
            cum_now = ch["cum_now"]
            cum_1d = ch["cum_1d"]
            delta = cum_now - cum_1d
            delta_str = f"+{delta:.1f}%" if delta > 0 else f"{delta:.1f}%"
            st.markdown(
                f"- **{ch['meeting_label']}**: `{ch['prev_rate']}` → `{curr_rng}` "
                f"| `{curr_rng}` cumulative: {cum_1d:.1f}% → **{cum_now:.1f}%** ({delta_str})"
            )
    else:
        st.info("No rate-path changes vs 1 Day Ago. The median-implied target rate is stable across all meetings.")

    cols = st.columns(min(len(path_df), 6))
    for i, row in path_df.iterrows():
        with cols[i % len(cols)]:
            delta_color = "normal"
            prev_rng = row.get("rate_range_prev")
            if pd.notna(prev_rng) and prev_rng != row["rate_range"]:
                delta_color = "off"  # Streamlit uses "off" for red-ish, "normal" for default
            st.metric(
                label=row["meeting_label"],
                value=row["rate_range"],
                delta=f"{row['probability']:.1f}%",
                delta_color=delta_color,
            )


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
                styles[i] = "background-color: #E3F2FD; font-weight: bold; color: #0D47A1"
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
        "prob_1m": "1 Month Ago",
        "prob_1w": "1 Week Ago",
        "prob_1d": "1 Day Ago",
        "prob_now": "Current",
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
                "1 Month Ago": "#B0BEC5",
                "1 Week Ago": "#64B5F6",
                "1 Day Ago": "#1976D2",
                "Current": "#0A1628",
            },
        )
        fig3.update_layout(
            height=460,
            hovermode="x unified",
            plot_bgcolor="white",
            paper_bgcolor="white",
            bargap=0.25,
            bargroupgap=0.08,
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis=dict(
                title=dict(
                    text="Target rate range",
                    font=dict(size=13, color="#546E7A"),
                ),
                tickfont=dict(size=12, color="#37474F"),
                gridcolor="#ECEFF1",
                gridwidth=1,
            ),
            yaxis=dict(
                title=dict(
                    text="Probability (%)",
                    font=dict(size=13, color="#546E7A"),
                ),
                tickfont=dict(size=12, color="#37474F"),
                gridcolor="#ECEFF1",
                gridwidth=1,
                zeroline=False,
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                font=dict(size=12, color="#37474F"),
                bgcolor="rgba(255,255,255,0.9)",
                bordercolor="#E0E8F0",
                borderwidth=1,
            ),
            hoverlabel=dict(
                bgcolor="#0A1628",
                font_size=13,
                font_color="white",
                bordercolor="#1565C0",
                borderwidth=1,
            ),
        )
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
    "Significant probability shifts vs 1 day ago and vs 1 week ago (|Δ| ≥ 5%). "
    "Uses CME's own prob_1d and prob_1w columns. One row per meeting × rate range."
)


# Deduplicate snapshot data so each meeting × range appears once
upcoming_dedup = upcoming.drop_duplicates(subset=["meeting_date", "rate_range"])

if not upcoming_dedup.empty:
    alerts = []
    for md in meetings_sorted:
        curr_sub = upcoming_dedup[upcoming_dedup["meeting_date"] == md]
        if curr_sub.empty:
            continue

        for _, crow in curr_sub.iterrows():
            rng = crow["rate_range"]
            curr_prob = crow["prob_now"]
            d1 = None
            w1 = None

            # Use CME's own prob_1d and prob_1w columns (consistent with Panel 1 & 3)
            if "prob_1d" in crow.index and pd.notna(crow["prob_1d"]) and crow["prob_1d"] > 0:
                d1 = curr_prob - crow["prob_1d"]
            if "prob_1w" in crow.index and pd.notna(crow["prob_1w"]) and crow["prob_1w"] > 0:
                w1 = curr_prob - crow["prob_1w"]

            if (d1 is not None and abs(d1) >= 5) or (w1 is not None and abs(w1) >= 5):
                alerts.append({
                    "meeting_date": md,
                    "meeting": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
                    "range": rng,
                    "current": curr_prob,
                    "1d_delta": d1,
                    "1w_delta": w1,
                })

    if alerts:
        alert_df = pd.DataFrame(alerts)
        # Sort by meeting date (chronological) then by rate range
        alert_df = alert_df.sort_values(["meeting_date", "range"])

        # Format deltas with signs and arrows
        def _fmt_delta(v):
            if v is None:
                return "—"
            sign = "+" if v > 0 else ""
            return f"{sign}{v:.1f}%"

        def _fmt_delta_color(v):
            if v is None:
                return ""
            return "color: #D85A30" if v > 0 else "color: #27AE60"

        display_df = alert_df.copy()
        display_df["1 day ago"] = display_df["1d_delta"].apply(_fmt_delta)
        display_df["1 week ago"] = display_df["1w_delta"].apply(_fmt_delta)
        display_df["current"] = display_df["current"].apply(lambda x: f"{x:.1f}%")
        display_df = display_df.rename(columns={"range": "rate range", "current": "now"})

        def _style_alerts(row):
            """Color the delta cells by using the original alert_df row via the shared index."""
            styles = [""] * len(row)
            src = alert_df.loc[row.name]
            # 1 day ago column
            if "1 day ago" in row.index:
                idx = list(row.index).index("1 day ago")
                styles[idx] = _fmt_delta_color(src["1d_delta"])
            # 1 week ago column
            if "1 week ago" in row.index:
                idx = list(row.index).index("1 week ago")
                styles[idx] = _fmt_delta_color(src["1w_delta"])
            return styles

        st.dataframe(
            display_df[["meeting", "rate range", "now", "1 day ago", "1 week ago"]]
            .style.apply(_style_alerts, axis=1)
            .hide(axis="index"),
            use_container_width=True,
        )

        # Per-meeting summary cards (sorted chronologically, colored by direction, hot badge for >=15%)
        if len(alert_df) > 0:
            st.markdown("#### Per-meeting summary")
            st.caption("Colored chips show each significant change. Green = up, red = down. 🔥 = change ≥ 15%.")

            def _chip_html(range_name, delta_value):
                """Render a single change chip with appropriate color and hot badge."""
                if delta_value is None:
                    return ""
                direction = "up" if delta_value >= 0 else "down"
                is_hot = abs(delta_value) >= 15
                sign = "+" if delta_value >= 0 else ""
                label = f"{range_name} {sign}{delta_value:.1f}%"
                hot_class = " pm-chip-hot" if is_hot else ""
                dir_class = f"pm-chip-{direction}"
                hot_badge = '<span class="pm-hot-badge">🔥 15%+</span>' if is_hot else ''
                return f'<span class="pm-chip {dir_class}{hot_class}">{label} {hot_badge}</span>'

            # Sort meetings chronologically by meeting_date
            sorted_meetings = sorted(
                alert_df["meeting_date"].unique()
            )

            for md in sorted_meetings:
                g = alert_df[alert_df["meeting_date"] == md]
                if g.empty:
                    continue
                meeting_label = g["meeting"].iloc[0]

                d_chips = []
                w_chips = []
                has_hot = False
                for _, r in g.iterrows():
                    if r["1d_delta"] is not None and abs(r["1d_delta"]) >= 5:
                        d_chips.append(_chip_html(r["range"], r["1d_delta"]))
                        if abs(r["1d_delta"]) >= 15:
                            has_hot = True
                    if r["1w_delta"] is not None and abs(r["1w_delta"]) >= 5:
                        w_chips.append(_chip_html(r["range"], r["1w_delta"]))
                        if abs(r["1w_delta"]) >= 15:
                            has_hot = True

                hot_notice = '<span class="pm-hot-badge">🔥 SIGNIFICANT</span>' if has_hot else ''

                d_html = (
                    '<div class="pm-changes">' + "".join(d_chips) + '</div>'
                    if d_chips else '<span style="color:#90A4AE; font-size:0.85rem;">—</span>'
                )
                w_html = (
                    '<div class="pm-changes">' + "".join(w_chips) + '</div>'
                    if w_chips else '<span style="color:#90A4AE; font-size:0.85rem;">—</span>'
                )

                card_html = f"""
                <div class="pm-card">
                  <div class="pm-meeting">
                    {meeting_label}
                    {hot_notice}
                  </div>
                  <div class="pm-label">vs 1 Day Ago</div>
                  {d_html}
                  <div class="pm-label">vs 1 Week Ago</div>
                  {w_html}
                </div>
                """
                st.markdown(card_html, unsafe_allow_html=True)
    else:
        st.success("No significant changes (|Δ| < 5%) vs 1 day ago or 1 week ago. Market stable.")
else:
    st.info("No upcoming data available for change detection.")


# ── Footer ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "Data: Official CME FedWatch Tool (cmegroup.cn) · Scraped via Playwright · "
    "Unofficial tracker for informational purposes only. Not financial advice."
)
