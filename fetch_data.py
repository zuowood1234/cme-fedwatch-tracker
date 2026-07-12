#!/usr/bin/env python3
"""
CME FedWatch Data Scraper v2

Scrapes OFFICIAL probability data directly from the CME FedWatch tool page.
Uses Playwright (headless Firefox) because the page renders via ASP.NET QuikStrike widget.

KEY INSIGHT: The CME page is TAB-BASED — it shows all meeting dates but only displays
probability data for the currently SELECTED meeting. We must click each meeting tab
sequentially to extract all meetings' data.

Data output:
  - data/daily/YYYY-MM-DD.json   (full snapshot)
  - data/fedwatch_history.csv     (long-format for dashboard)
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
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install firefox")
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

def parse_percentage(text):
    """Parse '87.5%' or '87.5' or '<0.1%' into float."""
    text = text.strip().replace("%", "").replace("<", "").replace("≈", "").replace(">","")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_meeting_date(text):
    """Parse various date formats into YYYY-MM-DD."""
    text = text.strip()
    # Try common formats
    formats = [
        "%b %d, %Y", "%b %d %Y",       # Jul 29, 2026
        "%d-%b-%y", "%d-%b-%Y",        # 29-Jul-26
        "%B %d, %Y", "%B %d %Y",       # July 29, 2026
        "%m/%d/%Y",                     # 07/29/2026
        "%Y-%m-%d",                     # 2026-07-29
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Fallback: match "29 Jul26" or "29 Jul 26" pattern
    m = re.match(r"(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{2,4})?", text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        month_str = m.group(2)
        year_raw = m.group(3)
        year = int(year_raw) if year_raw and int(year_raw) > 99 else 2026
        try:
            dt = datetime.strptime(f"{month_str} {day} {year}", "%b %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return text


def normalize_rate_range(text):
    """Normalize rate range like '3.50-3.75%' or '3.50% - 3.75%' into consistent format."""
    text = text.strip()
    m = re.search(r"(\d+\.\d+)\s*%?\s*[-–—\s]+\s*(\d+\.\d+)\s*%", text)
    if m:
        return f"{float(m.group(1)):.2f}%-{float(m.group(2)):.2f}%"
    return text.strip()


# ── Core scraper ────────────────────────────────────────────────────────────

def scrape_cme_fedwatch(max_retries=3):
    """
    Main scraper function.
    Clicks through each meeting tab and extracts probability data.
    """
    captured_apis = []
    last_error = None

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
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

        # Capture API responses for debugging
        def handle_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            if ("json" in ct or "ajax" in url.lower()) and len(captured_apis) < 20:
                try:
                    cl = int(response.headers.get("content-length", 0))
                    if cl < 500000:
                        if "json" in ct:
                            data = response.json()
                        else:
                            data = "(non-json)"
                        captured_apis.append({"url": url[:200], "data": str(data)[:500]})
                        if any(k in url.lower() for k in ["fedwatch", "probability", "ratepath"]):
                            print(f"  [API] Captured: {url[:100]}...")
                except Exception:
                    pass

        page.on("response", handle_response)

        # ── Navigate with retry ─────────────────────────────────────────
        for attempt in range(1, max_retries + 1):
            try:
                print(f"[attempt {attempt}/{max_retries}] Navigating to CME FedWatch...")
                page.goto(CME_FEDWATCH_URL, wait_until="domcontentloaded", timeout=120000)
                print("  Page loaded. Waiting for JS rendering...")
                page.wait_for_timeout(15000)  # QuikStrike can be very slow
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    print(f"  Failed: {e}. Retrying in {attempt * 5}s...")
                    time.sleep(attempt * 5)
                else:
                    raise

        if last_error:
            raise last_error

        # ── Step 1: Discover the page structure ─────────────────────────
        print("\n=== Analyzing page structure ===")

        # Get full page text for initial analysis
        body_text = page.inner_text("body")
        print(f"  Body text length: {len(body_text)} chars")

        # Save full HTML for debug
        debug_dir = DATA_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            raw_html = page.content()
            with open(debug_dir / f"{snapshot_date}_full_page.html", "w") as f:
                f.write(raw_html)
            print(f"  Saved full HTML: {len(raw_html)} chars")
        except Exception as e:
            print(f"  Could not save HTML: {e}")

        # ── Step 2: Find meeting tabs ────────────────────────────────────
        print("\n=== Finding meeting tabs ===")

        meeting_tabs = find_meeting_tabs(page)
        print(f"  Found {len(meeting_tabs)} meeting tabs")

        if not meeting_tabs:
            print("  WARNING: No tabs found. Trying fallback extraction...")
            meetings_data = fallback_extraction(page)
            browser.close()
            return {
                "meetings": meetings_data,
                "current_target": "",
                "effr": None,
                "captured_apis": captured_apis,
                "raw_html": body_text[:5000],
            }

        # ── Step 3: Click each tab and extract data ─────────────────────
        print("\n=== Extracting data from each meeting ===")
        meetings_data = []

        for idx, tab_info in enumerate(meeting_tabs):
            tab_el = tab_info["element"]
            tab_text = tab_info["text"]
            meeting_date = tab_info.get("date", "")

            print(f"\n  --- Tab {idx+1}/{len(meeting_tabs)}: '{tab_text}' ---")

            try:
                # Click the tab
                tab_el.click()
                page.wait_for_timeout(3000)  # Wait for ASP.NET postback + render

                # Extract probability data for this meeting
                meeting_data = extract_meeting_probabilities(page, tab_text, meeting_date)
                if meeting_data:
                    meetings_data.append(meeting_data)
                    top = max(meeting_data["probabilities"].items(), key=lambda x: x[1])
                    print(f"  -> {meeting_data['date']}: most likely {top[0]}={top[1]:.1f}%")
                else:
                    print(f"  -> No data extracted for '{tab_text}'")

            except Exception as e:
                print(f"  Error clicking tab '{tab_text}': {e}")
                continue

        # ── Step 4: Extract global info ─────────────────────────────────
        current_target = extract_current_target_from_page(page)
        effr = extract_effr_from_page(page)

        browser.close()

    return {
        "meetings": meetings_data,
        "current_target": current_target,
        "effr": effr,
        "captured_apis": captured_apis,
        "raw_html": body_text[:5000] if 'body_text' in dir() else "",
    }


def find_meeting_tabs(page):
    """
    Find all meeting date tab/button elements on the CME FedWatch page.
    Returns list of {"element": ElementHandle, "text": str, "date": str}
    """
    tabs = []

    # Strategy 1: Look for elements containing dates in common FOMC meeting patterns
    # CME typically shows dates like "29 Jul26", "16 Sep26", etc.
    date_pattern = re.compile(
        r"^(\d{1,2})[\s\-]*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\s\-]*\d{2,4}?\s*$",
        re.IGNORECASE,
    )

    # Look for clickable date elements in various containers
    selectors_to_try = [
        # Tab-like elements
        "[role='tab']",
        "[role='button']",
        ".tab",
        ".tab-item",
        ".nav-tab",
        ".meeting-date",
        "[class*='tab'] [class*='date']",
        "[class*='trigger']",
        "[class*='Picker'] span",
        "[id*='Trigger']",
        "[class*='PopUpTabs'] *",
        "#ucPopUpTabs *",
        # Date spans/divs that are clickable
        "span[class*='date']",
        "div[class*='date']:not(div[class*='container']):not(div[class*='wrapper'])",
        # Generic clickable elements with date text
        "a[href*='#']",
        "[onclick]",
        "[data-date]",
    ]

    seen_texts = set()

    for selector in selectors_to_try:
        try:
            elements = page.query_selector_all(selector)
            for el in elements:
                try:
                    text = el.inner_text().strip()
                    if not text or text in seen_texts:
                        continue
                    if date_pattern.match(text):
                        parsed_date = parse_meeting_date(text)
                        tabs.append({
                            "element": el,
                            "text": text,
                            "date": parsed_date,
                        })
                        seen_texts.add(text)
                        print(f"    Found tab [{selector}]: '{text}' -> {parsed_date}")
                except Exception:
                    continue
        except Exception:
            continue

    # Strategy 2: If no tabs found with selectors, search by text content
    if not tabs:
        print("    Selector search failed. Trying text-based search...")
        try:
            # Find all elements that look like meeting dates
            all_spans = page.query_selector_all("span, div, a, td, th, li, button")
            for el in all_spans:
                try:
                    text = el.inner_text().strip()
                    if not text or text in seen_texts:
                        continue
                    if len(text) > 30:  # Skip long text blocks
                        continue
                    if date_pattern.match(text):
                        # Check if it's clickable or visible
                        is_visible = el.is_visible()
                        if is_visible:
                            parsed_date = parse_meeting_date(text)
                            tabs.append({
                                "element": el,
                                "text": text,
                                "date": parsed_date,
                            })
                            seen_texts.add(text)
                            print(f"    Found by text: '{text}' -> {parsed_date}")
                except Exception:
                    continue
        except Exception as e:
            print(f"    Text search error: {e}")

    return tabs


def extract_meeting_probabilities(page, tab_label, meeting_date):
    """
    After clicking a meeting tab, extract the probability data shown.
    Returns dict with "date", "probabilities", "raw_label" keys.
    """
    result = {
        "date": meeting_date,
        "raw_date": tab_label,
        "probabilities": {},
    }

    try:
        body_text = page.inner_text("body")

        # ── Method A: Look for probability table/grid near the selected tab ──
        # The CME page shows probabilities in a format like:
        # PROBABILITIES
        # EASE    NO CHANGE    HIKE
        # 0.0%    65.8%        34.2%
        # And also shows the actual rate ranges somewhere nearby

        # Find the probability section
        lines = body_text.split("\n")

        prob_found = False
        ease_header_found = False
        rate_ranges_found = []  # Track any rate ranges found on page

        for i, line in enumerate(lines):
            line_stripped = line.strip()

            # Detect EASE / NO CHANGE / HIKE header
            if re.match(r"EASE\s+NO?\s*CHANGE\s+HIKE", line_stripped, re.IGNORECASE):
                ease_header_found = True
                prob_found = True
                continue

            # Detect PROBABILITIES label
            if re.match(r"PROBABILITIES", line_stripped, re.IGNORECASE):
                prob_found = True
                continue

            # After header, find percentage values
            if ease_header_found and re.search(r"\d+\.*\d*\s*%", line_stripped):
                pcts = re.findall(r"(\d+\.?\d*)\s*%", line_stripped)
                if len(pcts) >= 3:
                    result["probabilities"] = {}
                    # We need to map these to actual rate ranges
                    # First get the rate ranges from context
                    rate_ranges = find_rate_ranges_on_page(page)
                    if len(rate_ranges) >= 3:
                        labels = ["EASE_range", "NO CHANGE_range", "HIKE_range"]
                        for j, pct in enumerate(pcts[:3]):
                            if float(pct) > 0 or j == 1:  # Always include NO_CHANGE
                                rng = rate_ranges[j] if j < len(rate_ranges) else labels[j]
                                result["probabilities"][rng] = float(pct)
                    else:
                        # Fallback: use generic labels with actual values
                        if float(pcts[0]) > 0:
                            result["probabilities"]["EASE"] = float(pcts[0])
                        result["probabilities"]["NO CHANGE"] = float(pcts[1])
                        if float(pcts[2]) > 0:
                            result["probabilities"]["HIKE"] = float(pcts[2])
                    break

        # ── Method B: If Method A failed, try parsing rate ranges directly ──
        if not result["probabilities"]:
            # Look for rows with rate range + probability pairs
            rate_probs = extract_rate_probability_pairs(page)
            if rate_probs:
                result["probabilities"] = rate_probs

        # ── Method C: Try to find detailed probability grid/table ──
        if not result["probabilities"]:
            rate_probs = extract_from_table_elements(page)
            if rate_probs:
                result["probabilities"] = rate_probs

    except Exception as e:
        print(f"    Extraction error: {e}")

    return result if result["probabilities"] else None


def find_rate_ranges_on_page(page):
    """
    Find the actual rate range labels displayed on the page.
    These should be near the probability percentages, like:
    '3.25%-3.50%', '3.50%-3.75%', '3.75%-4.00%'
    """
    ranges = []
    try:
        body_text = page.inner_text("body")

        # Find all rate range patterns
        rate_matches = re.findall(
            r"(\d+\.\d+)\s*%?\s*[-–—]\s*(\d+\.\d+)\s*%",
            body_text,
        )

        seen = set()
        for low, high in rate_matches:
            low_f, high_f = float(low), float(high)
            # Filter: valid fed funds rate ranges (typically 0-10%)
            if 0 <= low_f <= 10 and 0 <= high_f <= 10 and high_f > low_f:
                rng_key = f"{low_f:.2f}%-{high_f:.2f}%"
                if rng_key not in seen:
                    ranges.append(rng_key)
                    seen.add(rng_key)

        # Sort by rate value
        ranges.sort(key=lambda r: float(r.split("-")[0].replace("%", "")))

        if ranges:
            print(f"    Rate ranges found: {ranges}")

    except Exception as e:
        print(f"    Error finding rate ranges: {e}")

    return ranges


def extract_rate_probability_pairs(page):
    """
    Try to extract rate -> probability mappings from page structure.
    Looks for patterns like '3.50%-3.75%  65.8%'
    """
    probs = {}
    try:
        # Check for table rows where first cell = rate range, second cell = probability
        tables = page.query_selector_all("table")
        for table in tables:
            rows = table.query_selector_all("tr")
            for row in rows:
                cells = row.query_selector_all("td, th")
                if len(cells) >= 2:
                    first_text = cells[0].inner_text().strip()
                    second_text = cells[1].inner_text().strip()

                    rng = normalize_rate_range(first_text)
                    if rng and "%" in first_text:  # It looks like a rate range
                        pct = parse_percentage(second_text)
                        if pct > 0:
                            probs[rng] = pct
    except Exception:
        pass

    return probs


def extract_from_table_elements(page):
    """
    Extract probabilities from any table element that contains rate data.
    More aggressive table parsing.
    """
    probs = {}
    try:
        tables = page.query_selector_all("table")
        for table in tables:
            html = table.inner_html()
            text = table.inner_text()

            # Must contain both rate-looking content and percentages
            if not re.search(r"\d+\.\d+", text) or not re.search(r"\d+\s*%", text):
                continue

            rows = table.query_selector_all("tr")
            for row in rows:
                cells = row.query_selector_all("td, th")
                cell_texts = [c.inner_text().strip() for c in cells]

                for i, ct in enumerate(cell_texts):
                    # Is this cell a rate range?
                    if re.match(r".*\d+\.\d+\s*%?\s*[-–—].*\d+\.\d+\s*%.*", ct):
                        rng = normalize_rate_range(ct)
                        # Next cell(s) might be probability
                        for j in range(i + 1, min(i + 3, len(cell_texts))):
                            pct = parse_percentage(cell_texts[j])
                            if pct > 0 and pct <= 100:
                                probs[rng] = pct
                                break
    except Exception:
        pass

    return probs


def extract_current_target_from_page(page):
    """Try to find current target rate from page text."""
    try:
        text = page.inner_text("body")
        m = re.search(
            r"(?:Current\s+(?:Rate|Target)|Target\s+Rate).*?(\d+\.\d+)\s*%?\s*[-–—]\s*(\d+\.\d+)\s*%",
            text, re.IGNORECASE,
        )
        if m:
            return f"{float(m.group(1)):.2f}%-{float(m.group(2)):.2f}%"
    except Exception:
        pass
    return ""


def extract_effr_from_page(page):
    """Try to find EFFR from page."""
    try:
        text = page.inner_text("body")
        m = re.search(r"(?:EFFR|Effective Federal Funds Rate).*?(\d+\.\d+)\s*%", text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def fallback_extraction(page):
    """
    Fallback: try to extract whatever data we can without clicking tabs.
    Used when no tabs are found.
    """
    meetings = []
    try:
        body_text = page.inner_text("body")
        lines = body_text.split("\n")

        # Try to find all date + probability groupings
        current_date = None
        for line in lines:
            line = line.strip()
            date_match = re.match(
                r"^(\d{1,2})[\s\-]*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\s\-]*(\d{2,4})?",
                line, re.IGNORECASE,
            )
            if date_match:
                current_date = parse_meeting_date(line)
                continue

            # Probability line after a date
            if current_date and re.search(r"\d+\.*\d*\s*%", line):
                pcts = re.findall(r"(\d+\.?\d*)\s*%", line)
                if len(pcts) >= 2:
                    probs = {}
                    rate_ranges = find_rate_ranges_on_page(page)
                    for i, p in enumerate(pcts[:3]):
                        label = rate_ranges[i] if i < len(rate_ranges) else f"option_{i}"
                        probs[label] = float(p)
                    meetings.append({
                        "date": current_date,
                        "raw_date": line,
                        "probabilities": probs,
                    })
                    current_date = None

    except Exception as e:
        print(f"Fallback extraction error: {e}")

    return meetings


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
    print(f"Saved daily snapshot: {filepath}")


def append_to_csv(data, snapshot_date):
    """Append today's data to history CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

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

            most_likely = max(probs, key=probs.get) if probs else ""

            for rate_range, prob in probs.items():
                writer.writerow([
                    snapshot_date, meeting_date, rate_range, prob,
                    rate_range == most_likely, current_target, effr, "cme_official",
                ])
                rows_written += 1

            top_range = max(probs, key=probs.get)
            print(f"  CSV: {meeting_date} | most likely {top_range}={probs[top_range]:.1f}%")

    print(f"Appended {rows_written} rows to {HISTORY_CSV}")


def save_debug_info(data, snapshot_date):
    """Save debug artifacts."""
    debug_dir = DATA_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    if data.get("captured_apis"):
        api_path = debug_dir / f"{snapshot_date}_apis.json"
        with open(api_path, "w") as f:
            json.dump(data["captured_apis"], f, indent=2, default=str)
        print(f"Saved APIs: {api_path}")

    if data.get("raw_html"):
        html_path = debug_dir / f"{snapshot_date}_html.txt"
        with open(html_path, "w") as f:
            f.write(data["raw_html"])
        print(f"Saved HTML snippet: {html_path}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    snapshot_date = now.strftime("%Y-%m-%d")

    print(f"[{now.isoformat()}] CME FedWatch Scraper v2")
    print(f"  Snapshot: {snapshot_date}")

    data = scrape_cme_fedwatch(max_retries=3)

    if not data["meetings"]:
        print("\nERROR: No meeting data extracted!")
        save_debug_info(data, snapshot_date)
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"Extracted {len(data['meetings'])} meetings:")
    for m in data["meetings"]:
        probs = m.get("probabilities", {})
        top = max(probs.items(), key=lambda x: x[1]) if probs else ("N/A", 0)
        print(f"  {m['date']}: {len(probs)} rate options, top={top[0]} ({top[1]:.1f}%)")
    print(f"Current target: {data.get('current_target', 'N/A')}")
    print(f"EFFR: {data.get('effr', 'N/A')}")

    print("\nSaving data...")
    save_daily_snapshot(data, snapshot_date)
    append_to_csv(data, snapshot_date)
    save_debug_info(data, snapshot_date)
    print(f"\nDone! Data saved for {snapshot_date}")


if __name__ == "__main__":
    main()
