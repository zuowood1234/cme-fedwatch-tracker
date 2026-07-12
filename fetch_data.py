#!/usr/bin/env python3
"""
CME FedWatch Data Scraper v3

Strategy: Use Playwright to load the CME FedWatch page, wait for full JS rendering,
then extract probability data by clicking through each meeting tab.

Key improvements over v2:
- Wait for network idle (not just fixed timeout)
- Handle QuikStrike iframe properly
- More robust element detection
- Better error handling for headless environments
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
    print("ERROR: playwright not installed.")
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


def parse_pct(text):
    """Parse percentage string to float."""
    try:
        return float(text.strip().replace("%","").replace("<","").replace(">","").replace("≈",""))
    except ValueError:
        return 0.0


def parse_meeting_date(text):
    """Parse meeting date string to YYYY-MM-DD."""
    text = text.strip()
    formats = ["%b %d, %Y", "%b %d %Y", "%d-%b-%y", "%d-%b-%Y",
               "%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%Y-%m-%d"]
    for f in formats:
        try:
            return datetime.strptime(text, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{2,4})?", text, re.IGNORECASE)
    if m:
        day, mon, yr = int(m.group(1)), m.group(2), m.group(3)
        yr = int(yr) if yr and int(yr) > 99 else 2026
        try:
            return datetime.strptime(f"{mon} {day} {yr}", "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return text


def norm_rate_range(text):
    """Normalize '3.50-3.75%' -> '3.50%-3.75%'."""
    m = re.search(r"(\d+\.\d+)\s*%?\s*[-–—]\s*(\d+\.\d+)\s*%", text)
    if m:
        return f"{float(m.group(1)):.2f}%-{float(m.group(2)):.2f}%"
    return text.strip()


# ── Main scraper ────────────────────────────────────────────────────────────

def scrape_cme_fedwatch(max_retries=3):
    captured_apis = []
    last_error = None

    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=["--width=1920", "--height=1080"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()

        # Capture interesting API responses
        def on_response(resp):
            url = resp.url
            try:
                ct = resp.headers.get("content-type","")
                if len(captured_apis) < 30:
                    info = {"url": url[:200]}
                    if "json" in ct:
                        try: info["data"] = str(resp.json())[:500]
                        except: pass
                    captured_apis.append(info)
            except: pass
        page.on("response", on_response)

        # ── Navigate ─────────────────────────────────────────────────────
        for attempt in range(1, max_retries + 1):
            try:
                print(f"[attempt {attempt}/{max_retries}] Loading CME FedWatch...")
                # Use networkidle for proper JS rendering
                page.goto(CME_FEDWATCH_URL, wait_until="networkidle", timeout=180000)
                print("  Page loaded (networkidle). Waiting extra for QuikStrike...")
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    print(f"  Failed: {e}. Retrying in {attempt*5}s...")
                    time.sleep(attempt * 5)
                else:
                    raise

        # ── Wait for content to fully render ─────────────────────────────
        # QuikStrike can take 20-40 seconds to fully render on slow connections
        print("  Waiting for QuikStrike widget to render...")

        # Strategy: poll for content appearing
        max_wait = 60  # seconds
        rendered = False
        for w in range(max_wait):
            time.sleep(2)  # Check every 2 seconds
            elapsed = (w + 1) * 2
            body_text = ""
            try:
                body_text = page.inner_text("body")
            except:
                continue

            # Check if we have meaningful content (>5000 chars suggests rendering happened)
            # Also look for key indicators
            has_dates = bool(re.search(r"\d{1,2}\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", body_text, re.I))
            has_probs = bool(re.search(r"EASE|NO\s*CHANGE|HIKE|\d+\.\d+\s*%", body_text, re.I))
            text_len = len(body_text)

            if elapsed % 10 == 0 or (has_dates and has_probs):
                print(f"  [{elapsed}s] text_len={text_len}, dates={has_dates}, probs={has_probs}")

            if has_probs and text_len > 5000:
                rendered = True
                print(f"  Content detected at {elapsed}s!")
                break

        if not rendered:
            print(f"  WARNING: Content may not have fully rendered after {max_wait}s")
            # Try anyway - sometimes partial content is useful
            try:
                body_text = page.inner_text("body")
                print(f"  Final text length: {len(body_text)} chars")
            except:
                body_text = ""

        # Extra wait for any animations
        page.wait_for_timeout(3000)

        # ── Save debug HTML ─────────────────────────────────────────────
        debug_dir = DATA_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            full_html = page.content()
            with open(debug_dir / f"{snapshot_date}_full_page.html", "w") as f:
                f.write(full_html)
            print(f"  Saved HTML: {len(full_html)} chars")
        except Exception as e:
            print(f"  Could not save HTML: {e}")

        # ── Extract data ────────────────────────────────────────────────
        print("\n=== Extracting Data ===")

        # Try multiple extraction strategies
        meetings_data = []

        # Strategy 1: Look for iframe with QuikStrike content
        frame_target = None
        for sel in ["iframe[src*='quikstrike']", "iframe[src*='QuikStrike']", "iframe"]:
            try:
                iframe_el = page.query_selector(sel)
                if iframe_el:
                    frame = iframe_el.content_frame()
                    if frame:
                        frame_text = frame.inner_text("body")
                        print(f"  Found iframe ({sel}): {len(frame_text)} chars")
                        if len(frame_text) > 1000:
                            frame_target = frame
                            break
            except:
                continue

        target = frame_target if frame_target else page

        # Strategy 2: Click-based extraction
        meetings_data = click_and_extract(target, page)

        # Strategy 3: If click failed, try direct parsing
        if not meetings_data:
            print("  Click extraction failed, trying direct parsing...")
            meetings_data = direct_parse_extraction(target)

        # Get global metadata
        current_target = get_current_target(page)
        effr = get_effr(page)

        browser.close()

    return {
        "meetings": meetings_data,
        "current_target": current_target,
        "effr": effr,
        "captured_apis": captured_apis,
        "raw_html": body_text[:5000] if 'body_text' in dir() else "",
    }


def click_and_extract(frame, page):
    """
    Find meeting date tabs, click each one, extract probabilities.
    This is the primary extraction method.
    """
    meetings = []
    date_pat = re.compile(r"^(\d{1,2})[\s\-]*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\s\-]*\d{2,4}?\s*$", re.I)

    # Collect all clickable date elements
    tabs = []
    seen = set()

    # Search broadly across the page (not just inside frame)
    search_contexts = [frame]
    if frame != page:
        search_contexts.append(page)

    for ctx in search_contexts:
        # Try many selector patterns
        selectors = [
            "[role='tab']", "[role='button']",
            ".tab", ".tab-item", ".nav-tab",
            "span[class*='date']", "div[class*='date']",
            "a[href^='#']", "[onclick]", "[data-date]",
            "#ucPopUpTabs span", "#ucPopUpTabs div",
            "[class*='Trigger']", "[class*='trigger']",
            "[id*='Picker'] span",
            # Generic elements containing date-like text
            "span", "div", "a", "td", "li", "button",
        ]

        for sel in selectors:
            try:
                elems = ctx.query_selector_all(sel) if ctx == page else frame.query_selector_all(sel)
                for el in elems:
                    try:
                        txt = el.inner_text().strip()
                        if not txt or txt in seen or len(txt) > 25:
                            continue
                        if date_pat.match(txt):
                            tabs.append({"el": el, "txt": txt, "date": parse_meeting_date(txt)})
                            seen.add(txt)
                    except:
                        continue
            except:
                continue

        # Stop if we already found tabs in the first context
        if tabs:
            break

    print(f"  Found {len(tabs)} meeting tabs")

    if not tabs:
        return []

    # Click each tab and extract
    for i, tab in enumerate(tabs):
        print(f"\n  [{i+1}/{len(tabs)}] Clicking: '{tab['txt']}'")
        try:
            tab["el"].click()
            page.wait_for_timeout(4000)  # Wait for postback/render

            # Re-check which context has the data now
            best_ctx = frame
            for ctx in [frame, page]:
                try:
                    t = ctx.inner_text("body")
                    if "EASE" in t.upper() or "NO CHANGE" in t.upper():
                        best_ctx = ctx
                        break
                except:
                    continue

            # Extract probabilities from the active view
            probs = extract_probs_from_context(best_ctx, tab["txt"], tab["date"])
            if probs:
                meetings.append(probs)
                top = max(probs["probabilities"].items(), key=lambda x: x[1])
                print(f"    -> {probs['date']}: top={top[0]}={top[1]:.1f}%")
            else:
                print(f"    -> No probabilities found")

        except Exception as e:
            print(f"    Error: {e}")

    return meetings


def extract_probs_from_context(ctx, label, meeting_date):
    """Extract probability data for current active meeting."""
    result = {"date": meeting_date, "raw_date": label, "probabilities": {}}

    try:
        text = ctx.inner_text("body")
        lines = [l.strip() for l in text.split("\n")]

        # Find EASE/NO CHANGE/HIKE header then percentages
        header_idx = None
        for i, line in enumerate(lines):
            if re.match(r"EASE\s+NO?\s*CHANGE\s+HIKE", line, re.I):
                header_idx = i
                break

        if header_idx is not None:
            # Next non-empty line(s) should be percentages
            for j in range(header_idx + 1, min(header_idx + 5, len(lines))):
                if lines[j] and re.search(r"\d+.*%", lines[j]):
                    pcts = re.findall(r"(\d+\.?\d*)\s*%", lines[j])
                    if len(pcts) >= 3:
                        # Map to rate ranges
                        ranges = find_rate_ranges(ctx)
                        if len(ranges) >= 3:
                            labels = ranges[:3]
                        else:
                            labels = ["EASE (-25bp)", "NO CHANGE", "HIKE (+25bp)"]

                        for k, pct in enumerate(pcts[:3]):
                            val = float(pct)
                            if val > 0 or k == 1:  # Always include NO CHANGE
                                result["probabilities"][labels[k]] = val
                        break

        # If no header-based extraction worked, try finding rate:prob pairs
        if not result["probabilities"]:
            pairs = find_rate_prob_pairs(ctx)
            if pairs:
                result["probabilities"] = pairs

    except Exception as e:
        print(f"    Extraction error: {e}")

    return result if result["probabilities"] else None


def find_rate_ranges(ctx):
    """Find rate range labels on page like 3.25%-3.50%, 3.50%-3.75%, etc."""
    ranges = []
    try:
        text = ctx.inner_text("body")
        matches = re.findall(r"(\d+\.\d+)\s*%?\s*[-–—]\s*(\d+\.\d+)\s*%", text)
        seen = set()
        for lo, hi in matches:
            lo_f, hi_f = float(lo), float(hi)
            if 0 <= lo_f <= 10 and hi_f > lo_f:
                key = f"{lo_f:.2f}%-{hi_f:.2f}%"
                if key not in seen:
                    ranges.append(key)
                    seen.add(key)
        ranges.sort(key=lambda r: float(r.split("-")[0].replace("%","")))
    except:
        pass
    return ranges


def find_rate_prob_pairs(ctx):
    """Find rate range -> probability mappings in tables."""
    probs = {}
    try:
        tables = ctx.query_selector_all("table")
        for table in tables:
            for row in table.query_selector_all("tr"):
                cells = row.query_selector_all("td,th")
                texts = [c.inner_text().strip() for c in cells]
                for idx, t in enumerate(texts):
                    if re.match(r".*\d+\.\d+.*[-–—].*\d+\.\d+.*%.*", t):  # Looks like rate range
                        rng = norm_rate_range(t)
                        for j in range(idx+1, min(idx+4, len(texts))):
                            p = parse_pct(texts[j])
                            if 0 < p <= 100:
                                probs[rng] = p
                                break
    except:
        pass
    return probs


def direct_parse_extraction(frame):
    """Fallback: parse all visible data without clicking."""
    meetings = []
    try:
        text = frame.inner_text("body")
        lines = [l.strip() for l in text.split("\n")]
        cur_date = None
        date_pat = re.compile(r"^(\d{1,2})[\s\-]*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\s\-]*\d{2,4}?", re.I)

        for line in lines:
            if date_pat.match(line):
                cur_date = parse_meeting_date(line)
                continue
            if cur_date and re.search(r"\d+.*%", line):
                pcts = re.findall(r"(\d+\.?\d*)\s*%", line)
                if len(pcts) >= 2:
                    probs = {}
                    ranges = find_rate_ranges(frame)
                    for i, p in enumerate(pcts[:3]):
                        lbl = ranges[i] if i < len(ranges) else f"option_{i}"
                        probs[lbl] = float(p)
                    meetings.append({"date": cur_date, "raw_date": line, "probabilities": probs})
                    cur_date = None
    except Exception as e:
        print(f"Fallback error: {e}")
    return meetings


def get_current_target(page):
    """Extract current target rate from page."""
    try:
        text = page.inner_text("body")
        m = re.search(r"(?:Current\s*(?:Rate|Target)|Target\s*Rate).*?(\d+\.\d+)\s*%?[-–—]\s*(\d+\.\d+)\s*%", text, re.I)
        if m:
            return f"{float(m.group(1)):.2f}%-{float(m.group(2)):.2f}%"
    except:
        pass
    return ""


def get_effr(page):
    """Extract EFFR from page."""
    try:
        text = page.inner_text("body")
        m = re.search(r"(?:EFFR|Effective Federal Funds Rate).*?(\d+\.\d+)\s*%", text, re.I)
        if m:
            return float(m.group(1))
    except:
        pass
    return None


# ── Persistence ─────────────────────────────────────────────────────────────

def save_daily_snapshot(data, sd):
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    snap = {
        "date": sd, "current_target": data.get("current_target",""),
        "effr": data.get("effr"), "meetings": data.get("meetings",[]),
        "captured_api_count": len(data.get("captured_apis",[])),
    }
    p = DAILY_DIR / f"{sd}.json"
    with open(p,"w") as f:
        json.dump(snap, f, indent=2, default=str)
    print(f"Saved snapshot: {p}")


def append_to_csv(data, sd):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not HISTORY_CSV.exists():
        with open(HISTORY_CSV,"w",newline="") as f:
            csv.writer(f).writerow(CSV_HEADER)

    ct = data.get("current_target","")
    effr = data.get("effr","")
    n = 0
    with open(HISTORY_CSV,"a",newline="") as f:
        w = csv.writer(f)
        for m in data.get("meetings",[]):
            md = m.get("date",""); probs = m.get("probabilities",{})
            if not probs: continue
            ml = max(probs.items(), key=lambda x: x[1])[0]
            for rr, pr in probs.items():
                w.writerow([sd, md, rr, pr, rr==ml, ct, effr, "cme_official"])
                n += 1
            print(f"  CSV: {md} | top={ml}={probs[ml]:.1f}%")
    print(f"Appended {n} rows")


def save_debug(data, sd):
    debug_dir = DATA_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    if data.get("captured_apis"):
        with open(debug_dir / f"{sd}_apis.json","w") as f:
            json.dump(data["captured_apis"], f, indent=2, default=str)
        print(f"Saved debug APIs")
    if data.get("raw_html"):
        with open(debug_dir / f"{sd}_html.txt","w") as f:
            f.write(data["raw_html"])
        print(f"Saved debug HTML snippet")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    sd = now.strftime("%Y-%m-%d")
    print(f"[{now.isoformat()}] CME FedWatch Scraper v3\n  Snapshot: {sd}")

    data = scrape_cme_fedwatch(max_retries=3)

    if not data["meetings"]:
        print("\nERROR: No meeting data extracted!")
        save_debug(data, sd)
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"Extracted {len(data['meetings'])} meetings:")
    for m in data["meetings"]:
        ps = m.get("probabilities",{})
        t = max(ps.items(), key=lambda x: x[1]) if ps else ("N/A",0)
        print(f"  {m['date']}: {len(ps)} options, top={t[0]} ({t[1]:.1f}%)")
    print(f"Target: {data.get('current_target','N/A')}")
    print(f"EFFR: {data.get('effr','N/A')}")

    print("\nSaving...")
    save_daily_snapshot(data, sd)
    append_to_csv(data, sd)
    save_debug(data, sd)
    print(f"\nDone! {sd}")


if __name__ == "__main__":
    main()
