"""analysis.py — Pure Python berekeningen op Garmin-data (Fase 2).

Architectuurprincipe #2 (REKENEN ≠ AI): alles wat rekenkundig kan, gebeurt
hier zonder AI. De AI krijgt straks alleen de kant-en-klare getallen uit
`analyze_history()` en mag die uitsluitend DUIDEN, niet berekenen. Dat houdt de
cijfers betrouwbaar en de kosten laag.

Deze module importeert geen Streamlit en geen Garmin — puur en los testbaar.
Hij leest alleen `.ok`/`.data` van de MetricResult-objecten (duck-typed), zodat
er geen harde koppeling met de client-laag nodig is.
"""

from __future__ import annotations

from datetime import date, datetime
from statistics import mean, pstdev
from typing import Any, Optional

# Drempels voor "zware sessie" op basis van Garmin Training Effect (schaal 0–5).
# Easy duurlopen zitten rond aerobic TE 2; tempo/drempel/intervallen/races hoger.
HARD_AEROBIC_TE = 3.5
HARD_ANAEROBIC_TE = 2.0


# --------------------------------------------------------------------------- #
# Kleine helpers
# --------------------------------------------------------------------------- #
def _dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
    return cur if cur is not None else default


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _act_date(a: dict) -> Optional[str]:
    s = a.get("startTimeLocal") or ""
    return s[:10] if len(s) >= 10 else None


def _act_load(a: dict) -> float:
    v = a.get("activityTrainingLoad")
    return float(v) if _is_num(v) else 0.0


def _act_km(a: dict) -> float:
    v = a.get("distance")
    return float(v) / 1000.0 if _is_num(v) else 0.0


def _is_run(a: dict) -> bool:
    return "run" in (_dig(a, "activityType", "typeKey", default="") or "")


def _act_datetime(a: dict) -> Optional[datetime]:
    s = a.get("startTimeLocal") or ""
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _is_hard(a: dict) -> bool:
    aero = a.get("aerobicTrainingEffect")
    anaero = a.get("anaerobicTrainingEffect")
    aero = float(aero) if _is_num(aero) else 0.0
    anaero = float(anaero) if _is_num(anaero) else 0.0
    return aero >= HARD_AEROBIC_TE or anaero >= HARD_ANAEROBIC_TE


def most_recent_hard_session(acts: list[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """De meest recente zware training (hoog Training Effect) en hoe lang geleden."""
    now = now or datetime.now()
    best: Optional[dict] = None
    for a in acts:
        if not _is_hard(a):
            continue
        dt = _act_datetime(a)
        if dt is None:
            continue
        hours = (now - dt).total_seconds() / 3600.0
        if hours < 0:
            continue
        if best is None or hours < best["hours_ago"]:
            best = {
                "hours_ago": round(hours, 1),
                "name": a.get("activityName")
                or _dig(a, "activityType", "typeKey", default="training"),
                "aerobic_te": round(float(a.get("aerobicTrainingEffect") or 0), 1),
                "anaerobic_te": round(float(a.get("anaerobicTrainingEffect") or 0), 1),
                "training_load": round(float(a.get("activityTrainingLoad") or 0), 1),
            }
    return best


# --------------------------------------------------------------------------- #
# Extractie uit de history-structuur (client.get_history())
# --------------------------------------------------------------------------- #
def hrv_series(history: dict) -> dict[str, float]:
    """{datum: HRV lastNightAvg} voor de dagen waarop Garmin een meting had."""
    out: dict[str, float] = {}
    for d, r in history.get("hrv", {}).items():
        v = _dig(getattr(r, "data", None), "hrvSummary", "lastNightAvg")
        if _is_num(v):
            out[d] = float(v)
    return out


def sleep_hours_series(history: dict) -> dict[str, float]:
    """{datum: slaapuren} op basis van dailySleepDTO.sleepTimeSeconds."""
    out: dict[str, float] = {}
    for d, r in history.get("sleep", {}).items():
        sec = _dig(getattr(r, "data", None), "dailySleepDTO", "sleepTimeSeconds")
        if _is_num(sec) and sec > 0:
            out[d] = round(sec / 3600.0, 2)
    return out


def activities_list(history: dict) -> list[dict]:
    r = history.get("activities")
    if getattr(r, "ok", False):
        return r.data or []
    return []


def garmin_hrv_status(history: dict) -> Optional[str]:
    """Garmin's eigen HRV-status (bv. BALANCED) van de meest recente meting."""
    for _, r in sorted(history.get("hrv", {}).items(), reverse=True):
        st = _dig(getattr(r, "data", None), "hrvSummary", "status")
        if st:
            return st
    return None


# --------------------------------------------------------------------------- #
# Berekeningen (pure functies op getallen — makkelijk te testen)
# --------------------------------------------------------------------------- #
def analyze_hrv(series: dict[str, float]) -> dict:
    """HRV-baseline (gemiddelde + spreiding) en de afwijking van vandaag."""
    if not series:
        return {"available": False}
    values = [v for _, v in sorted(series.items())]
    current = values[-1]
    baseline = mean(values)
    sd = pstdev(values) if len(values) > 1 else 0.0
    deviation = current - baseline

    # Aaneengesloten reeks recente dagen ónder de baseline (vermoeidheidssignaal).
    streak = 0
    for v in reversed(values):
        if v < baseline:
            streak += 1
        else:
            break

    last7 = values[-7:]
    prev7 = values[-14:-7]
    trend = round(mean(last7) - mean(prev7), 1) if last7 and prev7 else None

    return {
        "available": True,
        "current": round(current, 1),
        "baseline_mean": round(baseline, 1),
        "baseline_sd": round(sd, 1),
        "deviation_from_baseline": round(deviation, 1),
        "z_score": round(deviation / sd, 2) if sd > 0 else None,
        "days_below_baseline_streak": streak,
        "trend_7d_vs_prev7": trend,
        "n_days": len(values),
    }


def analyze_sleep(series: dict[str, float]) -> dict:
    """Slaaptrend: laatste nacht, 7- en 28-daags gemiddelde, korte nachten."""
    if not series:
        return {"available": False}
    values = [v for _, v in sorted(series.items())]
    last7 = values[-7:]
    last28 = values[-28:]
    return {
        "available": True,
        "last_night_h": round(values[-1], 2),
        "avg_7d_h": round(mean(last7), 2),
        "avg_28d_h": round(mean(last28), 2),
        "short_nights_under_7h_last7": sum(1 for v in last7 if v < 7.0),
        "trend_7d_vs_28d_h": round(mean(last7) - mean(last28), 2),
        "n_nights": len(values),
    }


def _acwr_zone(acwr: Optional[float]) -> str:
    """Standaard ACWR-zones (Gabbett): sweet spot ~0.8–1.3."""
    if acwr is None:
        return "onbekend"
    if acwr < 0.8:
        return "laag (detraining-risico)"
    if acwr <= 1.3:
        return "optimaal (sweet spot)"
    if acwr <= 1.5:
        return "verhoogd"
    return "hoog (blessurerisico)"


def analyze_acwr(acts: list[dict], end: Optional[date] = None) -> dict:
    """Ruwe acute:chronische belasting op basis van Garmin trainingsbelasting.

    acute   = som van de trainingsbelasting over de laatste 7 dagen.
    chronic = gemiddelde wekelijkse belasting over de laatste 28 dagen.
    """
    end = end or date.today()
    by_day: dict[str, float] = {}
    for a in acts:
        d = _act_date(a)
        if d:
            by_day[d] = by_day.get(d, 0.0) + _act_load(a)

    def load_window(days_back_start: int, days_back_end: int) -> float:
        total = 0.0
        for d, load in by_day.items():
            dd = (end - date.fromisoformat(d)).days
            if days_back_start <= dd <= days_back_end:
                total += load
        return total

    acute = load_window(0, 6)
    chronic_total = load_window(0, 27)
    chronic_weekly = chronic_total / 4.0
    acwr = round(acute / chronic_weekly, 2) if chronic_weekly > 0 else None

    return {
        "available": chronic_weekly > 0,
        "acute_load_7d": round(acute, 1),
        "chronic_weekly_load": round(chronic_weekly, 1),
        "acwr": acwr,
        "zone": _acwr_zone(acwr),
    }


def analyze_volume(acts: list[dict], end: Optional[date] = None, weeks: int = 4) -> dict:
    """Volume per 7-daagse blok: afstand (km), trainingsbelasting, aantal runs."""
    end = end or date.today()
    buckets = []
    for w in range(weeks):
        start_back, end_back = w * 7, w * 7 + 6
        km = load = 0.0
        runs = 0
        for a in acts:
            d = _act_date(a)
            if not d:
                continue
            dd = (end - date.fromisoformat(d)).days
            if start_back <= dd <= end_back:
                km += _act_km(a)
                load += _act_load(a)
                if _is_run(a):
                    runs += 1
        label = "afgelopen 7 dagen" if w == 0 else f"{start_back + 1}-{end_back + 1} dagen geleden"
        buckets.append(
            {"label": label, "km": round(km, 1), "training_load": round(load, 1), "runs": runs}
        )
    return {"weeks": buckets}


# --------------------------------------------------------------------------- #
# Dagelijkse readiness (Fase 3) — pure drempelwaarden, geen AI
# --------------------------------------------------------------------------- #
# Nederlandse labels + betekenis van het stoplicht (één bron voor app + coach).
READINESS_LABELS = {"green": "GROEN", "amber": "ORANJE", "red": "ROOD"}
READINESS_MEANING = {
    "green": "hersteld — ruimte voor een zware of intensieve training",
    "amber": "gemengde signalen of recente zware sessie — pas de intensiteit aan, vaak easy",
    "red": "onderherstel — rust of zeer rustig",
}



def analyze_readiness(
    history: dict,
    today_summary,
    end: Optional[date] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Bepaal een go/no-go-stoplicht voor vandaag uit de berekende signalen.

    `today_summary` is een MetricResult (of dict) van de dagsamenvatting van
    vandaag. Het stoplicht beschermt tegen te hard trainen bij onderherstel én
    benoemt eerlijk als er juist ruimte is.
    """
    end = end or date.today()
    summary = getattr(today_summary, "data", today_summary) or {}

    hrv = analyze_hrv(hrv_series(history))
    sleep_series = sleep_hours_series(history)
    sleep_values = [v for _, v in sorted(sleep_series.items())]
    last_sleep = sleep_values[-1] if sleep_values else None
    sleep_avg7 = round(mean(sleep_values[-7:]), 2) if sleep_values else None

    bb_wake = summary.get("bodyBatteryAtWakeTime")
    rhr = summary.get("restingHeartRate")
    rhr_base = summary.get("lastSevenDaysAvgRestingHeartRate")
    rhr_delta = (rhr - rhr_base) if (_is_num(rhr) and _is_num(rhr_base)) else None

    acts = activities_list(history)
    acwr = analyze_acwr(acts, end)
    hard = most_recent_hard_session(acts, now)

    red: list[str] = []
    amber: list[str] = []
    notes: list[str] = []

    # HRV
    if hrv.get("available"):
        z = hrv.get("z_score")
        streak = hrv.get("days_below_baseline_streak", 0)
        if (z is not None and z <= -1.5) or streak >= 4:
            red.append(f"HRV diep onder baseline (z={z}, {streak} dagen onder).")
        elif (z is not None and z <= -0.7) or streak >= 2:
            amber.append(f"HRV onder baseline (z={z}, {streak} dagen onder).")
        elif z is not None and z >= 1.0:
            amber.append("HRV duidelijk boven baseline — goed hersteld.")

    # Slaap (laatste nacht)
    if last_sleep is not None:
        if last_sleep < 5.5:
            red.append(f"Zeer korte nacht ({last_sleep} u).")
        elif last_sleep < 7.0:
            amber.append(f"Korte nacht ({last_sleep} u).")

    # Rust-hartslag vs. 7-daagse baseline
    if rhr_delta is not None:
        if rhr_delta >= 5:
            red.append(f"Rust-HS {rhr_delta} hoger dan je weekgemiddelde.")
        elif rhr_delta >= 3:
            amber.append(f"Rust-HS {rhr_delta} hoger dan je weekgemiddelde.")

    # Body Battery bij ontwaken
    if _is_num(bb_wake):
        if bb_wake < 40:
            red.append(f"Body Battery laag bij ontwaken ({bb_wake}).")
        elif bb_wake < 60:
            amber.append(f"Body Battery matig bij ontwaken ({bb_wake}).")

    # Acute:chronische belasting
    if acwr.get("available"):
        ratio = acwr.get("acwr")
        if ratio is not None and ratio > 1.5:
            red.append(f"Belasting hoog (ACWR {ratio}).")
        elif ratio is not None and (ratio > 1.3 or ratio < 0.8):
            amber.append(f"ACWR {ratio} — {acwr.get('zone')}.")

    # Recente zware sessie (Training Effect): herstelcijfers mogen vandaag niet
    # "groen, ram erop" zeggen vlak na een harde inspanning.
    if hard:
        h = hard["hours_ago"]
        te = f"Training Effect {hard['aerobic_te']}/{hard['anaerobic_te']}"
        if h <= 24:
            amber.append(
                f"Zware sessie pas {h:.0f}u geleden ({hard['name']}, {te}) — "
                "vandaag easy of rust, ongeacht je herstelcijfers."
            )
        elif h <= 48:
            notes.append(
                f"Zware sessie ~{h:.0f}u geleden ({hard['name']}, {te}) — "
                "bouw vandaag rustig op."
            )

    if red:
        light = "red"
    elif amber:
        light = "amber"
    else:
        light = "green"

    base_reasons = red + amber if (red or amber) else ["Alle signalen in orde."]

    return {
        "date": end.isoformat(),
        "light": light,
        "reasons": base_reasons + notes,
        "signals": {
            "last_hard_session": hard,
            "hrv": hrv,
            "sleep_last_night_h": last_sleep,
            "sleep_avg_7d_h": sleep_avg7,
            "body_battery_at_wake": bb_wake if _is_num(bb_wake) else None,
            "resting_hr": rhr if _is_num(rhr) else None,
            "resting_hr_baseline_7d": rhr_base if _is_num(rhr_base) else None,
            "resting_hr_delta": rhr_delta,
            "acwr": acwr.get("acwr"),
            "acwr_zone": acwr.get("zone"),
        },
    }


# --------------------------------------------------------------------------- #
# Alles samen → input voor de AI
# --------------------------------------------------------------------------- #
def analyze_history(history: dict, end: Optional[date] = None) -> dict:
    """Bereken alle metrics uit een client.get_history()-resultaat."""
    end = end or date.today()
    hrv = hrv_series(history)
    sleep = sleep_hours_series(history)
    acts = activities_list(history)
    return {
        "end_date": end.isoformat(),
        "hrv": {**analyze_hrv(hrv), "garmin_status": garmin_hrv_status(history)},
        "sleep": analyze_sleep(sleep),
        "acwr": analyze_acwr(acts, end),
        "volume": analyze_volume(acts, end),
    }
