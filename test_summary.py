"""Test script: render per-meeting summary with mock data via Streamlit."""
import streamlit as st
import pandas as pd
import numpy as np

st.set_page_config(page_title="PM Summary Test", layout="wide")

# ── CSS (copied from app.py) ──────────────────────────────────────────
st.markdown("""
<style>
.pm-card {
    background: #FFFFFF;
    border: 1px solid #E0E8F0;
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 8px;
    border-left: 3px solid #1565C0;
}
.pm-meeting {
    font-weight: 700;
    color: #0D47A1;
    font-size: 1rem;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
}
.pm-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: #546E7A;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 6px;
    margin-bottom: 3px;
}
.pm-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
}
.pm-chip {
    display: inline-flex;
    align-items: center;
    padding: 2px 9px;
    border-radius: 12px;
    font-size: 0.82rem;
    font-weight: 600;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    white-space: nowrap;
}
.pm-chip-up {
    background: #E8F5E9;
    color: #1B7A3E;
    border: 1px solid #A5D6A7;
}
.pm-chip-down {
    background: #FFEBEE;
    color: #C0392B;
    border: 1px solid #EF9A9A;
}
.pm-chip-hot {
    background: linear-gradient(135deg, #FFF8E1 0%, #FFECB3 100%);
    color: #E65100 !important;
    border: 1px solid #FFB74D;
    font-weight: 700;
}
.pm-chip-hot::after {
    content: ' \U0001f525';
    font-size: 0.75rem;
}
.pm-badge-hot {
    display: inline-block;
    background: linear-gradient(135deg, #E65100, #FF6F00);
    color: white;
    font-size: 0.68rem;
    font-weight: 800;
    padding: 1px 7px;
    border-radius: 8px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-left: auto;
}
.pm-empty {
    color: #B0BEC5;
    font-size: 0.82rem;
    font-style: italic;
}
</style>
""", unsafe_allow_html=True)

st.title("Per-meeting Summary Test")

# ── Mock data with various edge cases ─────────────────────────────────
alert_df = pd.DataFrame([
    {"meeting_date": pd.Timestamp("2026-07-29"), "meeting": "Jul 29, 2026", "range": "325-350", "current": 30.0, "1d_delta": -16.7, "1w_delta": None},
    {"meeting_date": pd.Timestamp("2026-09-16"), "meeting": "Sep 16, 2026", "range": "350-375", "current": 56.0, "1d_delta": 8.2, "1w_delta": 18.5},
    {"meeting_date": pd.Timestamp("2026-09-16"), "meeting": "Sep 16, 2026", "range": "375-400", "current": 25.4, "1d_delta": -6.1, "1w_delta": -22.0},
    {"meeting_date": pd.Timestamp("2026-10-28"), "meeting": "Oct 28, 2026", "range": "350-375", "current": 44.0, "1d_delta": None, "1w_delta": 12.3},
    {"meeting_date": pd.Timestamp("2026-12-09"), "meeting": "Dec 09, 2026", "range": "375-400", "current": 15.0, "1d_delta": 5.0, "1w_delta": 5.0},
])

# Also test with NaN (what pandas actually produces from None)
alert_df_nan = pd.DataFrame([
    {"meeting_date": pd.Timestamp("2026-07-29"), "meeting": "Jul 29, 2026", "range": "325-350", "current": 30.0, "1d_delta": np.nan, "1w_delta": np.nan},
])

st.subheader("Test 1: Normal data with various deltas")
st.markdown("#### Per-meeting summary")
st.caption("Green = positive change (probability up). Red = negative change (probability down). 🔥 = change >= 15%.")

def _chip_html(range_name, delta_value):
    if delta_value is None:
        return ""
    direction = "up" if delta_value >= 0 else "down"
    is_hot = abs(delta_value) >= 15
    sign = "+" if delta_value >= 0 else ""
    label = f"{range_name} {sign}{delta_value:.1f}%"
    dir_class = f"pm-chip-{direction}"
    hot_class = " pm-chip-hot" if is_hot else ""
    return f'<span class="pm-chip {dir_class}{hot_class}">{label}</span>'

sorted_dates = sorted(alert_df["meeting_date"].unique())

for md in sorted_dates:
    g = alert_df[alert_df["meeting_date"] == md]
    if g.empty:
        continue
    meeting_label = g["meeting"].iloc[0]

    d_chips = []
    w_chips = []
    has_hot = False
    for _, r in g.iterrows():
        if r["1d_delta"] is not None and abs(r["1d_delta"]) >= 5:
            d_chips.append(_chip_html(r["range"], r["1d_delta"]))
            if abs(r["1d_delta"]) >= 15:
                has_hot = True
        if r["1w_delta"] is not None and abs(r["1w_delta"]) >= 5:
            w_chips.append(_chip_html(r["range"], r["1w_delta"]))
            if abs(r["1w_delta"]) >= 15:
                has_hot = True

    hot_badge = '<span class="pm-badge-hot">🔥 HOT</span>' if has_hot else ''

    d_html = (
        '<div class="pm-chips">' + "".join(d_chips) + '</div>'
        if d_chips else '<span class="pm-empty">\u2014</span>'
    )
    w_html = (
        '<div class="pm-chips">' + "".join(w_chips) + '</div>'
        if w_chips else '<span class="pm-empty">\u2014</span>'
    )

    card_html = (
        '\n                <div class="pm-card">\n'
        '                  <div class="pm-meeting">\n'
        f'                    {meeting_label}\n'
        f'                    {hot_badge}\n'
        '                  </div>\n'
        '                  <div class="pm-label">vs 1 Day Ago</div>\n'
        f'                  {d_html}\n'
        '                  <div class="pm-label">vs 1 Week Ago</div>\n'
        f'                  {w_html}\n'
        '                </div>'
    )
    st.markdown(card_html, unsafe_allow_html=True)

st.subheader("Test 2: NaN edge case")
st.write("Testing with NaN values (what pandas produces from None in DataFrame):")
for _, r in alert_df_nan.iterrows():
    st.write(f"1d_delta = {r['1d_delta']}, is not None: {r['1d_delta'] is not None}, abs >= 5: {abs(r['1d_delta']) >= 5 if not pd.isna(r['1d_delta']) else 'NaN'}")

st.success("If you can see this, the test passed!")
