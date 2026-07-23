#!/usr/bin/env python3
"""
Server 酱 (ServerChan) WeChat push helper.

Posts a daily summary of CME FedWatch changes to WeChat via ServerChan.
Requires SERVERCHAN_SENDKEY environment variable.
"""
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests


def _range_bounds(rng):
    m = re.match(r"(\d+)-(\d+)", str(rng))
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def _range_midpoint(rng):
    lo, hi = _range_bounds(rng)
    if lo is None:
        return 0
    return (lo + hi) / 200


def _most_likely_range_by_median(sub):
    """Pick the median-implied rate range (cumulative probability >= 50%)."""
    if sub.empty:
        return None, 0
    dedup = sub.drop_duplicates("rate_range").copy()
    dedup["_lo"] = dedup["rate_range"].apply(lambda x: _range_bounds(x)[0])
    dedup = dedup.sort_values("_lo", ascending=False).reset_index(drop=True)
    cum = 0.0
    chosen_idx = None
    chosen_cum = 0.0
    for i, row in dedup.iterrows():
        cum += row["prob_now"]
        if cum >= 50 and chosen_idx is None:
            chosen_idx = i
            chosen_cum = cum
    if chosen_idx is None:
        chosen_idx = len(dedup) - 1
        chosen_cum = cum
    chosen = dedup.iloc[chosen_idx]
    return chosen["rate_range"], chosen_cum


def load_history(csv_path):
    """Load and normalize fedwatch_history.csv."""
    df = pd.read_csv(csv_path)
    if "date" in df.columns and "snapshot_date" not in df.columns:
        df = df.rename(columns={"date": "snapshot_date"})
    if "probability" in df.columns and "prob_now" not in df.columns:
        df = df.rename(columns={"probability": "prob_now"})
    if "current_target" in df.columns and "current_target_rate" not in df.columns:
        df = df.rename(columns={"current_target": "current_target_rate"})
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce").dt.date
    df["meeting_date"] = pd.to_datetime(df["meeting_date"], errors="coerce").dt.date
    df["prob_now"] = pd.to_numeric(df["prob_now"], errors="coerce")
    df = df.dropna(subset=["snapshot_date", "meeting_date"])
    return df


def compute_rate_path(df_snapshot):
    """Compute median-implied rate path for a snapshot."""
    meetings_sorted = sorted(df_snapshot["meeting_date"].unique())
    rows = []
    for md in meetings_sorted:
        sub = df_snapshot[df_snapshot["meeting_date"] == md]
        rng, prob = _most_likely_range_by_median(sub)
        if rng is None:
            continue
        rows.append({
            "meeting_date": md,
            "rate_range": rng,
            "probability": prob,
        })
    return pd.DataFrame(rows)


def build_rate_path_changes(df):
    """Compare today's vs previous snapshot's median-implied rate path."""
    all_dates = sorted(df["snapshot_date"].unique())
    if len(all_dates) < 2:
        return []

    curr_date = all_dates[-1]
    prev_date = all_dates[-2]
    curr = compute_rate_path(df[df["snapshot_date"] == curr_date].drop_duplicates(subset=["meeting_date", "rate_range"]))
    prev = compute_rate_path(df[df["snapshot_date"] == prev_date].drop_duplicates(subset=["meeting_date", "rate_range"]))

    merged = curr.merge(prev, on="meeting_date", suffixes=("", "_prev"), how="left")
    changes = []
    for _, row in merged.iterrows():
        prev_rng = row.get("rate_range_prev")
        if pd.notna(prev_rng) and prev_rng != row["rate_range"]:
            changes.append({
                "meeting_date": row["meeting_date"],
                "meeting_label": row["meeting_date"].strftime("%b %d, %Y"),
                "prev_rate": prev_rng,
                "curr_rate": row["rate_range"],
                "prev_prob": row.get("probability_prev", 0),
                "curr_prob": row["probability"],
            })
    return changes


def build_prob_alerts(df, threshold=5.0):
    """Build Panel-4 style per-meeting probability alerts vs 1 day and 1 week ago."""
    all_dates = sorted(df["snapshot_date"].unique())
    if len(all_dates) < 2:
        return []

    latest_date = all_dates[-1]
    latest = df[df["snapshot_date"] == latest_date].drop_duplicates(subset=["meeting_date", "rate_range"])

    def find_prev_date(target_days_back):
        latest = all_dates[-1]
        candidates = [d for d in all_dates if (latest - d).days >= target_days_back - 1]
        return max(candidates) if candidates else None

    prev_1d = find_prev_date(1)
    prev_1w = find_prev_date(7)
    prev_1d_data = df[df["snapshot_date"] == prev_1d].drop_duplicates(subset=["meeting_date", "rate_range"]) if prev_1d else pd.DataFrame()
    prev_1w_data = df[df["snapshot_date"] == prev_1w].drop_duplicates(subset=["meeting_date", "rate_range"]) if prev_1w else pd.DataFrame()

    meetings_sorted = sorted(latest["meeting_date"].unique())
    alerts = []
    for md in meetings_sorted:
        curr_sub = latest[latest["meeting_date"] == md]
        if curr_sub.empty:
            continue
        for _, crow in curr_sub.iterrows():
            rng = crow["rate_range"]
            curr_prob = crow["prob_now"]
            d1 = None
            w1 = None
            if not prev_1d_data.empty:
                prow = prev_1d_data[(prev_1d_data["meeting_date"] == md) & (prev_1d_data["rate_range"] == rng)]
                if not prow.empty:
                    d1 = curr_prob - prow["prob_now"].iloc[0]
            if not prev_1w_data.empty:
                prow = prev_1w_data[(prev_1w_data["meeting_date"] == md) & (prev_1w_data["rate_range"] == rng)]
                if not prow.empty:
                    w1 = curr_prob - prow["prob_now"].iloc[0]
            if (d1 is not None and abs(d1) >= threshold) or (w1 is not None and abs(w1) >= threshold):
                alerts.append({
                    "meeting": md.strftime("%b %d, %Y"),
                    "range": rng,
                    "now": curr_prob,
                    "d1": d1,
                    "w1": w1,
                })
    return alerts


def build_daily_summary(data_dir):
    """Build Markdown summary for ServerChan push."""
    csv_path = Path(data_dir) / "fedwatch_history.csv"
    if not csv_path.exists():
        return None, None

    df = load_history(csv_path)
    if df.empty:
        return None, None

    all_dates = sorted(df["snapshot_date"].unique())
    latest_date = all_dates[-1]

    # Try to get exact scrape time from daily JSON
    scrape_time_str = ""
    daily_json = Path(data_dir) / "daily" / f"{latest_date}.json"
    if daily_json.exists():
        try:
            import json
            with open(daily_json, "r", encoding="utf-8") as f:
                snap = json.load(f)
            iso = snap.get("scrape_time")
            if iso:
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                cst = dt.astimezone(timezone(timedelta(hours=8)))
                scrape_time_str = cst.strftime("%Y-%m-%d %H:%M CST")
        except Exception:
            pass

    title = f"CME FedWatch 日报 - {latest_date}"

    lines = []
    lines.append(f"**数据日期**: {latest_date}")
    if scrape_time_str:
        lines.append(f"**抓取时间**: {scrape_time_str}")
    lines.append("")

    # Rate path changes
    path_changes = build_rate_path_changes(df)
    if path_changes:
        lines.append("#### 🔴 利率预期路径变化")
        for ch in path_changes:
            lines.append(
                f"- **{ch['meeting_label']}**: `{ch['prev_rate']}` → `{ch['curr_rate']}` "
                f"({ch['prev_prob']:.1f}% → {ch['curr_prob']:.1f}%)"
            )
    else:
        lines.append("#### ✅ 利率预期路径稳定")
        lines.append("所有会议的 median-implied 目标利率区间与昨日相比无变化。")
    lines.append("")

    # Probability alerts
    alerts = build_prob_alerts(df)
    if alerts:
        lines.append("#### ⚠️ 概率显著变动 (|Δ| ≥ 5%)")
        lines.append("| 会议 | 区间 | 当前 | 1天前 | 1周前 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for a in alerts:
            d1 = f"{a['d1']:+.1f}%" if a["d1"] is not None else "—"
            w1 = f"{a['w1']:+.1f}%" if a["w1"] is not None else "—"
            lines.append(f"| {a['meeting']} | {a['range']} | {a['now']:.1f}% | {d1} | {w1} |")
    else:
        lines.append("#### ✅ 概率变动平稳")
        lines.append("没有利率区间的概率出现 ≥ 5% 的显著变化。")
    lines.append("")

    lines.append(f"[查看完整仪表盘](https://zuowood1234-cme-fedwatch-tracker.streamlit.app)")

    return title, "\n".join(lines)


def send_serverchan(title, content, sendkey=None):
    """Send message via ServerChan. Returns (success, response_text)."""
    if sendkey is None:
        sendkey = os.environ.get("SERVERCHAN_SENDKEY")
    if not sendkey:
        return False, "SERVERCHAN_SENDKEY not set"

    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = {"title": title, "desp": content}
    try:
        resp = requests.post(url, data=payload, timeout=20)
        data = resp.json()
        if data.get("code") == 0:
            return True, str(data)
        return False, str(data)
    except Exception as e:
        return False, str(e)


def push_daily_summary(data_dir):
    """Build and push daily summary. Returns (success, message)."""
    title, content = build_daily_summary(data_dir)
    if title is None:
        return False, "No data available for summary"
    return send_serverchan(title, content)


if __name__ == "__main__":
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "./data"
    ok, msg = push_daily_summary(data_dir)
    print("OK" if ok else "FAILED", msg)
    sys.exit(0 if ok else 1)
