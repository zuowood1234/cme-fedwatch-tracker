# CME FedWatch Tracker

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://zuowood1234-cme-fedwatch-tracker.streamlit.app)

A free, open-source daily tracker for **CME FedWatch interest-rate probabilities** with a live interactive dashboard.

> **What this does:** Scrapes official CME FedWatch probability data, archives it to GitHub, and displays it in a public 4-panel dashboard — rate path, probability distribution, weekly trends, and change alerts.

---

## Dashboard panels

| # | Panel | Description |
|---|-------|-------------|
| 1 | **Rate Path Summary** | Line chart: most likely target rate for each upcoming FOMC meeting |
| 2 | **Probability Distribution** | Full table: every meeting × every rate range (current target highlighted) |
| 3 | **Past Week Daily Changes** | How the most likely rate shifted over the past 7 days |
| 4 | **Change Alerts** | Significant shifts (|Δ| ≥ 5%) vs yesterday, same-rate comparison |

---

## Architecture (v5 — Streamlit Cloud Self-Scraping)

```
CME Chinese FedWatch (cmegroup.cn/fed-watch/)
        |
        v  [Playwright + headed/xvfb browser]
  Streamlit Cloud (app.py + scraper.py)
  ├── Auto-scrape on load if data is stale (>6h)
  ├── Manual "Refresh Data" button
  ├── Push data to GitHub via Contents API (persistence)
        |
        v
  GitHub Repo (zuowood1234/cme-fedwatch-tracker)
  ├── data/daily/YYYY-MM-DD.json   (full snapshots)
  └── data/fedwatch_history.csv    (flat long-format)
```

- **Data source:** Official CME FedWatch Tool (scraped directly, no calculations)
- **Scraping:** Playwright + Chromium with xvfb virtual display on Streamlit Cloud
- **Persistence:** GitHub repo (auto-push via API after each scrape)
- **Hosting:** Streamlit Community Cloud (free tier)
- **Cost:** $0

---

## Deploy to Streamlit Cloud

### Prerequisites

1. Fork/clone this repo to your GitHub account
2. A [GitHub Personal Access Token](https://github.com/settings/tokens/new) with `repo` scope
3. A [Streamlit Cloud](https://share.streamlit.io) account

### Steps

1. **Configure Secrets in Streamlit Cloud**

   In your Streamlit app > Settings > Secrets:

   ```
   GITHUB_TOKEN=ghp_your_token_here
   GITHUB_USERNAME=your_github_username
   ```

2. **Connect & Deploy**

   - Go to [share.streamlit.io](https://share.streamlit.io)
   - **New app** → select your `cme-fedwatch-tracker` repo
   - Branch: `main` | File: `app.py`
   - Click **Deploy**

3. **First Run**

   The app will auto-detect no data and trigger the first scrape (~1-2 min).
   After that, data refreshes automatically every 6 hours.

### Prevent Sleep (optional)

Streamlit Cloud free tier sleeps after 12h of inactivity.
Use [UptimeRobot](https://uptimerobot.com) (free tier) to ping your app URL every 10 min.

---

## Quick start (local)

```bash
git clone https://github.com/zuowood1234/cme-fedwatch-tracker.git
cd cme-fedwatch-tracker
pip install -r requirements.txt
playwright install chromium

# Fetch today's data (opens a visible browser window)
python scraper.py

# Or use legacy script:
python fetch_data.py

# Run the dashboard
streamlit run app.py
```

---

## Project structure

| File | Purpose |
|------|---------|
| `app.py` | Streamlit dashboard + integrated scraper + GitHub sync |
| `scraper.py` | Importable Playwright scraper module (headless/xvfb compatible) |
| `fetch_data.py` | Legacy CLI scraper (standalone, kept for reference) |
| `requirements.txt` | Python dependencies (pip) |
| `packages.txt` | System dependencies for Streamlit Cloud (apt) |
| `.github/workflows/daily-update.yml` | Backup GitHub Actions workflow (disabled by default) |

---

## Data storage

| File | Format | Purpose |
|------|--------|---------|
| `data/daily/YYYY-MM-DD.json` | JSON | Complete daily snapshot (all meetings, all probabilities, NOW/1D/1W/1M) |
| `data/fedwatch_history.csv` | CSV | Flat long-format for dashboard analytics |

---

## Notes

- Data is scraped from CME's **Chinese FedWatch page** (`cmegroup.cn/fed-watch/`) which uses the same QuikStrike tool as the English site but renders more reliably
- The scraper requires a **headed browser** (or xvfb virtual display on Linux servers) because QuikStrike is a heavy ASP.NET control that doesn't render in standard headless mode
- Historical data builds up over time as daily snapshots accumulate
- All data comes directly from CME — no calculations or estimations

---

## License

MIT -- free for any use.

## Acknowledgements

- Data: [CME FedWatch Tool](https://www.cmegroup.cn/fed-watch/)
- Dashboard: [Streamlit](https://streamlit.io)
- Browser automation: [Playwright](https://playwright.dev)
