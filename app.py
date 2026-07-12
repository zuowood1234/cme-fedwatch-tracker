"""
CME FedWatch Tracker - Streamlit Dashboard

5 panels:
  1. Rate path summary - most likely rate for each meeting (line chart)
  2. Current probability distribution (table)
  3. Past week daily changes (from our archive)
  4. 1-week vs 1-month comparison (from our archive)
  5. Change alerts - highlight big probability shifts

Data source: Official CME FedWatch data, scraped daily via GitHub Actions.
Run locally: streamlit run app.py
"""

import datetime
from io import StringIO

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Config ──────────────────────────────────────────────────────────────────
GITHUB_USERNAME = "zuowood1234"
GITHUB_RAW_BASE = (
    f"https://raw.githubusercontent.com/{GITHUB_USERNAME}"
    f"/cme-fedwatch-tracker/main"
)
CSV_URL = f"{GITHUB_RAW_BASE}/data/fedwatch_history.csv"

# ── Page setup ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CME FedWatch Tracker",
    page_icon="📊",
    layout="wide",
)

st.title("📊 CME FedWatch Tracker")
st.caption(
    "Official CME FedWatch probabilities for every upcoming FOMC meeting. "
    "Data scraped directly from CME. Refreshed daily via GitHub Actions."
)


# ── Load data ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=600)
def load_data(url: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(url)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["meeting_date"] = pd.to_datetime(df["meeting_date"]).dt.date
        df["probability"] = pd.to_numeric(df["probability"], errors="coerce")
        return df
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        return pd.DataFrame()


df = load_data(CSV_URL)

if df.empty:
    st.warning(
        "No data yet. Run the GitHub Actions workflow once to populate "
        "`data/fedwatch_history.csv`."
    )
    st.stop()

# ── Helper: comparison display ──────────────────────────────────────────────
def show_comparison(curr_df, old_df, meetings):
    """Show side-by-side comparison of most likely rate: current vs old."""
    comp_data = []
    for md in meetings:
        c = curr_df[curr_df["meeting_date"] == md]
        o = old_df[old_df["meeting_date"] == md]
        if c.empty:
            continue
        c_max = c.loc[c["probability"].idxmax()]
        curr_rate = c_max["rate_range"]
        curr_prob = c_max["probability"]

        if o.empty:
            old_rate = "—"
            old_prob = 0
            change = 0
        else:
            o_max = o.loc[o["probability"].idxmax()]
            old_rate = o_max["rate_range"]
            old_prob = o_max["probability"]
            change = curr_prob - old_prob

        comp_data.append({
            "Meeting": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
            "Then": f"{old_rate} ({old_prob:.1f}%)" if old_rate != "—" else "—",
            "Now": f"{curr_rate} ({curr_prob:.1f}%)",
            "Change": f"{change:+.1f}%" if old_rate != "—" else "—",
            "Rate Shift": "⚠️" if old_rate != "—" and old_rate != curr_rate else "",
        })

    if comp_data:
        st.dataframe(pd.DataFrame(comp_data), use_container_width=True, hide_index=True)
    else:
        st.info("No comparable data.")


# ── Prepare data ────────────────────────────────────────────────────────────
all_dates = sorted(df["date"].unique())
latest_date = max(all_dates)
today = datetime.date.today()

latest = df[df["date"] == latest_date].copy()
upcoming = latest[latest["meeting_date"] >= today].copy()

if upcoming.empty:
    upcoming = latest.copy()

current_target = (
    upcoming["current_target"].dropna().iloc[0]
    if not upcoming.empty and upcoming["current_target"].notna().any()
    else ""
)
effr_val = (
    upcoming["effr"].dropna().iloc[0]
    if not upcoming.empty and upcoming["effr"].notna().any()
    else "N/A"
)

meetings_sorted = sorted(upcoming["meeting_date"].unique())

# ── Sidebar ─────────────────────────────────────────────────────────────────
st.sidebar.success(f"Last updated:\n{latest_date}")
st.sidebar.markdown("---")
st.sidebar.markdown(f"**Total snapshots:** {len(all_dates)}")
st.sidebar.markdown(f"**Earliest:** {all_dates[0]}")
st.sidebar.markdown(f"**Latest:** {latest_date}")
st.sidebar.markdown("---")
st.sidebar.markdown(
    "**Data source**\n"
    "Official CME FedWatch Tool\n"
    "Scraped daily via Playwright.\n"
    "No calculations — direct from CME."
)
st.sidebar.markdown("---")
st.sidebar.markdown(
    f"**Current target:** {current_target}\n\n"
    f"**EFFR:** {effr_val}%"
)

# ════════════════════════════════════════════════════════════════════════════
# Panel 1: Rate Path Summary - most likely rate for each meeting (line chart)
# ════════════════════════════════════════════════════════════════════════════
st.header("① Rate Path Summary")
st.caption(
    "The most likely target rate for each upcoming FOMC meeting. "
    "This shows the market-implied rate trajectory."
)

# For each meeting, find the most likely rate range
path_data = []
for md in meetings_sorted:
    sub = upcoming[upcoming["meeting_date"] == md]
    if sub.empty:
        continue
    max_row = sub.loc[sub["probability"].idxmax()]
    # Extract midpoint of rate range for plotting
    rng = max_row["rate_range"]
    import re
    m = re.match(r"(\d+\.\d+)%-(\d+\.\d+)%", str(rng))
    midpoint = (float(m.group(1)) + float(m.group(2))) / 2 if m else 0
    path_data.append({
        "meeting_date": md,
        "meeting_label": md.strftime("%b %d, %Y"),
        "rate_range": rng,
        "midpoint": midpoint,
        "probability": max_row["probability"],
    })

if path_data:
    path_df = pd.DataFrame(path_data)

    # Create the line chart
    fig = go.Figure()

    # Add the rate path line
    fig.add_trace(go.Scatter(
        x=path_df["meeting_label"],
        y=path_df["midpoint"],
        mode="lines+markers+text",
        text=[f"{r}<br>{p:.0f}%" for r, p in zip(path_df["rate_range"], path_df["probability"])],
        textposition="top center",
        textfont=dict(size=10),
        line=dict(color="#534AB7", width=3),
        marker=dict(size=12, color="#534AB7"),
        name="Most likely rate",
        hovertemplate="<b>%{x}</b><br>Rate: %{text}<extra></extra>",
    ))

    # Add current rate reference line
    if current_target:
        m = re.match(r"(\d+\.\d+)%-(\d+\.\d+)%", current_target)
        if m:
            current_mid = (float(m.group(1)) + float(m.group(2))) / 2
            fig.add_hline(
                y=current_mid,
                line_dash="dash",
                line_color="#e74c3c",
                annotation_text=f"Current: {current_target}",
                annotation_position="top left",
            )

    fig.update_layout(
        yaxis_title="Target rate (%)",
        yaxis_tickformat=".2f",
        xaxis_title="FOMC meeting",
        height=400,
        showlegend=False,
        hovermode="x unified",
    )

    st.plotly_chart(fig, use_container_width=True)

    # Quick summary cards
    st.markdown("**Quick view:**")
    cols = st.columns(min(len(path_data), 6))
    for i, row in path_data[:6]:
        with cols[i % 6]:
            st.metric(
                label=row["meeting_label"],
                value=row["rate_range"],
                delta=f"{row['probability']:.1f}%",
            )

# ════════════════════════════════════════════════════════════════════════════
# Panel 2: Current Probability Distribution (full table)
# ════════════════════════════════════════════════════════════════════════════
st.header("② Current Probability Distribution")
st.caption(
    "Complete probability breakdown for every meeting × every rate range. "
    "The current target column is highlighted in purple. "
    "The highest probability in each row is bold."
)

# Pivot: meetings as rows, rate ranges as columns
pivot = upcoming.pivot_table(
    index="meeting_date", columns="rate_range", values="probability", aggfunc="first"
).sort_index()

# Sort columns by rate value (ascending)
def sort_rate_ranges(col):
    m = re.match(r"(\d+\.\d+)%", str(col))
    return float(m.group(1)) if m else 0

if not pivot.empty:
    pivot = pivot.reindex(columns=sorted(pivot.columns, key=sort_rate_ranges))

    # Style the table
    def style_distribution(row):
        styles = [""] * len(row)
        for i, col in enumerate(pivot.columns):
            val = row[col]
            if pd.isna(val):
                continue
            # Highlight current target column
            if str(col) == str(current_target):
                styles[i] = "background-color: #EEEDFE; font-weight: bold; color: #534AB7"
            # Bold the max value in each row
            elif val == row.max():
                styles[i] = "font-weight: bold; color: #D85A30"
        return styles

    # Format meeting dates for display
    display_pivot = pivot.copy()
    display_pivot.index = [d.strftime("%b %d, %Y") if hasattr(d, "strftime") else str(d) for d in display_pivot.index]

    styled = display_pivot.style.format("{:.1f}%", na_rep="—").apply(style_distribution, axis=1)
    st.dataframe(styled, use_container_width=True, height=400)

# ════════════════════════════════════════════════════════════════════════════
# Panel 3: Past Week Daily Changes
# ════════════════════════════════════════════════════════════════════════════
st.header("③ Past Week - Daily Rate Changes")
st.caption(
    "How the most likely rate for each meeting changed over the past 7 days. "
    "Each line = one meeting. Data from our daily archive."
)

# Get the last 7 days of data
past_week_dates = [d for d in all_dates if d >= latest_date - datetime.timedelta(days=7)]
past_week = df[df["date"].isin(past_week_dates)].copy()

if len(past_week_dates) >= 2:
    # For each date + meeting, find the most likely rate midpoint
    weekly_path = []
    for d in past_week_dates:
        day_data = past_week[past_week["date"] == d]
        for md in meetings_sorted:
            sub = day_data[day_data["meeting_date"] == md]
            if sub.empty:
                continue
            max_row = sub.loc[sub["probability"].idxmax()]
            rng = max_row["rate_range"]
            m = re.match(r"(\d+\.\d+)%-(\d+\.\d+)%", str(rng))
            midpoint = (float(m.group(1)) + float(m.group(2))) / 2 if m else 0
            weekly_path.append({
                "date": d,
                "meeting_date": md,
                "meeting_label": md.strftime("%b %d, %Y"),
                "midpoint": midpoint,
                "rate_range": rng,
            })

    if weekly_path:
        wp_df = pd.DataFrame(weekly_path)

        fig3 = px.line(
            wp_df,
            x="date",
            y="midpoint",
            color="meeting_label",
            markers=True,
            labels={
                "date": "Date",
                "midpoint": "Most likely rate (%)",
                "meeting_label": "FOMC meeting",
            },
            title="Daily change in most likely rate (past week)",
        )
        fig3.update_layout(
            height=400,
            hovermode="x unified",
            yaxis_tickformat=".2f",
        )
        st.plotly_chart(fig3, use_container_width=True)

        # Also show a table of daily most-likely rates
        st.markdown("**Daily most likely rate by meeting:**")
        weekly_table = wp_df.pivot_table(
            index="meeting_label", columns="date", values="rate_range", aggfunc="first"
        ).sort_index()
        weekly_table.columns = [d.strftime("%m/%d") if hasattr(d, "strftime") else str(d) for d in weekly_table.columns]
        st.dataframe(weekly_table, use_container_width=True)
    else:
        st.info("Not enough data to show weekly changes yet.")
else:
    st.info(
        f"Only {len(past_week_dates)} snapshot(s) available. "
        f"Need at least 2 days of data to show weekly changes. "
        f"Check back after more daily updates."
    )

# ════════════════════════════════════════════════════════════════════════════
# Panel 4: 1-Week vs 1-Month Comparison
# ════════════════════════════════════════════════════════════════════════════
st.header("④ 1-Week vs 1-Month Comparison")
st.caption(
    "How probabilities have shifted compared to 1 week ago and 1 month ago. "
    "Shows the change in most likely rate for each meeting."
)

# Find dates closest to 1 week ago and 1 month ago
one_week_ago = latest_date - datetime.timedelta(days=7)
one_month_ago = latest_date - datetime.timedelta(days=30)

# Find the closest available dates
def find_closest_date(target, dates_list):
    if not dates_list:
        return None
    return min(dates_list, key=lambda d: abs((d - target).days))

week_compare_date = find_closest_date(one_week_ago, all_dates)
month_compare_date = find_closest_date(one_month_ago, all_dates)

col1, col2 = st.columns(2)

with col1:
    st.markdown(f"**vs. 1 week ago** ({week_compare_date})")
    if week_compare_date and week_compare_date != latest_date:
        show_comparison(latest, df[df["date"] == week_compare_date], meetings_sorted)
    else:
        st.info("Need at least 1 week of archived data.")

with col2:
    st.markdown(f"**vs. 1 month ago** ({month_compare_date})")
    if month_compare_date and month_compare_date != latest_date and (latest_date - month_compare_date).days >= 7:
        show_comparison(latest, df[df["date"] == month_compare_date], meetings_sorted)
    else:
        st.info("Need at least 1 month of archived data.")


# ════════════════════════════════════════════════════════════════════════════
# Panel 5: Change Alerts
# ════════════════════════════════════════════════════════════════════════════
st.header("⑤ Change Alerts")
st.caption(
    "Meetings where the most likely rate probability shifted significantly "
    "compared to the previous day. Threshold: ±10%."
)

# Compare latest vs previous day
if len(all_dates) >= 2:
    prev_date = all_dates[-2]
    prev_data = df[df["date"] == prev_date]

    alerts = []
    for md in meetings_sorted:
        curr_sub = upcoming[upcoming["meeting_date"] == md]
        prev_sub = prev_data[prev_data["meeting_date"] == md]

        if curr_sub.empty or prev_sub.empty:
            continue

        # Compare most likely rate
        curr_max = curr_sub.loc[curr_sub["probability"].idxmax()]
        prev_max = prev_sub.loc[prev_sub["probability"].idxmax()]

        curr_rate = curr_max["rate_range"]
        prev_rate = prev_max["rate_range"]
        curr_prob = curr_max["probability"]
        prev_prob = prev_max["probability"]

        prob_change = curr_prob - prev_prob

        if abs(prob_change) >= 10 or curr_rate != prev_rate:
            alerts.append({
                "meeting": md.strftime("%b %d, %Y") if hasattr(md, "strftime") else str(md),
                "prev_rate": prev_rate,
                "prev_prob": prev_prob,
                "curr_rate": curr_rate,
                "curr_prob": curr_prob,
                "prob_change": prob_change,
                "rate_changed": curr_rate != prev_rate,
            })

    if alerts:
        for a in alerts:
            change_str = f"+{a['prob_change']:.1f}%" if a["prob_change"] > 0 else f"{a['prob_change']:.1f}%"
            color = "🔴" if a["prob_change"] < 0 or a["rate_changed"] else "🟢"

            st.markdown(
                f"{color} **{a['meeting']}**  \n"
                f"Previous: {a['prev_rate']} ({a['prev_prob']:.1f}%) → "
                f"Now: **{a['curr_rate']}** ({a['curr_prob']:.1f}%)  \n"
                f"Change: **{change_str}**"
                f"{' ⚠️ Rate changed!' if a['rate_changed'] else ''}"
            )
    else:
        st.success("No significant changes detected. Market expectations are stable.")
else:
    st.info("Need at least 2 days of data to detect changes.")

# ── Footer ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "Data: Official CME FedWatch Tool, scraped daily. "
    "This is an unofficial tracker for informational purposes only. "
    "Not financial advice."
)
