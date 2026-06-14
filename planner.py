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
    # 90 km is de harde ondergrens. Periodisering loopt via INTENSITEIT, niet
    # via volume: ook deload-weken houden de omvang op ~basis.
    base = max(90, round(current_weekly_km or 90))

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
        km = base  # volume blijft op de basis; we sturen op intensiteit
        if vac:
            fase = "Vakantie – herstel"
            focus = "Rustig Z1–Z2, intensiteit eruit (max 12 km/dag)"
            notitie = vac[2].get("notitie", "")
        elif races_this:
            fase = f"Wedstrijd: {races_this[0].get('naam', '')}"
            focus = "Race als kwaliteit, rest van de week rustig"
        elif weeks_to_a is not None and weeks_to_a <= 1:
            fase = "Taper (10K)"
            focus = "Volume op basis houden, scherpte/intensiteit eruit, fris worden"
        elif weeks_to_a is not None and weeks_to_a <= 3:
            fase = "Scherpen (10K)"
            focus = "10K-racetempo + drempel (Z3/Z4)"
            build_count += 1
        elif near_b:
            fase = "Scherpen (baan/3000m)"
            focus = "VO2 / baan-intervallen, snelheid"
            build_count += 1
        else:
            fase = "Opbouw"
            focus = "Drempel (Z3), volume op basis houden"
            build_count += 1

        # Deload elke 4e opbouw/scherp-week: INTENSITEIT omlaag, omvang blijft ~basis.
        if fase in ("Opbouw", "Scherpen (10K)", "Scherpen (baan/3000m)") and build_count % 4 == 0:
            fase += " · deload (intensiteit ↓)"
            focus = "Rustige aerobe week — volume op basis, weinig/geen kwaliteit"

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
