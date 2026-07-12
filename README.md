# CME FedWatch Tracker

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![GitHub Actions](https://github.com/<YOUR_USERNAME>/cme-fedwatch-tracker/actions/workflows/daily-update.yml/badge.svg)
[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://<YOUR_USERNAME>-cme-fedwatch-tracker.streamlit.app)

A free, open-source daily tracker for **CME FedWatch interest-rate probabilities** with a live interactive dashboard.

> **What this does:** Scrapes official CME FedWatch probability data daily, archives it permanently, and displays it in a public dashboard with 5 panels — rate path, distribution, weekly changes, historical comparison, and change alerts.

---

## Dashboard panels

| # | Panel | Description |
|---|-------|-------------|
| 1 | **Rate Path Summary** | Line chart showing the most likely target rate for each upcoming FOMC meeting (2026-2027) |
| 2 | **Current Probability Distribution** | Full table: every meeting x every rate range, with current target highlighted |
| 3 | **Past Week Daily Changes** | How the most likely rate shifted over the past 7 days (from our archive) |
| 4 | **1-Week vs 1-Month Comparison** | Side-by-side comparison of probabilities then vs now |
| 5 | **Change Alerts** | Highlights meetings where probability shifted >10% day-over-day |

---

## Architecture

```
CME FedWatch Tool (official page)
        |
        v
  Playwright scraper (fetch_data.py)
  - Intercepts API calls
  - Scrapes rendered table
        |
        v
  GitHub Actions (daily at 06:00 UTC)
  - Runs scraper
  - Commits data to repo
        |
        v
  data/daily/YYYY-MM-DD.json  (full snapshots)
  data/fedwatch_history.csv   (flat long-format)
        |
        v
  Streamlit Cloud (free, public dashboard)
  - Reads CSV from GitHub raw URL
  - Auto-refreshes on data update
```

- **Data source:** Official CME FedWatch Tool (scraped, not calculated)
- **Updated:** every day at 06:00 UTC via GitHub Actions
- **Hosted:** Streamlit Community Cloud (free tier)
- **Cost:** $0

---

## Quick start (local)

```bash
git clone https://github.com/<YOUR_USERNAME>/cme-fedwatch-tracker.git
cd cme-fedwatch-tracker
pip install -r requirements.txt
playwright install chromium

# Fetch today's data
python fetch_data.py

# Run the dashboard
streamlit run app.py
```

---

## Deploy your own

### 1. Create the GitHub repo

Push this code to a **public** repo named `cme-fedwatch-tracker`.

### 2. Replace placeholder

In `app.py`, replace `<YOUR_USERNAME>` with your GitHub username.

### 3. Enable the daily update workflow

The workflow is in `.github/workflows/daily-update.yml`.
Go to **Actions** tab -> enable it -> **Run workflow** manually to populate data.

### 4. Deploy to Streamlit Cloud

1. Sign in to [share.streamlit.io](https://share.streamlit.io) with GitHub
2. **New app** -> select your repo, branch `main`, file `app.py`
3. Click **Deploy** -- live in ~2 minutes

### 5. (Optional) Prevent sleep

Streamlit Cloud free tier sleeps after 12h of inactivity.
Use [UptimeRobot](https://uptimerobot.com) (free) to ping your app URL daily.

---

## Data storage

| File | Format | Purpose |
|------|--------|---------|
| `data/daily/YYYY-MM-DD.json` | JSON | Complete daily snapshot (all meetings, all probabilities) |
| `data/fedwatch_history.csv` | CSV | Flat long-format for dashboard analytics |
| `data/debug/` | JSON/HTML | Captured API responses and raw HTML for debugging |

---

## Notes

- Data is scraped directly from CME's official FedWatch page -- no calculations
- The scraper uses Playwright (headless Chromium) because CME renders data via JavaScript
- Historical data builds up over time as the daily workflow runs
- CME page also provides "1 week ago" and "1 month ago" comparison, but our archive enables daily granularity and longer history

---

## License

MIT -- free for any use.

## Acknowledgements

- Data: [CME FedWatch Tool](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html)
- Dashboard: [Streamlit](https://streamlit.io)
- Automation: [GitHub Actions](https://github.com/features/actions)
