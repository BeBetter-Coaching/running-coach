"""charts.py — Interactieve Altair-grafieken voor het dashboard.

Pure functies (geen Streamlit): elke functie krijgt kant-en-klare data en geeft
een Altair-chart terug. Kleuren zijn afgestemd op het BeBetter-thema. Lege data
geeft None terug, zodat de app dat netjes kan afvangen.
"""

from __future__ import annotations

from typing import Optional

import altair as alt
import pandas as pd

# BeBetter-palet
CYAN = "#5EE6EB"
BLUE = "#2876FB"
GOLD = "#FAC775"
TEXT = "#EAF2FF"
MUTED = "#8FA8CE"
GREEN = "#3BD16F"
RED = "#EB5E5E"


def hrv_chart(
    hrv_by_date: dict[str, float],
    baseline_mean: Optional[float] = None,
    baseline_sd: Optional[float] = None,
) -> Optional[alt.LayerChart]:
    if not hrv_by_date:
        return None
    df = pd.DataFrame(
        [{"datum": pd.to_datetime(d), "HRV": v} for d, v in sorted(hrv_by_date.items())]
    )
    layers = []
    if baseline_mean is not None and baseline_sd:
        band = pd.DataFrame(
            {
                "datum": df["datum"],
                "low": baseline_mean - baseline_sd,
                "high": baseline_mean + baseline_sd,
            }
        )
        layers.append(
            alt.Chart(band)
            .mark_area(opacity=0.15, color=BLUE)
            .encode(x="datum:T", y="low:Q", y2="high:Q")
        )
        layers.append(
            alt.Chart(pd.DataFrame({"y": [baseline_mean]}))
            .mark_rule(color=MUTED, strokeDash=[4, 4])
            .encode(y="y:Q")
        )
    layers.append(
        alt.Chart(df)
        .mark_line(point=True, color=CYAN, strokeWidth=2)
        .encode(
            x=alt.X("datum:T", title=None),
            y=alt.Y("HRV:Q", scale=alt.Scale(zero=False), title="HRV (ms)"),
            tooltip=[alt.Tooltip("datum:T", title="Datum"), alt.Tooltip("HRV:Q")],
        )
    )
    return alt.layer(*layers).properties(height=240, width="container")


def sleep_chart(sleep_by_date: dict[str, float]) -> Optional[alt.LayerChart]:
    if not sleep_by_date:
        return None
    df = pd.DataFrame(
        [{"datum": pd.to_datetime(d), "uren": v} for d, v in sorted(sleep_by_date.items())]
    )
    df["gem"] = df["uren"].rolling(7, min_periods=1).mean()
    bars = alt.Chart(df).mark_bar(color=BLUE, opacity=0.85).encode(
        x=alt.X("datum:T", title=None),
        y=alt.Y("uren:Q", title="Slaap (u)"),
        tooltip=[alt.Tooltip("datum:T", title="Datum"), alt.Tooltip("uren:Q", format=".1f", title="Uren")],
    )
    avg = alt.Chart(df).mark_line(color=CYAN, strokeWidth=2).encode(x="datum:T", y="gem:Q")
    target = (
        alt.Chart(pd.DataFrame({"y": [7]}))
        .mark_rule(color=GOLD, strokeDash=[4, 4])
        .encode(y="y:Q")
    )
    return alt.layer(bars, avg, target).properties(height=240, width="container")


def weekly_bar(weeks: list[dict], key: str, title: str, color: str = CYAN) -> Optional[alt.Chart]:
    if not weeks:
        return None
    rows = [
        {"week": w.get("label", ""), "order": i, "waarde": w.get(key, 0)}
        for i, w in enumerate(reversed(weeks))  # oudste links
    ]
    df = pd.DataFrame(rows)
    return (
        alt.Chart(df)
        .mark_bar(color=color, cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("week:N", sort=alt.SortField("order"), title=None, axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("waarde:Q", title=title),
            tooltip=[alt.Tooltip("week:N", title="Periode"), alt.Tooltip("waarde:Q", title=title, format=".1f")],
        )
        .properties(height=240, width="container")
    )


# (ACWR wordt nu als begrijpelijke '% van je normale week'-balk in de app
#  getoond, zie render_load_meter in app.py — geen Altair-chart meer.)
