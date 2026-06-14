"""planner.py — Lange-termijn skelet / periodisering (Fase B).

Pure Python (geen AI, geen Streamlit), conform principe REKENEN ≠ AI: dit bepaalt
de STRUCTUUR (week-voor-week fase, doel-weekvolume, focus) op basis van de races,
de kalender (vakanties) en het actuele weekvolume uit Garmin. De concrete
trainingen per week (Fase C) komen later, mét AI en het weekstramien van Jip.

Het skelet is bewust transparant en heuristisch — een eerste, leesbaar voorstel
dat de gebruiker kan bijsturen.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional


def parse_date(s) -> Optional[date]:
    """Tolerante datum-parser: accepteert o.a. YYYY-MM-DD en YYYYMMDD."""
    s = str(s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _vacation_weeks(kalender: list[dict]) -> list[tuple[date, date, dict]]:
    out = []
    rust_types = {"vakantie", "lichte week", "niet beschikbaar"}
    for k in kalender or []:
        if (k.get("type", "") or "").strip().lower() in rust_types:
            v_from = parse_date(k.get("van"))
            v_to = parse_date(k.get("tot"))
            if v_from and v_to:
                out.append((v_from, v_to, k))
    return out


def build_skeleton(
    plan: dict, current_weekly_km: float, today: Optional[date] = None
) -> dict:
    """Bouw het week-voor-week skelet van nu tot de laatste race.

    Geeft terug: {"base_km", "races", "weken": [ {week_start, fase, doel_km,
    focus, races, notitie} ]}.
    """
    today = today or date.today()
    base = max(20, round(current_weekly_km or 60))

    races = []
    for r in plan.get("races", []) or []:
        d = parse_date(r.get("datum"))
        if d and d >= today:
            races.append({**r, "_date": d})
    races.sort(key=lambda x: x["_date"])
    vacations = _vacation_weeks(plan.get("kalender", []))

    horizon_end = races[-1]["_date"] if races else today + timedelta(weeks=8)
    monday = today - timedelta(days=today.weekday())

    weeken: list[dict] = []
    build_count = 0
    cur = monday
    while cur <= horizon_end:
        w_start, w_end = cur, cur + timedelta(days=6)
        races_this = [r for r in races if w_start <= r["_date"] <= w_end]
        vac = next((v for v in vacations if not (v[1] < w_start or v[0] > w_end)), None)

        next_a = next(
            (r for r in races if (r.get("prioriteit", "").upper() == "A") and r["_date"] >= w_start),
            None,
        )
        weeks_to_a = ((next_a["_date"] - w_start).days // 7) if next_a else None
        near_b = next(
            (
                r
                for r in races
                if r.get("prioriteit", "").upper() == "B"
                and 0 <= (r["_date"] - w_start).days <= 21
            ),
            None,
        )

        notitie = ""
        if vac:
            fase = "Vakantie – herstel/onderhoud"
            km = min(round(base * 0.7), 70)
            focus = "Rustig, Z1–Z2"
            notitie = vac[2].get("notitie", "")
        elif races_this:
            r0 = races_this[0]
            prio = r0.get("prioriteit", "").upper()
            fase = f"Wedstrijd: {r0.get('naam', '')}"
            km = round(base * (0.6 if prio == "A" else 0.8))
            focus = "Scherp aanzetten, racen, daarna herstel"
        elif weeks_to_a is not None and weeks_to_a <= 1:
            fase = "Taper (10K)"
            km = round(base * 0.6)
            focus = "Volume omlaag, kort racetempo aanzetten"
        elif weeks_to_a is not None and weeks_to_a <= 3:
            fase = "Scherpen (10K)"
            km = base
            focus = "10K-racetempo + drempel (Z3/Z4)"
            build_count += 1
        elif near_b:
            fase = "Scherpen (baan/3000m)"
            km = base
            focus = "VO2 / baan-intervallen, snelheid"
            build_count += 1
        else:
            fase = "Opbouw"
            km = base
            focus = "Drempel (Z3) + volume vasthouden"
            build_count += 1

        # Herstelweek elke 4e opbouw/scherp-week (niet in race/vakantie/taper).
        if fase in ("Opbouw", "Scherpen (10K)", "Scherpen (baan/3000m)") and build_count % 4 == 0:
            fase += " · herstelweek"
            km = round(base * 0.75)

        weeken.append(
            {
                "week_start": w_start.isoformat(),
                "fase": fase,
                "doel_km": km,
                "focus": focus,
                "races": [r.get("naam", "") for r in races_this],
                "notitie": notitie,
            }
        )
        cur += timedelta(days=7)

    return {
        "base_km": base,
        "races": [
            {"naam": r.get("naam", ""), "datum": r["_date"].isoformat(), "prioriteit": r.get("prioriteit", "")}
            for r in races
        ],
        "weken": weeken,
    }
