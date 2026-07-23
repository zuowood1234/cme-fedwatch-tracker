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


def _most_likely_range_by_median(sub, prob_col="prob_now"):
    """Pick the median-implied rate range (cumulative probability >= 50%).
    Returns (None, 0) if the probability column has no valid data.
    Special case: with only 2 ranges, pick the higher-prob one directly (no cumulative)."""
    if sub.empty:
        return None, 0
    dedup = sub.drop_duplicates("rate_range").copy()
    if prob_col not in dedup.columns or dedup[prob_col].fillna(0).sum() == 0:
        return None, 0

    # Special case: only 2 rate ranges → pick higher-prob range directly
    if len(dedup) == 2:
        best = dedup.loc[dedup[prob_col].idxmax()]
        return best["rate_range"], best[prob_col]

    dedup["_lo"] = dedup["rate_range"].apply(lambda x: _range_bounds(x)[0])
    dedup = dedup.sort_values("_lo", ascending=False).reset_index(drop=True)
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
    if "prob_1d" in df.columns:
        df["prob_1d"] = pd.to_numeric(df["prob_1d"], errors="coerce")
    df = df.dropna(subset=["snapshot_date", "meeting_date"])
    return df


def compute_rate_path(df_snapshot, prob_col="prob_now"):
    """Compute median-implied rate path for a snapshot using the specified probability column."""
    meetings_sorted = sorted(df_snapshot["meeting_date"].unique())
    rows = []
    for md in meetings_sorted:
        sub = df_snapshot[df_snapshot["meeting_date"] == md]
        rng, prob = _most_likely_range_by_median(sub, prob_col=prob_col)
        if rng is None:
            continue
        rows.append({
            "meeting_date": md,
            "rate_range": rng,
            "probability": prob,
        })
    return pd.DataFrame(rows)


def _cumulative_prob_for_range(sub, target_rng, prob_col):
    """Calculate cumulative probability for target_rng and above (high to low)."""
    if sub.empty:
        return None
    dedup = sub.drop_duplicates("rate_range").copy()
    dedup["_lo"] = dedup["rate_range"].apply(lambda x: _range_bounds(x)[0])
    dedup = dedup.sort_values("_lo", ascending=False).reset_index(drop=True)
    cum = 0.0
    for _, row in dedup.iterrows():
        cum += row[prob_col]
        if row["rate_range"] == target_rng:
            return cum
    return cum


def build_rate_path_changes(df):
    """Compare median-implied rate path: prob_now vs prob_1d (CME's own 1 Day Ago column).
    For each changed meeting, report the cumulative probability (>= chosen range) today vs yesterday."""
    all_dates = sorted(df["snapshot_date"].unique())
    if not all_dates:
        return []

    curr_date = all_dates[-1]
    latest = df[df["snapshot_date"] == curr_date].drop_duplicates(subset=["meeting_date", "rate_range"])

    if "prob_1d" not in latest.columns:
        return []

    curr_path = compute_rate_path(latest, prob_col="prob_now")
    prev_path = compute_rate_path(latest, prob_col="prob_1d")

    if curr_path.empty or prev_path.empty:
        return []

    merged = curr_path.merge(prev_path, on="meeting_date", suffixes=("", "_prev"), how="left")
    changes = []
    for _, row in merged.iterrows():
        prev_rng = row.get("rate_range_prev")
        if pd.notna(prev_rng) and prev_rng != row["rate_range"]:
            curr_rng = row["rate_range"]
            sub = latest[latest["meeting_date"] == row["meeting_date"]].drop_duplicates("rate_range")
            # Cumulative probability for curr_rng: prob_now (today) and prob_1d (yesterday)
            cum_now = _cumulative_prob_for_range(sub, curr_rng, "prob_now")
            cum_1d = _cumulative_prob_for_range(sub, curr_rng, "prob_1d")
            changes.append({
                "meeting_date": row["meeting_date"],
                "meeting_label": row["meeting_date"].strftime("%b %d, %Y"),
                "prev_rate": prev_rng,
                "curr_rate": curr_rng,
                "cum_now": cum_now,      # cumulative prob_now (today)
                "cum_1d": cum_1d,        # cumulative prob_1d (yesterday)
            })
    return changes


def build_prob_alerts(df, threshold=5.0):
    """Build Panel-4 style per-meeting probability alerts using CME's own prob_1d and prob_1w columns."""
    all_dates = sorted(df["snapshot_date"].unique())
    if not all_dates:
        return []

    latest_date = all_dates[-1]
    latest = df[df["snapshot_date"] == latest_date].drop_duplicates(subset=["meeting_date", "rate_range"])

    if "prob_1d" not in latest.columns:
        return []

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
            if pd.notna(crow.get("prob_1d")) and crow["prob_1d"] > 0:
                d1 = curr_prob - crow["prob_1d"]
            if pd.notna(crow.get("prob_1w")) and crow["prob_1w"] > 0:
                w1 = curr_prob - crow["prob_1w"]
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

    # Rate path changes (using CME's own prob_1d column)
    path_changes = build_rate_path_changes(df)
    if path_changes:
        lines.append("#### 🔴 利率预期路径变化 (vs 1 Day Ago)")
        lines.append("*累积概率 = 该区间及以上所有区间的概率之和，≥50% 即为 median-implied 最可能利率*")
        for ch in path_changes:
            curr_rng = ch["curr_rate"]
            cum_now = ch["cum_now"]
            cum_1d = ch["cum_1d"]
            if cum_1d is not None and cum_now is not None:
                delta = cum_now - cum_1d
                delta_str = f"+{delta:.1f}%" if delta > 0 else f"{delta:.1f}%"
                lines.append(
                    f"- **{ch['meeting_label']}**: `{ch['prev_rate']}` → `{curr_rng}` "
                    f"| `{curr_rng}` 累积概率: {cum_1d:.1f}% → **{cum_now:.1f}%** ({delta_str})"
                )
            else:
                lines.append(
                    f"- **{ch['meeting_label']}**: `{ch['prev_rate']}` → `{curr_rng}` "
                    f"| `{curr_rng}` 累积概率: **{cum_now:.1f}%**"
                )
    else:
        lines.append("#### ✅ 利率预期路径稳定")
        lines.append("所有会议的 median-implied 目标利率区间与 CME 1 Day Ago 相比无变化。")
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
