#!/usr/bin/env python3
"""
CME FedWatch Data Scraper

Scrapes OFFICIAL probability data directly from the CME FedWatch tool page.
Uses Playwright (headless browser) because the page renders data via JavaScript.

Strategy (dual approach for robustness):
  1. Intercept XHR/fetch responses — if CME loads data via API, capture the JSON
  2. Scrape the rendered HTML table as fallback

Data is saved in two formats:
  - data/daily/YYYY-MM-DD.json   (full snapshot, complete state)
  - data/fedwatch_history.csv     (flat long-format, for dashboard analytics)

Usage:
    python fetch_data.py
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
CME_FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DAILY_DIR = DATA_DIR / "daily"
HISTORY_CSV = DATA_DIR / "fedwatch_history.csv"

CSV_HEADER = [
    "date", "meeting_date", "rate_range", "probability",
    "is_most_likely", "current_target", "effr", "source",
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def parse_rate_range(text):
    """Parse '3.50-3.75%' or '3.50%-3.75%' into normalized '3.50%-3.75%'."""
    text = text.strip()
    # Match patterns like 3.50-3.75, 3.50%-3.75%, 3.5-3.75
    m = re.search(r"(\d+\.\d+)\s*%?\s*[-–]\s*(\d+\.\d+)\s*%", text)
    if m:
        return f"{m.group(1)}%-{m.group(2)}%"
    return text


def parse_percentage(text):
    """Parse '87.5%' or '87.5' into float 87.5."""
    text = text.strip().replace("%", "").replace("<", "").replace("≈", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_meeting_date(text):
    """Parse 'Jul 29' or 'Jul 29, 2026' or '29-Jul-26' into YYYY-MM-DD."""
    text = text.strip()
    for fmt in ["%b %d, %Y", "%b %d %Y", "%d-%b-%y", "%d-%b-%Y", "%B %d, %Y", "%B %d %Y"]:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text  # return as-is if can't parse


def extract_current_target(page):
    """Try to find the current target rate range from the page."""
    try:
        # Look for text like "Current Rate: 3.50%-3.75%" or highlighted current column
        text = page.inner_text("body")
        m = re.search(r"(?:current|target).*?(\d+\.\d+)\s*%?\s*[-–]\s*(\d+\.\d+)\s*%", text, re.IGNORECASE)
        if m:
            return f"{m.group(1)}%-{m.group(2)}%"
    except Exception:
        pass
    return ""


def extract_effr(page):
    """Try to find the current EFFR from the page."""
    try:
        text = page.inner_text("body")
        m = re.search(r"(?:EFFR|Effective Federal Funds Rate).*?(\d+\.\d+)\s*%", text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


# ── Core scraper ────────────────────────────────────────────────────────────

def scrape_cme_fedwatch(max_retries=3):
    """
    Launch Playwright, navigate to CME FedWatch, extract probability data.
    Includes retry logic for transient network errors.

    Returns dict with keys:
        meetings: list of {date, probabilities: {range: pct}}
        current_target: str
        effr: float or None
        captured_apis: list of {url, data} (for debugging)
        raw_html: str (snapshot of table HTML for debugging)
    """
    captured_apis = []
    last_error = None

    # Use Firefox instead of Chromium — different HTTP stack avoids ERR_HTTP2_PROTOCOL_ERROR
    # Firefox uses its own networking (Necko) and handles TLS/HTTP differently
    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()

        # ── Strategy 1: Intercept API responses ────────────────────────────
        def handle_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            keywords = ["fedwatch", "probability", "forecast", "quikstrike",
                        "fedWatch", "ratepath", "fomc"]
            if any(k.lower() in url.lower() for k in keywords) and "json" in ct:
                try:
                    data = response.json()
                    captured_apis.append({"url": url, "data": data})
                    print(f"  [API] Captured JSON from: {url}")
                except Exception:
                    pass

        page.on("response", handle_response)

        # ── Retry loop ─────────────────────────────────────────────────
        for attempt in range(1, max_retries + 1):
            try:
                print(f"Navigating to {CME_FEDWATCH_URL} ... (attempt {attempt}/{max_retries})")
                page.goto(CME_FEDWATCH_URL, wait_until="domcontentloaded", timeout=120000)
                print("  Page loaded, waiting for content...")

                # Extra wait for JS rendering
                page.wait_for_timeout(5000)
                last_error = None
                break

            except Exception as e:
                err_msg = str(e).lower()
                last_error = e
                if attempt < max_retries:
                    wait_sec = attempt * 5
                    print(f"  Attempt failed ({type(e).__name__}: {e})")
                    print(f"  Retrying in {wait_sec}s...")
                    time.sleep(wait_sec)
                else:
                    raise

        if last_error:
            raise last_error

        # Wait for the probability table to render
        # The CME FedWatch page typically embeds a QuikStrike iframe
        print("Waiting for data to render...")

        # Try to find and switch to iframe (QuikStrike widget)
        frame = None
        iframe_selectors = [
            "iframe[src*='quikstrike']",
            "iframe[src*='fedwatch']",
            "iframe[src*='fed-watch']",
            "iframe[title*='FedWatch']",
            "iframe[id*='fedwatch']",
            "iframe[name*='fedwatch']",
        ]

        for sel in iframe_selectors:
            try:
                iframe_el = page.query_selector(sel)
                if iframe_el:
                    frame = iframe_el.content_frame()
                    print(f"  Found iframe: {sel}")
                    break
            except Exception:
                continue

        # If no iframe, use main page
        if frame is None:
            frame = page
            print("  No iframe found, using main page")

        # Wait for table-like content to appear
        table_selectors = [
            "table",
            "[class*='probability']",
            "[class*='fedwatch']",
            "[data-role='probability']",
            ".rates-table",
            ".probability-table",
        ]

        table_found = False
        for sel in table_selectors:
            try:
                frame.wait_for_selector(sel, timeout=15000)
                print(f"  Found element: {sel}")
                table_found = True
                break
            except Exception:
                continue

        if not table_found:
            print("  WARNING: No table element found. Will try to parse page text.")

        # Give it a moment for any final JS rendering
        page.wait_for_timeout(3000)

        # ── Strategy 2: Scrape the rendered table ──────────────────────────
        meetings_data = extract_table_data(frame)

        # Extract current target and EFFR
        current_target = extract_current_target(page)
        if not current_target:
            # Try in frame
            current_target = extract_current_target(frame)

        effr = extract_effr(page)
        if effr is None:
            effr = extract_effr(frame)

        # Capture raw HTML for debugging
        raw_html = ""
        try:
            raw_html = frame.inner_html("body")[:5000]
        except Exception:
            raw_html = ""

        browser.close()

    return {
        "meetings": meetings_data,
        "current_target": current_target,
        "effr": effr,
        "captured_apis": captured_apis,
        "raw_html": raw_html,
    }


def extract_table_data(frame):
    """
    Extract probability data from the rendered table.

    Handles two table orientations:
      A) Rows = meetings, Columns = rate ranges  (most common)
      B) Rows = rate ranges, Columns = meetings

    Returns: list of {date: str, probabilities: {range: pct}}
    """
    meetings = []

    # Try to find all tables
    tables = frame.query_selector_all("table")
    print(f"  Found {len(tables)} table(s) on page")

    for table_idx, table in enumerate(tables):
        try:
            rows = table.query_selector_all("tr")
            if len(rows) < 2:
                continue

            print(f"  Table {table_idx}: {len(rows)} rows")

            # Get header row
            header_cells = rows[0].query_selector_all("th, td")
            headers = [c.inner_text().strip() for c in header_cells]
            print(f"  Headers: {headers[:8]}...")

            # Determine orientation by checking if headers look like rate ranges
            rate_range_pattern = re.compile(r"\d+\.\d+\s*%?\s*[-–]\s*\d+\.\d+\s*%")
            date_pattern = re.compile(
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                r"[\s\-]*\d{1,2}", re.IGNORECASE
            )

            header_is_rates = sum(1 for h in headers if rate_range_pattern.search(h))
            header_is_dates = sum(1 for h in headers if date_pattern.search(h))

            if header_is_rates > header_is_dates:
                # Orientation A: columns = rate ranges, rows = meetings
                meetings = parse_orientation_a(rows, headers, rate_range_pattern)
            else:
                # Orientation B: columns = meetings, rows = rate ranges
                meetings = parse_orientation_b(rows, headers, date_pattern)

            if meetings:
                print(f"  Extracted {len(meetings)} meetings from table {table_idx}")
                return meetings

        except Exception as e:
            print(f"  Error parsing table {table_idx}: {e}")
            continue

    # If no table parsed, try div-based layout
    if not meetings:
        meetings = extract_div_based(frame)

    return meetings


def parse_orientation_a(rows, headers, rate_range_pattern):
    """
    Parse table where:
      - Each row = one meeting (date in first column)
      - Each column = one rate range
      - Cells = probability %
    """
    meetings = []
    rate_cols = []  # (col_index, normalized_range)

    for i, h in enumerate(headers):
        rng = parse_rate_range(h)
        if rate_range_pattern.search(h) or "%" in h:
            rate_cols.append((i, rng))

    for row in rows[1:]:
        cells = row.query_selector_all("td")
        if len(cells) < 2:
            continue

        # First cell = meeting date
        date_text = cells[0].inner_text().strip()
        if not date_text:
            continue

        meeting_date = parse_meeting_date(date_text)
        probs = {}

        for col_idx, rng in rate_cols:
            if col_idx < len(cells):
                val_text = cells[col_idx].inner_text().strip()
                if val_text and val_text != "—" and val_text != "-":
                    pct = parse_percentage(val_text)
                    if pct > 0:
                        probs[rng] = pct

        if probs:
            meetings.append({
                "date": meeting_date,
                "raw_date": date_text,
                "probabilities": probs,
            })

    return meetings


def parse_orientation_b(rows, headers, date_pattern):
    """
    Parse table where:
      - Each row = one rate range (first column)
      - Each column = one meeting date
      - Cells = probability %
    """
    meetings = []
    meeting_cols = []  # (col_index, parsed_date)

    for i, h in enumerate(headers):
        if date_pattern.search(h):
            meeting_cols.append((i, parse_meeting_date(h)))

    for row in rows[1:]:
        cells = row.query_selector_all("td")
        if len(cells) < 2:
            continue

        # First cell = rate range
        range_text = cells[0].inner_text().strip()
        rng = parse_rate_range(range_text)
        if not rng:
            continue

        for col_idx, meeting_date in meeting_cols:
            if col_idx < len(cells):
                val_text = cells[col_idx].inner_text().strip()
                if val_text and val_text != "—" and val_text != "-":
                    pct = parse_percentage(val_text)
                    if pct > 0:
                        # Find or create meeting entry
                        found = False
                        for m in meetings:
                            if m["date"] == meeting_date:
                                m["probabilities"][rng] = pct
                                found = True
                                break
                        if not found:
                            meetings.append({
                                "date": meeting_date,
                                "raw_date": headers[col_idx],
                                "probabilities": {rng: pct},
                            })

    return meetings


def extract_div_based(frame):
    """
    Extract probability data from text-based rendering.
    CME FedWatch page renders probabilities as labeled text sections,
    not standard HTML tables. Parse the full page text to extract data.
    """
    meetings = []
    try:
        all_text = frame.inner_text("body")
        print(f"  Page text length: {len(all_text)} chars")
        print(f"  First 800 chars:\n{all_text[:800]}")
        print(f"  ...")

        # ── Strategy: parse meeting dates and their probability rows ──
        # Pattern: dates like "29 Jul26" or "16 Sep 2026" followed by prob lines
        lines = all_text.split("\n")

        current_meeting = None
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            # Detect meeting date lines: "29 Jul26", "16 Sep26", "28 Oct26", etc.
            date_match = re.match(
                r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{2})?\s*$",
                line,
                re.IGNORECASE,
            )
            if date_match:
                day = int(date_match.group(1))
                month_str = date_match.group(2)
                year = date_match.group(3) if date_match.group(3) else "26"
                year_full = f"20{year}" if len(year) == 2 else year

                # Build date string
                try:
                    dt = datetime.strptime(f"{month_str} {day} {year_full}", "%b %d %Y")
                    meeting_date = dt.strftime("%Y-%m-%d")
                    current_meeting = {"date": meeting_date, "raw_date": line}
                except ValueError:
                    continue
                continue

            # Detect probability header line: "EASE    NO CHANGE    HIKE"
            if re.match(r"EASE\s+NO\s*CHANGE\s+HIKE", line, re.IGNORECASE):
                # Next line should be percentages like "0.0 %   65.8 %   34.2 %"
                if i + 1 < len(lines) and current_meeting:
                    pct_line = lines[i + 1].strip()
                    probs = parse_probability_line(pct_line)
                    if probs:
                        current_meeting["probabilities"] = probs
                        meetings.append(current_meeting)
                        top_range = max(probs, key=probs.get)
                        print(f"  {current_meeting['date']}: most likely {top_range} at {probs[top_range]:.1f}%")
                    current_meeting = None

    except Exception as e:
        print(f"  Error in div-based extraction: {e}")

    return meetings


def parse_probability_line(line):
    """Parse '0.0 %   65.8 %   34.2 %' into rate-range dict."""
    # Find all percentage values in order: EASE, NO CHANGE, HIKE
    pcts = re.findall(r"(\d+\.?\d*)\s*%", line)

    if len(pcts) >= 3:
        ease_pct = float(pcts[0])
        no_change_pct = float(pcts[1])
        hike_pct = float(pcts[2])

        probs = {}

        # Map EASE/NO CHANGE/HIKE to rate ranges based on current target
        # Current target is ~3.50-3.75%. We'll label generically:
        # EASE = lower range, NO CHANGE = current, HIKE = higher range
        # But we need the actual ranges from the page context...
        # Use generic labels that will be refined later
        if ease_pct > 0:
            probs["EASE"] = ease_pct
        if no_change_pct > 0:
            probs["NO CHANGE"] = no_change_pct
        if hike_pct > 0:
            probs["HIKE"] = hike_pct

        return probs

    # Also try format: single values like "65.8%" on separate lines
    if len(pcts) == 1:
        return {"probability": float(pcts[0])}

    return {}


# ── Data persistence ────────────────────────────────────────────────────────

def save_daily_snapshot(data, snapshot_date):
    """Save complete daily snapshot as JSON."""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    filepath = DAILY_DIR / f"{snapshot_date}.json"

    snapshot = {
        "date": snapshot_date,
        "current_target": data.get("current_target", ""),
        "effr": data.get("effr"),
        "meetings": data.get("meetings", []),
        "captured_api_count": len(data.get("captured_apis", [])),
    }

    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    print(f"  Saved daily snapshot: {filepath}")


def append_to_csv(data, snapshot_date):
    """Append data to the long-format history CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create CSV with header if it doesn't exist
    if not HISTORY_CSV.exists():
        with open(HISTORY_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)

    current_target = data.get("current_target", "")
    effr = data.get("effr", "")
    meetings = data.get("meetings", [])

    rows_written = 0
    with open(HISTORY_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        for m in meetings:
            meeting_date = m.get("date", "")
            probs = m.get("probabilities", {})

            if not probs:
                continue

            # Find most likely rate
            most_likely = max(probs, key=probs.get) if probs else ""

            for rate_range, prob in probs.items():
                writer.writerow([
                    snapshot_date,
                    meeting_date,
                    rate_range,
                    prob,
                    rate_range == most_likely,
                    current_target,
                    effr,
                    "cme_official",
                ])
                rows_written += 1

            # Log summary
            top_range = max(probs, key=probs.get)
            print(f"  {meeting_date}: most likely {top_range} at {probs[top_range]:.1f}%")

    print(f"  Appended {rows_written} rows to {HISTORY_CSV}")


def save_debug_info(data, snapshot_date):
    """Save captured API responses and raw HTML for debugging."""
    debug_dir = DATA_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    # Save captured API responses
    if data.get("captured_apis"):
        api_path = debug_dir / f"{snapshot_date}_apis.json"
        with open(api_path, "w") as f:
            json.dump(data["captured_apis"], f, indent=2, default=str)
        print(f"  Saved API debug info: {api_path}")

    # Save raw HTML snippet
    if data.get("raw_html"):
        html_path = debug_dir / f"{snapshot_date}_html.txt"
        with open(html_path, "w") as f:
            f.write(data["raw_html"])
        print(f"  Saved HTML debug info: {html_path}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    snapshot_date = now.strftime("%Y-%m-%d")

    print(f"[{now.isoformat()}] Starting CME FedWatch data scrape...")
    print(f"  Snapshot date: {snapshot_date}")

    # Scrape (with retries)
    data = scrape_cme_fedwatch(max_retries=3)

    if not data["meetings"]:
        print("\nERROR: No meeting data extracted!")
        print("Debug info saved for inspection.")
        save_debug_info(data, snapshot_date)
        sys.exit(1)

    print(f"\nExtracted {len(data['meetings'])} meetings")
    print(f"Current target: {data.get('current_target', 'N/A')}")
    print(f"EFFR: {data.get('effr', 'N/A')}")
    print(f"Captured APIs: {len(data.get('captured_apis', []))}")

    # Save
    print("\nSaving data...")
    save_daily_snapshot(data, snapshot_date)
    append_to_csv(data, snapshot_date)
    save_debug_info(data, snapshot_date)

    print(f"\nDone! Data for {snapshot_date} saved successfully.")


if __name__ == "__main__":
    main()
