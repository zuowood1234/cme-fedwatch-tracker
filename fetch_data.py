#!/usr/bin/env python3
"""
CME FedWatch Data Scraper v4 — Final Working Version

Scrapes CME FedWatch probability data from the Chinese site (cmegroup.cn/fed-watch/)
which embeds the QuikStrike tool in an iframe.

Key findings from testing:
- URL: https://www.cmegroup.cn/fed-watch/ (Chinese site works best)
- Must use headed browser (headless doesn't render QuikStrike reliably)
- Data lives inside a QuikStrike iframe (QuikStrikeView.aspx)
- 10 meeting tabs with IDs like ctrl0_lbMeeting ... ctrl9_lbMeeting
- Each tab click triggers ASP.NET postback; data updates in 1-3s
- Probability table format:
    TARGET RATE (BPS) | NOW | 1 DAY | 1 WEEK | 1 MONTH
    325-350          | x%  | y%   | z%   | w%
    350-375 (Current)| ...
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
CME_URL = "https://www.cmegroup.cn/fed-watch/"
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DAILY_DIR = DATA_DIR / "daily"
HISTORY_CSV = DATA_DIR / "fedwatch_history.csv"
DEBUG_DIR = DATA_DIR / "debug"

# CSV columns
CSV_HEADER = [
    "snapshot_date", "meeting_date", "rate_range",
    "prob_now", "prob_1d", "prob_1w", "prob_1m",
    "is_most_likely", "contract", "mid_price", "current_target_rate",
]

# Month mapping for Chinese date parsing
CN_MONTHS = {
    '1月': 'Jan', '2月': 'Feb', '3月': 'Mar', '4月': 'Apr',
    '5月': 'May', '6月': 'Jun', '7月': 'Jul', '8月': 'Aug',
    '9月': 'Sep', '10月': 'Oct', '11月': 'Nov', '12月': 'Dec',
}


def parse_cn_date(text):
    """Parse Chinese date like '29 7月 2026' or '29 7月26' to YYYY-MM-DD."""
    text = text.strip()
    # Pattern: DD M YYYY or DD M YY
    m = re.match(r'(\d{1,2})\s*(\d{1,2})月\s*(\d{2,4})', text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return f"{year}-{month:02d}-{day:02d}"
        except ValueError:
            pass
    return text


def parse_pct(s):
    """Parse percentage string to float."""
    if not s:
        return 0.0
    try:
        return float(str(s).strip().replace('%', '').replace('<', '').replace('>', '').replace('≈', '').replace('\u200b', ''))
    except (ValueError, AttributeError):
        return 0.0


def extract_meeting_info(text):
    """
    Extract meeting info from the full text using regex.
    Returns dict with meeting_date, contract, expires, mid_price.
    """
    info = {}

    # Meeting Date line: looks like "29 7月 2026" followed by contract info
    dm = re.search(r'(\d{1,2}\s*\d{1,2}月\s*\d{4})\s+(\w+)\s+(\d{1,2}\s*\d{1,2}月\s*\d{4})\s+([\d.]+)', text)
    if dm:
        info['meeting_date_raw'] = dm.group(1)
        info['meeting_date'] = parse_cn_date(dm.group(1))
        info['contract'] = dm.group(2)
        info['expires_raw'] = dm.group(3)
        info['expires'] = parse_cn_date(dm.group(3))
        info['mid_price'] = dm.group(4)

    # Current target rate
    tm = re.search(r'Current target rate is (\d+-\d+)', text)
    if tm:
        info['current_target'] = tm.group(1)

    # Timestamp
    tsm = re.search(r'Data as of (.+?)\s*CT', text)
    if tsm:
        info['timestamp'] = tsm.group(1).strip()

    return info


def extract_probabilities_from_text(text):
    """
    Extract the full probability table from iframe text.

    Returns list of dicts: [{range, now, day1, week1, month1}, ...]
    Also extracts EASE/NO_CHANGE/HIKE summary.
    """
    result = {'summary': {}, 'table': []}

    lines = text.split('\n')

    # ── Find EASE / NO CHANGE / HIKE summary ───────────────────────
    for i, line in enumerate(lines):
        if re.match(r'EASE\s+NO\s*CHANGE\s+HIKE', line, re.I):
            # Next non-empty line has percentages
            for j in range(i + 1, min(i + 4, len(lines))):
                pcts = re.findall(r'([\d.]+)%', lines[j])
                if len(pcts) >= 3:
                    result['summary'] = {
                        'ease': float(pcts[0]),
                        'no_change': float(pcts[1]),
                        'hike': float(pcts[2]),
                    }
                break
            break

    # ── Find detailed probability table ─────────────────────────────
    # Table header: TARGET RATE (BPS) ... PROBABILITY(%) ... NOW * 1 DAY 1 WEEK 1 MONTH
    # Then dates row, then data rows like: "325-350\t0.0%\t0.0%\t0.0%\t2.3%"
    in_table = False
    header_found = False
    date_line_found = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Detect header
        if 'TARGET RATE' in stripped.upper() and 'PROBABILITY' in stripped.upper():
            header_found = True
            continue

        if header_found and not date_line_found:
            # This should be the sub-header line: NOW * 1 DAY 1 WEEK 1 MONTH
            if 'NOW' in stripped.upper() or '1 DAY' in stripped.upper() or '1 WEEK' in stripped.upper():
                date_line_found = True
                in_table = True
            continue

        if in_table and stripped:
            # Check if this is a data row: starts with rate range pattern like "325-350" or "350-375"
            # Rate ranges are in bps format: XXX-XXX or XXX-XXX (Current)
            row_match = re.match(r'^(\d+-\d+(?:\s*\(Current\))?)\t(.+)$', stripped)
            if row_match:
                rate_range = row_match.group(1).strip()
                values = row_match.group(2)
                cols = re.split(r'\t+', values)

                probs = {
                    'range': rate_range,
                    'now': parse_pct(cols[0]) if len(cols) > 0 else 0,
                    'day1': parse_pct(cols[1]) if len(cols) > 1 else 0,
                    'week1': parse_pct(cols[2]) if len(cols) > 2 else 0,
                    'month1': parse_pct(cols[3]) if len(cols) > 3 else 0,
                }
                result['table'].append(probs)
            elif re.match(r'^\d+-\d+', stripped):
                # Fallback: space-separated
                parts = re.split(r'\s+', stripped)
                if len(parts) >= 2 and '%' in str(parts[1]):
                    result['table'].append({
                        'range': parts[0],
                        'now': parse_pct(parts[1]),
                        'day1': parse_pct(parts[2]) if len(parts) > 2 else 0,
                        'week1': parse_pct(parts[3]) if len(parts) > 3 else 0,
                        'month1': parse_pct(parts[4]) if len(parts) > 4 else 0,
                    })

            # Stop at footer lines
            if stripped.startswith('* Data') or stripped.startswith('Powered by'):
                break

    return result


# ── Main scraper function ────────────────────────────────────────────────────

def scrape_all_meetings(headless=False, max_wait=90):
    """
    Main scraping function.
    Opens CME Chinese FedWatch page, waits for QuikStrike to render,
    then clicks through each meeting tab to extract all probability data.

    Args:
        headless: Whether to run browser in headless mode (False recommended)
        max_wait: Max seconds to wait for QuikStrike to render

    Returns:
        List of dicts, one per meeting, containing all extracted data.
    """
    all_meetings = []

    with sync_playwright() as p:
        print(f"Launching Chromium (headless={headless})...")
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
        )

        print(f"Loading {CME_URL}...")
        page.goto(CME_URL, wait_until='domcontentloaded', timeout=60000)

        # ── Wait for QuikStrike iframe to render ───────────────────────
        qs_frame = None
        print(f"Waiting for QuikStrike (max {max_wait}s)...")

        for sec in range(0, max_wait + 1, 5):
            time.sleep(min(5, max_wait - sec))
            elapsed = min(sec + 5, max_wait)

            for fi, frame in enumerate(page.frames):
                try:
                    text = frame.inner_text('body')
                    if 'EASE' in text and len(text) > 500:
                        qs_frame = frame
                        if elapsed % 15 == 0 or len(text) > 900:
                            print(f"  [{elapsed}s] QuikStrike rendered! frame[{fi}] ({len(text)} chars)")
                            break
                except Exception:
                    pass
            if qs_frame:
                break

            if elapsed % 15 == 0:
                print(f"  [{elapsed}s] Still waiting...")

        if not qs_frame:
            print("ERROR: QuikStrike did not render within timeout.")
            # Save what we have for debugging
            debug_dir = DEBUG_DIR
            debug_dir.mkdir(parents=True, exist_ok=True)
            with open(debug_dir / 'failed_frames.txt', 'w') as f:
                for fi, frame in enumerate(page.frames):
                    f.write(f"frame[{fi}]: {frame.url}\n")
            browser.close()
            return []

        # ── Find meeting tab links ────────────────────────────────────
        tab_links = qs_frame.evaluate('''() => {
            const links = document.querySelectorAll('a[id*="lbMeeting"]');
            return Array.from(links).map(a => ({
                id: a.id,
                text: a.textContent.trim(),
            }));
        }''')

        print(f"\nFound {len(tab_links)} meeting tabs:")
        for t in tab_links:
            print(f"  {t['text']} -> {t['id']}")

        if not tab_links:
            print("ERROR: No meeting tabs found!")
            browser.close()
            return []

        # ── Click each tab and extract data ───────────────────────────
        for idx, tab in enumerate(tab_links):
            tab_id = tab['id']
            tab_text = tab['text']
            print(f"\n{'='*50}")
            print(f"[{idx+1}/{len(tab_links)}] {tab_text}")

            # Click the tab (use __doPostBack via JS for reliability)
            try:
                clicked = qs_frame.evaluate('''(id) => {
                    const el = document.getElementById(id);
                    if (el) { el.click(); return true; }
                    return false;
                }''', tab_id)

                if not clicked:
                    print(f"  WARNING: Could not find element {tab_id}")
                    continue

                # Wait for ASP.NET update
                ready = False
                for w in range(12):  # Max 12 seconds waiting
                    time.sleep(1)
                    ready = qs_frame.evaluate('''() => {
                        // Check that throbber is hidden and we have data cells
                        const throbber = document.querySelector('.throbber, [class*="loading"]');
                        if (throbber && throbber.offsetParent !== null) return false;
                        const cells = document.querySelectorAll('td.center:not(.hide)');
                        return cells.length > 0;
                    }''')
                    if ready:
                        print(f"  Data updated after {w+1}s")
                        break

                time.sleep(1)  # Extra buffer

            except Exception as e:
                print(f"  Click error: {e}")
                continue

            # ── Extract text and parse ───────────────────────────────
            try:
                iframe_text = qs_frame.inner_text('body')

                # Parse meeting info
                info = extract_meeting_info(iframe_text)

                # Parse probabilities
                probs = extract_probabilities_from_text(iframe_text)

                meeting_record = {
                    'tab_label': tab_text,
                    'meeting_date': info.get('meeting_date', ''),
                    'meeting_date_raw': info.get('meeting_date_raw', ''),
                    'contract': info.get('contract', ''),
                    'expires': info.get('expires', ''),
                    'mid_price': info.get('mid_price', ''),
                    'current_target': info.get('current_target', ''),
                    'timestamp': info.get('timestamp', ''),
                    'summary': probs.get('summary', {}),
                    'rate_probabilities': probs.get('table', []),
                }

                all_meetings.append(meeting_record)

                # Print summary
                summary = probs.get('summary', {})
                print(f"  Date: {info.get('meeting_date', '?')}")
                print(f"  Contract: {info.get('contract', '?')} @ {info.get('mid_price', '?')}")
                if summary:
                    print(f"  EASE: {summary.get('ease', '?')}% | NO CHANGE: {summary.get('no_change', '?')}% | HIKE: {summary.get('hike', '?')}%")

                rp = probs.get('table', [])
                visible_rp = [r for r in rp if r['now'] > 0 or r['month1'] > 0]
                print(f"  Rate ranges with data: {len(visible_rp)}")
                for r in visible_rp[:6]:
                    print(f"    {r['range']:16s}  NOW={r['now']:>6.1f}%  1D={r['day1']:>6.1f}%  "
                          f"1W={r['week1']:>6.1f}%  1M={r['month1']:>6.1f}%")

            except Exception as e:
                print(f"  Extraction error: {e}")
                import traceback
                traceback.print_exc()

        browser.close()

    return all_meetings


# ── Persistence functions ─────────────────────────────────────────────────────

def save_results(meetings, snapshot_date=None):
    """Save extracted data to JSON and CSV formats."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # ── Save full JSON snapshot ──────────────────────────────────────
    snap_file = DAILY_DIR / f"{snapshot_date}.json"
    with open(snap_file, 'w', encoding='utf-8') as f:
        json.dump({
            'snapshot_date': snapshot_date,
            'scrape_time': datetime.now(timezone.utc).isoformat(),
            'source_url': CME_URL,
            'num_meetings': len(meetings),
            'meetings': meetings,
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved JSON: {snap_file}")

    # ── Append to history CSV ───────────────────────────────────────
    if meetings:
        if not HISTORY_CSV.exists():
            with open(HISTORY_CSV, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(CSV_HEADER)

        rows_added = 0
        with open(HISTORY_CSV, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            for m in meetings:
                md = m.get('meeting_date', '')
                contract = m.get('contract', '')
                mid_price = m.get('mid_price', '')
                current_target = m.get('current_target', '')

                for rp in m.get('rate_probabilities', []):
                    now_val = rp.get('now', 0)
                    writer.writerow([
                        snapshot_date, md, rp['range'],
                        now_val, rp.get('day1', 0), rp.get('week1', 0), rp.get('month1', 0),
                        1 if now_val >= max(
                            [r.get('now', 0) for r in m.get('rate_probabilities', [])] or [0]
                        ) and now_val > 0 else 0,
                        contract, mid_price, current_target,
                    ])
                    rows_added += 1

        print(f"Appended {rows_added} rows to {HISTORY_CSV}")

    return snap_file


# ── Main entry point ──────────────────────────────────────────────────────────

def main():
    start = datetime.now(timezone.utc)
    sd = start.strftime('%Y-%m-%d')
    print(f"{'='*60}")
    print(f"CME FedWatch Scraper v4")
    print(f"Time: {start.isoformat()} | Snapshot: {sd}")
    print(f"URL: {CME_URL}")
    print(f"{'='*60}\n")

    # Scrape with headed browser (recommended for local use)
    meetings = scrape_all_meetings(headless=False, max_wait=90)

    if not meetings:
        print("\nFATAL: No data extracted!")
        sys.exit(1)

    # Summary
    print(f"\n{'='*60}")
    print(f"EXTRACTED {len(meetings)} FOMC MEETINGS:")
    print(f"{'='*60}")
    for m in meetings:
        md = m.get('meeting_date', '?')
        ct = m.get('current_target', '?')
        s = m.get('summary', {})
        top_range = '?'
        top_prob = 0
        for rp in m.get('rate_probabilities', []):
            if rp.get('now', 0) > top_prob:
                top_prob = rp['now']
                top_range = rp['range']
        print(f"  {md} | Target: {ct} | Top: {top_range}={top_prob:.1f}%")

    # Save results
    save_file = save_results(meetings, sd)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"\n✅ Done in {elapsed:.1f}s | Saved: {save_file}")

    return meetings


if __name__ == "__main__":
    main()
