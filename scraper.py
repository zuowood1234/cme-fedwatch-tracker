#!/usr/bin/env python3
"""
CME FedWatch Scraper Module — Importable by both CLI and Streamlit

Scrapes probability data from cmegroup.cn/fed-watch/ (QuikStrike widget).
Uses Playwright + headed/xvfb browser.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
CME_URL = "https://www.cmegroup.cn/fed-watch/"

CN_MONTHS = {
    '1月': 'Jan', '2月': 'Feb', '3月': 'Mar', '4月': 'Apr',
    '5月': 'May', '6月': 'Jun', '7月': 'Jul', '8月': 'Aug',
    '9月': 'Sep', '10月': 'Oct', '11月': 'Nov', '12月': 'Dec',
}

CSV_HEADER = [
    "snapshot_date", "meeting_date", "rate_range",
    "prob_now", "prob_1d", "prob_1w", "prob_1m",
    "is_most_likely", "contract", "mid_price", "current_target_rate",
]


def parse_date(text):
    """Parse date like '29 Jul 2026' or '29 7月 2026' to YYYY-MM-DD."""
    text = text.strip()
    # Chinese format: '29 7月 2026'
    m = re.match(r'(\d{1,2})\s*(\d{1,2})月\s*(\d{2,4})', text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return f"{year}-{month:02d}-{day:02d}"
        except ValueError:
            pass

    # English format: '29 Jul 2026'
    m = re.match(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2,4})', text)
    if m:
        day, month_str, year = m.group(1), m.group(2), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            month = datetime.strptime(month_str, '%b').month
            return f"{year}-{month:02d}-{int(day):02d}"
        except ValueError:
            pass

    return text


# Keep old name for backward compatibility
parse_cn_date = parse_date


def parse_pct(s):
    """Parse percentage string to float."""
    if not s:
        return 0.0
    try:
        return float(str(s).strip().replace('%', '').replace('<', '')
                     .replace('>', '').replace('≈', '').replace('\u200b', ''))
    except (ValueError, AttributeError):
        return 0.0


def extract_meeting_info(text):
    """Extract meeting info dict from iframe text."""
    info = {}

    # Chinese format: "29 7月 2026 ZQN6 31 7月 2026 96.3700"
    dm = re.search(
        r'(\d{1,2}\s*\d{1,2}月\s*\d{4})\s+(\w+)\s+'
        r'(\d{1,2}\s*\d{1,2}月\s*\d{4})\s+([\d.]+)', text
    )

    # English format: "29 Jul 2026 ZQN6 31 Jul 2026 96.3700"
    if not dm:
        dm = re.search(
            r'(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+(\w+)\s+'
            r'(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+([\d.]+)', text
        )

    if dm:
        info['meeting_date'] = parse_date(dm.group(1))
        info['contract'] = dm.group(2)
        info['expires'] = parse_date(dm.group(3))
        info['mid_price'] = dm.group(4)

    tm = re.search(r'Current target rate is (\d+-\d+)', text, re.I)
    if tm:
        info['current_target'] = tm.group(1)

    tsm = re.search(r'Data as of (.+?)\s*CT', text)
    if tsm:
        info['timestamp'] = tsm.group(1).strip()

    return info


def extract_probabilities_from_text(text):
    """
    Extract full probability table from iframe text.
    Returns {'summary': {ease, no_change, hike}, 'table': [{range, now, day1, week1, month1}]}
    """
    result = {'summary': {}, 'table': []}
    lines = text.split('\n')

    # Find EASE / NO CHANGE / HIKE summary (allow spaces before %)
    for i, line in enumerate(lines):
        if re.match(r'EASE\s+NO\s*CHANGE\s+HIKE', line, re.I):
            for j in range(i + 1, min(i + 4, len(lines))):
                pcts = re.findall(r'([\d.]+)\s*%', lines[j])
                if len(pcts) >= 3:
                    result['summary'] = {
                        'ease': float(pcts[0]),
                        'no_change': float(pcts[1]),
                        'hike': float(pcts[2]),
                    }
                break
            break

    # Find detailed probability table
    header_found = False
    date_line_found = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if 'TARGET RATE' in stripped.upper() and 'PROBABILITY' in stripped.upper():
            header_found = True
            continue

        if header_found and not date_line_found:
            if 'NOW' in stripped.upper() or '1 DAY' in stripped.upper():
                date_line_found = True
                continue

        if header_found and date_line_found and stripped:
            row_match = re.match(r'^(\d+-\d+(?:\s*\(Current\))?)\t(.+)$', stripped)
            if row_match:
                rate_range = row_match.group(1).strip()
                values = row_match.group(2)
                cols = re.split(r'\t+', values)
                result['table'].append({
                    'range': rate_range,
                    'now': parse_pct(cols[0]) if len(cols) > 0 else 0,
                    'day1': parse_pct(cols[1]) if len(cols) > 1 else 0,
                    'week1': parse_pct(cols[2]) if len(cols) > 2 else 0,
                    'month1': parse_pct(cols[3]) if len(cols) > 3 else 0,
                })
            elif re.match(r'^\d+-\d+', stripped):
                parts = re.split(r'\s+', stripped)
                # Find the first part that looks like a percentage
                pct_idx = next((i for i, p in enumerate(parts) if '%' in p), None)
                if pct_idx is not None and pct_idx > 0:
                    rate_range = ' '.join(parts[:pct_idx])
                    result['table'].append({
                        'range': rate_range,
                        'now': parse_pct(parts[pct_idx]),
                        'day1': parse_pct(parts[pct_idx + 1]) if len(parts) > pct_idx + 1 else 0,
                        'week1': parse_pct(parts[pct_idx + 2]) if len(parts) > pct_idx + 2 else 0,
                        'month1': parse_pct(parts[pct_idx + 3]) if len(parts) > pct_idx + 3 else 0,
                    })

            if stripped.startswith('* Data') or stripped.startswith('Powered by'):
                break

    return result


def extract_meeting_from_dom(frame):
    """
    Extract meeting data using QuikStrike DOM selectors.
    More reliable than regex when text formatting changes.
    """
    try:
        data = frame.evaluate('''() => {
            function findInnerTable(keyword) {
                const tables = document.querySelectorAll('table.grid-thm');
                for (const t of tables) {
                    const text = Array.from(t.querySelectorAll('th, td')).map(el => el.textContent.trim()).join(' ');
                    if (text.includes(keyword)) return t;
                }
                return null;
            }

            function parsePct(s) {
                if (!s) return 0;
                const cleaned = s.toString().replace(/[%<>≈\u200b]/g, '').trim();
                const m = cleaned.match(/([\\d.]+)/);
                return m ? parseFloat(m[1]) : 0;
            }

            const result = { meeting_date: '', contract: '', expires: '', mid_price: '', current_target: '', summary: {}, table: [] };

            // Meeting info table (inner grid-thm table)
            const infoTable = findInnerTable('Meeting Date');
            if (infoTable) {
                const cells = infoTable.querySelectorAll('td');
                if (cells.length >= 4) {
                    result.meeting_date = cells[0].textContent.trim();
                    result.contract = cells[1].textContent.trim();
                    result.expires = cells[2].textContent.trim();
                    result.mid_price = cells[3].textContent.trim();
                }
            }

            // Probabilities summary table
            const probTable = findInnerTable('Probabilities');
            if (probTable) {
                const rows = probTable.querySelectorAll('tr');
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length >= 3) {
                        result.summary = {
                            ease: parsePct(cells[0].textContent),
                            no_change: parsePct(cells[1].textContent),
                            hike: parsePct(cells[2].textContent),
                        };
                    }
                }
            }

            // Target rate table
            const rateTable = findInnerTable('Target Rate (bps)');
            if (rateTable) {
                const rows = rateTable.querySelectorAll('tr');
                for (const row of rows) {
                    if (row.classList.contains('hide')) continue;
                    const cells = row.querySelectorAll('td');
                    if (cells.length >= 2) {
                        const range = cells[0].textContent.trim();
                        const values = Array.from(cells).slice(1).map(c => c.textContent.trim());
                        if (/^\\d+-\\d+/.test(range)) {
                            result.table.push({
                                range: range,
                                now: parsePct(values[0]),
                                day1: parsePct(values[1]),
                                week1: parsePct(values[2]),
                                month1: parsePct(values[3]),
                            });
                        }
                    }
                }
            }

            // Current target rate
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const m = el.textContent.match(/Current target rate is (\\d+-\\d+)/i);
                if (m) {
                    result.current_target = m[1];
                    break;
                }
            }

            return result;
        }''')
        return data
    except Exception:
        return None


# ── Main scraper ─────────────────────────────────────────────────────────────

def scrape_all_meetings(headless=False, max_wait=90, log_func=print):
    """
    Scrape all FOMC meeting probabilities from CME FedWatch.

    Args:
        headless: Browser mode (False = headed, needs display or xvfb)
        max_wait: Max seconds to wait for QuikStrike render
        log_func: Function for log output (default: print)

    Returns:
        List of dicts (one per meeting) with full probability data.
    """
    from playwright.sync_api import sync_playwright

    all_meetings = []

    with sync_playwright() as p:
        log_func(f"Launching Chromium (headless={headless})...")
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(
            viewport={'width': 1920, 'height': 1080},
            user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
            ),
        )

        log_func(f"Loading {CME_URL}...")
        page.goto(CME_URL, wait_until='domcontentloaded', timeout=60000)

        # Wait for QuikStrike iframe to render
        qs_frame = None
        log_func(f"Waiting for QuikStrike (max {max_wait}s)...")

        for sec in range(0, max_wait + 1, 5):
            time.sleep(min(5, max_wait - sec))
            elapsed = min(sec + 5, max_wait)

            for fi, frame in enumerate(page.frames):
                try:
                    text = frame.inner_text('body')
                    if 'EASE' in text and len(text) > 500:
                        qs_frame = frame
                        if elapsed % 15 == 0 or len(text) > 900:
                            log_func(f"  [{elapsed}s] Rendered! ({len(text)} chars)")
                            break
                except Exception:
                    pass
            if qs_frame:
                break
            if elapsed % 15 == 0:
                log_func(f"  [{elapsed}s] Still waiting...")

        if not qs_frame:
            log_func("ERROR: QuikStrike did not render within timeout.")
            browser.close()
            return []

        # Find meeting tab links
        tab_links = qs_frame.evaluate('''() => {
            const links = document.querySelectorAll('a[id*="lbMeeting"]');
            return Array.from(links).map(a => ({
                id: a.id,
                text: a.textContent.trim(),
            }));
        }''')

        log_func(f"Found {len(tab_links)} meeting tabs")
        if not tab_links:
            log_func("ERROR: No meeting tabs found!")
            browser.close()
            return []

        # Click each tab and extract
        prev_meeting_date = None
        for idx, tab in enumerate(tab_links):
            tab_id = tab['id']
            tab_text = tab['text']
            log_func(f"[{idx+1}/{len(tab_links)}] {tab_text}")

            try:
                # Capture current meeting date before clicking
                if idx > 0:
                    prev_meeting_date = qs_frame.evaluate('''() => {
                        const tables = document.querySelectorAll('table.grid-thm');
                        for (const t of tables) {
                            const text = Array.from(t.querySelectorAll('th, td')).map(el => el.textContent.trim()).join(' ');
                            if (text.includes('Meeting Date')) {
                                const cells = t.querySelectorAll('td');
                                return cells.length > 0 ? cells[0].textContent.trim() : '';
                            }
                        }
                        return '';
                    }''')

                clicked = qs_frame.evaluate('''(id) => {
                    const el = document.getElementById(id);
                    if (el) { el.click(); return true; }
                    return false;
                }''', tab_id)

                if not clicked:
                    log_func(f"  WARNING: Could not find {tab_id}")
                    continue

                # Wait for ASP.NET postback: meeting date changed or throbber gone
                max_wait_loops = 40  # 12 seconds max
                for w in range(max_wait_loops):
                    time.sleep(0.3)
                    ready = qs_frame.evaluate('''(prevDate) => {
                        const throbber = document.querySelector('.throbber, [class*="loading"]');
                        if (throbber && throbber.offsetParent !== null) return false;
                        if (!prevDate) return true;
                        const tables = document.querySelectorAll('table.grid-thm');
                        for (const t of tables) {
                            const text = Array.from(t.querySelectorAll('th, td')).map(el => el.textContent.trim()).join(' ');
                            if (text.includes('Meeting Date')) {
                                const cells = t.querySelectorAll('td');
                                const current = cells.length > 0 ? cells[0].textContent.trim() : '';
                                return current && current !== prevDate;
                            }
                        }
                        return false;
                    }''', prev_meeting_date)
                    if ready:
                        break
                else:
                    log_func("  WARNING: postback may not have completed, data might be stale")

                time.sleep(0.3)

            except Exception as e:
                log_func(f"  Click error: {e}")
                continue

            # Extract data: try DOM selectors first, then regex fallback
            try:
                dom_data = extract_meeting_from_dom(qs_frame)
                if dom_data and len(dom_data.get('table', [])) > 0:
                    info = {
                        'meeting_date': parse_date(dom_data.get('meeting_date', '')),
                        'contract': dom_data.get('contract', ''),
                        'expires': parse_date(dom_data.get('expires', '')),
                        'mid_price': dom_data.get('mid_price', ''),
                        'current_target': dom_data.get('current_target', ''),
                        'timestamp': '',
                    }
                    probs = {
                        'summary': dom_data.get('summary', {}),
                        'table': dom_data.get('table', []),
                    }
                else:
                    raise ValueError("DOM extraction returned empty table")
            except Exception as e:
                iframe_text = qs_frame.inner_text('body')
                info = extract_meeting_info(iframe_text)
                probs = extract_probabilities_from_text(iframe_text)

            # Fallback: parse tab text if meeting date still missing
            if not info.get('meeting_date') and tab_text:
                m = re.match(r'(\d{1,2})\s+([A-Za-z]{3})(\d{2,4})', tab_text)
                if m:
                    info['meeting_date'] = parse_date(f"{m.group(1)} {m.group(2)} {m.group(3)}")

            try:
                meeting_record = {
                    'tab_label': tab_text,
                    'meeting_date': info.get('meeting_date', ''),
                    'contract': info.get('contract', ''),
                    'mid_price': info.get('mid_price', ''),
                    'current_target': info.get('current_target', ''),
                    'timestamp': info.get('timestamp', ''),
                    'summary': probs.get('summary', {}),
                    'rate_probabilities': probs.get('table', []),
                }
                all_meetings.append(meeting_record)

                s = probs.get('summary', {})
                log_func(
                    f"  {info.get('meeting_date', '?')} | "
                    f"E:{s.get('ease','?')}% NC:{s.get('no_change','?')}% H:{s.get('hike','?')}% | "
                    f"{len(probs.get('table', []))} ranges"
                )
            except Exception as e:
                log_func(f"  Extraction error: {e}")

        browser.close()

    return all_meetings


# ── Persistence ──────────────────────────────────────────────────────────────

def save_results(meetings, output_dir, snapshot_date=None):
    """Save scraped data to JSON + CSV in output_dir."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    output_dir = Path(output_dir)
    daily_dir = output_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    history_csv = output_dir / "fedwatch_history.csv"

    # JSON snapshot
    snap_file = daily_dir / f"{snapshot_date}.json"
    with open(snap_file, 'w', encoding='utf-8') as f:
        json.dump({
            'snapshot_date': snapshot_date,
            'scrape_time': datetime.now(timezone.utc).isoformat(),
            'source_url': CME_URL,
            'num_meetings': len(meetings),
            'meetings': meetings,
        }, f, indent=2, ensure_ascii=False, default=str)

    # CSV append
    rows_added = 0
    if meetings:
        if not history_csv.exists():
            with open(history_csv, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(CSV_HEADER)

        with open(history_csv, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            for m in meetings:
                md = m.get('meeting_date', '')
                for rp in m.get('rate_probabilities', []):
                    now_val = rp.get('now', 0)
                    all_nows = [r.get('now', 0) for r in m.get('rate_probabilities', [])]
                    writer.writerow([
                        snapshot_date, md, rp['range'],
                        now_val, rp.get('day1', 0), rp.get('week1', 0), rp.get('month1', 0),
                        1 if now_val >= max(all_nows or [0]) and now_val > 0 else 0,
                        m.get('contract', ''), m.get('mid_price', ''), m.get('current_target', ''),
                    ])
                    rows_added += 1

    return snap_file, rows_added


# ── CLI entry point ──────────────────────────────────────────────────────────

def main(output_dir="./data"):
    start = datetime.now(timezone.utc)
    sd = start.strftime('%Y-%m-%d')
    print(f"CME FedWatch Scraper | {sd}")
    print(f"URL: {CME_URL}")

    meetings = scrape_all_meetings(headless=False, max_wait=90)
    if not meetings:
        print("FATAL: No data extracted!")
        sys.exit(1)

    snap_file, rows = save_results(meetings, output_dir, sd)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"Done in {elapsed:.1f}s | {rows} rows | {snap_file}")
    return meetings


if __name__ == "__main__":
    main()
