"""AI Hardloopcoach — Streamlit-app.

Twee pagina's:
- Coach-rapport (Fase 2): Python berekent HRV-baseline/afwijking, slaaptrend,
  ruwe ACWR en weekvolume; één AI-call duidt die getallen tot een wekelijks
  coach-rapport.
- Ruwe data (Fase 1): de ruwe Garmin-velden van de laatste 7 dagen.

Alle Garmin-aanroepen lopen via garmin_client.GarminClient (de client-laag);
alle berekeningen via analysis.py; de duiding via coach.py.
"""

from datetime import date, timedelta
from typing import Any, Optional

import streamlit as st

from garmin_client import GarminClient, GarminClientError
import pandas as pd

import analysis
import publish
import charts
import store
import planner
from coach import (
    generate_coach_report,
    generate_week_plan,
    adjust_today,
    build_flags,
    daily_readiness_advice,
    readiness_template,
    CoachError,
)

st.set_page_config(
    page_title="AI Hardloopcoach",
    page_icon="🏃",
    layout="wide",
)


# --------------------------------------------------------------------------- #
# Secrets & client
# --------------------------------------------------------------------------- #
def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def _check_password() -> bool:
    """Eenvoudig wachtwoord-slot voor de online (openbaar bereikbare) app.

    Lokaal of zonder `app_password`-secret blijft alles open. Online beschermt
    dit je Garmin-data + API-budget, ook al is de URL openbaar.
    """
    expected = get_secret("app_password")
    if not expected:
        return True
    if st.session_state.get("_auth_ok"):
        return True
    st.title("🔒 Hardloopcoach")
    pw = st.text_input("Wachtwoord", type="password")
    if pw:
        if pw == expected:
            st.session_state["_auth_ok"] = True
            st.rerun()
        else:
            st.error("Onjuist wachtwoord.")
    return False


if not _check_password():
    st.stop()


@st.cache_resource(show_spinner=False)
def get_client() -> GarminClient:
    return GarminClient(
        email=get_secret("garmin_email"),
        password=get_secret("garmin_password"),
        token_b64=get_secret("garmin_token_b64"),
    )


# cache_resource (niet cache_data): de resultaten bevatten MetricResult-objecten,
# die cache_data niet kan serialiseren. cache_resource bewaart het object zelf.
@st.cache_resource(ttl=3600, show_spinner="Garmin-data ophalen…")
def load_week() -> dict:
    return get_client().get_last_7_days()


@st.cache_resource(ttl=3600, show_spinner="Garmin-historie ophalen (28 dagen)…")
def load_history(days: int = 28) -> dict:
    return get_client().get_history(days=days)


@st.cache_resource(ttl=3600, show_spinner="Readiness-data ophalen…")
def load_readiness(days: int = 28) -> dict:
    return get_client().get_readiness_inputs(days=days)


# Bij een nieuwe dag automatisch verse data: wis de caches + dagelijkse AI-uitvoer
# de eerste keer dat de app op een nieuwe kalenderdag draait. Zo is 's ochtends
# alles vers zonder dat je iets hoeft te klikken.
_today_iso = date.today().isoformat()
if st.session_state.get("_cache_day") != _today_iso:
    load_week.clear()
    load_history.clear()
    load_readiness.clear()
    for _k in [k for k in st.session_state if k.startswith(("adjust_", "readiness_advice_"))]:
        del st.session_state[_k]
    st.session_state["_cache_day"] = _today_iso


# --------------------------------------------------------------------------- #
# Hulpjes
# --------------------------------------------------------------------------- #
def dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
    return cur if cur is not None else default


def fmt(value: Any, suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "—"


def chart_or_caption(chart, empty: str = "—") -> None:
    """Toon een Altair-chart, of een nette caption als er geen data is.

    Bewust een functie (geen ternaire expressie): een kale `A if c else B`-regel
    wordt door Streamlit's magic naar het scherm geschreven; een functie-aanroep
    niet.
    """
    if chart is not None:
        st.altair_chart(chart, width="stretch")
    else:
        st.caption(empty)


def _load_phrase(acwr: Optional[float]) -> str:
    if acwr is None:
        return "—"
    if acwr < 0.8:
        return "rustiger dan normaal"
    if acwr <= 1.3:
        return "in balans (veilige opbouw)"
    if acwr <= 1.5:
        return "snelle opbouw — let op"
    return "grote sprong — blessurerisico"


def render_load_meter(acwr: Optional[float]) -> None:
    """Begrijpelijke weergave van de acute:chronische belasting (ACWR).

    Vertaald naar '% van je normale week' met de 100%-baseline en de veilige
    zone. Een robuuste HTML-balk i.p.v. een fragiele chart.
    """
    if acwr is None:
        st.caption("Nog niet genoeg data voor de belasting.")
        return
    pct = round(acwr * 100)
    pos = max(0.0, min(100.0, acwr / 2 * 100))
    st.markdown(f"**{pct}% van je normale week** · {_load_phrase(acwr)}")
    bar = (
        '<div style="position:relative;height:30px;border-radius:6px;overflow:hidden;display:flex;">'
        f'<div style="width:40%;background:{charts.MUTED};"></div>'
        f'<div style="width:25%;background:{charts.GREEN};"></div>'
        f'<div style="width:10%;background:{charts.GOLD};"></div>'
        f'<div style="width:25%;background:{charts.RED};"></div>'
        '<div style="position:absolute;left:50%;top:0;bottom:0;border-left:2px dashed #EAF2FF;opacity:.6;"></div>'
        f'<div style="position:absolute;left:{pos:.0f}%;top:0;bottom:0;border-left:3px solid #FFFFFF;"></div>'
        '</div>'
        f'<div style="display:flex;justify-content:space-between;font-size:0.72rem;color:{charts.MUTED};margin-top:6px;">'
        '<span>rustiger</span><span>↑ normaal (100%)</span><span>risico</span></div>'
    )
    st.markdown(bar, unsafe_allow_html=True)
    st.caption("100% = je gemiddelde week (laatste 4 weken). 80–130% is de veilige zone.")


def render_raw(label: str, result) -> None:
    icon = "✅" if result.ok else "⚠️"
    cache_note = " · uit cache" if getattr(result, "from_cache", False) else ""
    with st.expander(f"{icon} {label}{cache_note}", expanded=False):
        if result.ok:
            if result.data in (None, [], {}):
                st.caption("Garmin gaf geen data terug voor deze dag.")
            else:
                st.json(result.data, expanded=False)
        else:
            st.error(result.error)


# --------------------------------------------------------------------------- #
# Sidebar + setup-check
# --------------------------------------------------------------------------- #
st.sidebar.title("🏃 Hardloopcoach")
page = st.sidebar.radio(
    "Weergave",
    ["🟢 Vandaag", "📈 Dashboard", "🎯 Planning", "🗓️ Weekplan", "🧠 Coach-rapport", "📊 Ruwe data (7 dagen)"],
)

if st.sidebar.button("🔄 Data verversen (vandaag)"):
    try:
        get_client().clear_cache(date.today().isoformat())
    except Exception:
        pass
    load_week.clear()
    load_history.clear()
    load_readiness.clear()
    # Ook de dagelijkse AI-uitvoer wissen, zodat readiness-advies en bijsturing
    # opnieuw berekend worden met de verse data.
    for _k in [k for k in st.session_state if k.startswith(("adjust_", "readiness_advice_"))]:
        del st.session_state[_k]
    st.rerun()

st.sidebar.divider()

has_login = bool(
    (get_secret("garmin_email") and get_secret("garmin_password"))
    or get_secret("garmin_token_b64")
)

if not has_login:
    st.title("AI Hardloopcoach")
    st.warning("Nog geen Garmin-inloggegevens gevonden.")
    st.markdown(
        """
**Zo zet je ze veilig klaar (lokaal):**

1. Kopieer `.streamlit/secrets.toml.example` naar `.streamlit/secrets.toml`.
2. Vul `garmin_email` en `garmin_password` in.
3. Herlaad deze pagina.

Voor Streamlit Cloud: draai eenmalig `python scripts/garmin_login.py` en zet de
`garmin_token_b64` in **Settings → Secrets**.
        """
    )
    st.stop()

# Login-status in de zijbalk.
try:
    name = get_client().get_display_name()
    st.sidebar.success(f"Ingelogd als {name}" if name else "Sessie actief")
except GarminClientError as e:
    st.sidebar.error("Inloggen mislukt")
    st.error(f"Kon niet bij Garmin inloggen: {e}")
    st.stop()
except Exception as e:  # noqa: BLE001
    st.sidebar.error("Inloggen mislukt")
    st.error(f"Onverwachte fout bij inloggen: {type(e).__name__}: {e}")
    st.stop()


# --------------------------------------------------------------------------- #
# Pagina: Vandaag — readiness (Fase 3)
# --------------------------------------------------------------------------- #
def render_today_page() -> None:
    st.title("Vandaag — mag je hard?")
    inputs = load_readiness(28)
    readiness = analysis.analyze_readiness(
        inputs["history"], inputs["today_summary"], inputs.get("today_readiness")
    )
    light = readiness["light"]
    sig = readiness["signals"]

    banners = {
        "green": (st.success, "🟢 GROEN — hersteld, ruimte om te trainen"),
        "amber": (st.warning, "🟡 ORANJE — let op, pas aan op gevoel"),
        "red": (st.error, "🔴 ROOD — onderherstel, houd het rustig"),
    }
    banner_fn, banner_text = banners[light]
    banner_fn(banner_text)

    # Advies: groen/rood gratis (templated); oranje via goedkope Haiku-call.
    api_key = get_secret("anthropic_api_key")
    state_key = f"readiness_advice_{readiness['date']}"
    if light == "amber":
        if api_key:
            if state_key not in st.session_state:
                with st.spinner("Coach weegt de signalen…"):
                    try:
                        st.session_state[state_key] = daily_readiness_advice(
                            readiness, api_key
                        )
                    except CoachError as e:
                        st.session_state[state_key] = readiness_template(readiness)
                        st.caption(f"(AI-advies niet beschikbaar: {e})")
            st.markdown(f"**{st.session_state[state_key]}**")
        else:
            st.markdown(f"**{readiness_template(readiness)}**")
            st.caption("Voeg `anthropic_api_key` toe voor een op maat geduid advies.")
    else:
        st.markdown(f"**{readiness_template(readiness)}**")

    # Vandaag geplande sessie uit het weekplan + dagelijkse bijsturing.
    gh_token = get_secret("GH_TOKEN")
    athlete_key = get_secret("fs_user_key")
    if athlete_key:
        today = date.today()
        wk_monday = (today - timedelta(days=today.weekday())).isoformat()
        wp = store.load_weekplan(athlete_key, wk_monday, gh_token)
        today_day = next(
            (d for d in wp.get("dagen", []) if d.get("datum") == today.isoformat()), None
        )
        if today_day:
            st.subheader("Vandaag gepland")
            st.markdown(
                f"**{str(today_day.get('dag', '')).capitalize()}** — {today_day.get('sessie', '')}"
            )
            adj_key = f"adjust_{today.isoformat()}"
            if light == "green":
                st.caption("✅ Readiness groen — ga zoals gepland.")
            elif api_key:
                if adj_key not in st.session_state:
                    with st.spinner("Coach checkt je sessie tegen je readiness…"):
                        try:
                            st.session_state[adj_key] = adjust_today(
                                today_day.get("sessie", ""),
                                readiness,
                                store.load_plan(athlete_key, gh_token),
                                api_key,
                            )
                        except CoachError as e:
                            st.session_state[adj_key] = ""
                            st.caption(f"(bijsturing niet beschikbaar: {e})")
                if st.session_state.get(adj_key):
                    st.info(f"🔧 Bijsturing: {st.session_state[adj_key]}")
            else:
                st.caption("Voeg `anthropic_api_key` toe voor dagelijkse bijsturing.")

    with st.expander("ℹ️ Wat betekent dit stoplicht?"):
        st.markdown(
            "**🟢 Groen** — hersteld; ruimte voor een zware of intensieve training.\n\n"
            "**🟡 Oranje** — gemengde signalen óf een recente zware sessie/wedstrijd; "
            "pas de intensiteit aan, vaak een rustige duurloop.\n\n"
            "**🔴 Rood** — onderherstel; rust of zeer rustig.\n\n"
            "Het stoplicht volgt **Garmin's eigen Training Readiness** (de score die "
            "je ook op je horloge ziet). Je **HRV**, **slaap**, **rust-HS**, **Body "
            "Battery**, **ACWR** en recente **zware sessies** staan eronder als "
            "context. Komt Garmin's score er niet door, dan valt het terug op die "
            "eigen signalen."
        )

    st.subheader("Signalen van vandaag")
    c1, c2, c3, c4 = st.columns(4)
    hrv = sig["hrv"]
    if hrv.get("available"):
        c1.metric(
            "HRV",
            fmt(hrv["current"]),
            delta=f"{hrv['deviation_from_baseline']:+} vs baseline",
            delta_color="normal",
        )
    c2.metric("Slaap (nacht)", fmt(sig["sleep_last_night_h"], " u"))
    rhr_delta = sig.get("resting_hr_delta")
    c3.metric(
        "Rust-HS",
        fmt(sig["resting_hr"]),
        delta=(f"{rhr_delta:+} vs 7d-gem." if rhr_delta is not None else None),
        delta_color="inverse",
    )
    c4.metric("Body Battery (ontwaken)", fmt(sig["body_battery_at_wake"]))

    st.subheader("Waarom")
    for r in readiness["reasons"]:
        st.markdown(f"- {r}")

    with st.expander("Berekende readiness-cijfers", expanded=False):
        st.json(readiness, expanded=False)


# --------------------------------------------------------------------------- #
# Pagina: Dashboard (grafieken)
# --------------------------------------------------------------------------- #
def render_dashboard_page() -> None:
    st.title("Dashboard")
    history = load_history(28)
    report = analysis.analyze_history(history)
    readiness_inputs = load_readiness(28)
    readiness = analysis.analyze_readiness(
        readiness_inputs["history"],
        readiness_inputs["today_summary"],
        readiness_inputs.get("today_readiness"),
    )

    hrv = report["hrv"]
    acwr = report["acwr"]
    weeks = report["volume"]["weeks"]
    light_txt = {"green": "🟢 Groen", "amber": "🟡 Oranje", "red": "🔴 Rood"}.get(
        readiness["light"], readiness["light"]
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Readiness vandaag", light_txt)
    if hrv.get("available"):
        c2.metric(
            "HRV",
            fmt(hrv["current"]),
            delta=f"{hrv['deviation_from_baseline']:+} vs baseline",
            delta_color="off",
        )
    _lv = acwr.get("acwr")
    c3.metric(
        "Belasting vs normaal",
        fmt(round(_lv * 100) if _lv is not None else None, "%"),
        help="Trainingsbelasting deze week t.o.v. je gemiddelde week (4 weken = 100%).",
    )
    c4.metric("Volume (7d)", fmt(weeks[0]["km"] if weeks else None, " km"))

    st.divider()
    st.subheader("HRV-trend — met je baseline-band")
    ch = charts.hrv_chart(
        analysis.hrv_series(history), hrv.get("baseline_mean"), hrv.get("baseline_sd")
    )
    chart_or_caption(ch, "Nog geen HRV-data.")

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Slaap per nacht")
        chart_or_caption(charts.sleep_chart(analysis.sleep_hours_series(history)), "Nog geen slaapdata.")
    with col_b:
        st.subheader("Opbouw — belasting vs. je normale week")
        render_load_meter(acwr.get("acwr"))

    st.subheader("Volume per week")
    col_c, col_d = st.columns(2)
    with col_c:
        chart_or_caption(charts.weekly_bar(weeks, "km", "Afstand (km)", charts.CYAN))
    with col_d:
        chart_or_caption(charts.weekly_bar(weeks, "training_load", "Trainingsbelasting", charts.GOLD))


# --------------------------------------------------------------------------- #
# Pagina: Coach-rapport (Fase 2)
# --------------------------------------------------------------------------- #
def render_coach_page() -> None:
    st.title("Wekelijks coach-rapport")
    st.caption("Berekend in Python, geduid door de AI-coach. Cijfers over ~28 dagen.")

    history = load_history(28)
    report = analysis.analyze_history(history)

    # Readiness van vandaag erbij, zodat het advies daarop aansluit (en niet
    # de "Vandaag"-pagina tegenspreekt).
    readiness_inputs = load_readiness(28)
    readiness = analysis.analyze_readiness(
        readiness_inputs["history"],
        readiness_inputs["today_summary"],
        readiness_inputs.get("today_readiness"),
    )

    hrv = report["hrv"]
    sleep = report["sleep"]
    acwr = report["acwr"]

    c1, c2, c3, c4 = st.columns(4)
    if hrv.get("available"):
        c1.metric(
            "HRV (laatste nacht)",
            fmt(hrv["current"]),
            delta=fmt(hrv["deviation_from_baseline"]) + " vs baseline",
            delta_color="off",
        )
    c2.metric("Slaap (7d gem.)", fmt(sleep.get("avg_7d_h"), " u"))
    c3.metric("ACWR", fmt(acwr.get("acwr")), help=acwr.get("zone", ""))
    weeks = report["volume"]["weeks"]
    c4.metric("Volume (7d)", fmt(weeks[0]["km"] if weeks else None, " km"))

    # Weekvolume-tabel
    st.subheader("Volume per week")
    st.dataframe(
        [
            {
                "periode": w["label"],
                "afstand (km)": w["km"],
                "trainingsbelasting": w["training_load"],
                "runs": w["runs"],
            }
            for w in weeks
        ],
        width="stretch",
        hide_index=True,
    )

    # Pre-filter aandachtspunten (gratis Python)
    st.subheader("Signalen (drempelwaarden)")
    for f in build_flags(report, readiness):
        st.markdown(f"- {f}")

    st.divider()

    # AI-duiding (dure call: achter een knop, gecachet per dag in de sessie)
    api_key = get_secret("anthropic_api_key")
    state_key = f"coach_report_{report['end_date']}"

    col_a, col_b = st.columns([1, 3])
    generate = col_a.button("🧠 Genereer coach-rapport", type="primary")
    if col_b.button("↻ Opnieuw genereren"):
        st.session_state.pop(state_key, None)
        generate = True

    if generate and state_key not in st.session_state:
        if not api_key:
            st.warning(
                "Nog geen Anthropic API-key. Zet `anthropic_api_key` in je secrets "
                "om het AI-rapport te genereren. De cijfers hierboven werken al."
            )
        else:
            with st.spinner("De coach denkt na over je cijfers…"):
                try:
                    st.session_state[state_key] = generate_coach_report(
                        report, api_key, readiness=readiness
                    )
                except CoachError as e:
                    st.error(str(e))

    if state_key in st.session_state:
        st.markdown(st.session_state[state_key])

    with st.expander("Berekende cijfers (ruwe input voor de AI)", expanded=False):
        st.json(report, expanded=False)

    # ----- Publiceren naar BeBetter (jouw eigen dossier) ------------------- #
    st.divider()
    with st.expander("📤 Publiceer naar Coachingsapp (jouw dossier)", expanded=False):
        st.caption(
            "Dit 'briefje' (readiness + weekcijfers + rapport) kan naar je eigen "
            "dossier in je coachingsapp. Je ziet eerst het voorbeeld — er gaat pas "
            "iets de deur uit als je op Publiceren klikt, en alleen naar het nieuwe "
            "bestand garmin_state.json (je klantdata blijft onaangeroerd)."
        )
        athlete_key = get_secret("fs_user_key")
        gh_token = get_secret("GH_TOKEN")
        try:
            state_entry = publish.build_athlete_state(
                athlete_key or "preview",
                readiness=readiness,
                weekly_metrics=report,
                weekly_report_md=st.session_state.get(state_key, ""),
            )
            st.json(state_entry, expanded=False)

            if st.button("👁️ Lokaal voorbeeld opslaan (gaat nergens heen)"):
                p = publish.save_local_preview(state_entry)
                st.success(f"Voorbeeld opgeslagen: {p}")

            if not athlete_key:
                st.info("Vul `fs_user_key` (je FinalSurge user_key) in je secrets in om te publiceren.")
            elif not gh_token:
                st.info("Vul `GH_TOKEN` (schrijfrechten op bebetter-data) in je secrets in om te publiceren.")
            elif st.button("📤 Publiceer naar Coachingsapp", type="primary"):
                ok, msg = publish.publish(athlete_key, state_entry, gh_token)
                (st.success if ok else st.error)(msg)
        except publish.PublishError as e:
            st.error(str(e))


# --------------------------------------------------------------------------- #
# Pagina: Ruwe data (Fase 1)
# --------------------------------------------------------------------------- #
def render_raw_page() -> None:
    st.title("Ruwe Garmin-data — laatste 7 dagen")
    week = load_week()
    dates = week["dates"]

    st.header("Trainingen")
    acts_res = week["activities"]
    if not acts_res.ok:
        st.error(f"Trainingen ophalen mislukt: {acts_res.error}")
    else:
        acts = acts_res.data or []
        if not acts:
            st.info("Geen trainingen gevonden in de afgelopen 7 dagen.")
        else:
            rows = []
            for a in acts:
                rows.append(
                    {
                        "datum": (a.get("startTimeLocal") or "")[:10],
                        "type": dig(a, "activityType", "typeKey", default=""),
                        "naam": a.get("activityName", ""),
                        "afstand (km)": round((a.get("distance") or 0) / 1000, 2),
                        "duur (min)": round((a.get("duration") or 0) / 60, 1),
                        "gem. HS": a.get("averageHR"),
                        "max HS": a.get("maxHR"),
                    }
                )
            st.dataframe(rows, width="stretch", hide_index=True)
        with st.expander("Ruwe trainingen-JSON", expanded=False):
            st.json(acts_res.data, expanded=False)

    st.divider()
    st.header("Per dag")
    st.caption("Nieuwste dag bovenaan. Klap een metric open voor de ruwe velden.")

    for d in reversed(dates):
        day = week["days"][d]
        st.subheader(d)
        c1, c2, c3, c4, c5 = st.columns(5)
        steps = dig(day["summary"].data, "totalSteps")
        resting_hr = dig(day["summary"].data, "restingHeartRate")
        avg_stress = dig(day["summary"].data, "averageStressLevel")
        sleep_sec = dig(day["sleep"].data, "dailySleepDTO", "sleepTimeSeconds")
        sleep_h = round(sleep_sec / 3600, 1) if sleep_sec else None
        hrv = dig(day["hrv"].data, "hrvSummary", "lastNightAvg")
        c1.metric("Stappen", fmt(steps))
        c2.metric("Rust-HS", fmt(resting_hr))
        c3.metric("Slaap", fmt(sleep_h, " u"))
        c4.metric("HRV (nacht)", fmt(hrv))
        c5.metric("Gem. stress", fmt(avg_stress))

        render_raw("Slaap", day["sleep"])
        render_raw("HRV", day["hrv"])
        render_raw("Hartslag (intraday)", day["heart_rate"])
        render_raw("Stress", day["stress"])
        render_raw("Dagsamenvatting", day["summary"])
        st.divider()

    st.header("Body Battery (7 dagen)")
    render_raw("Body Battery — ruwe data", week["body_battery"])


# --------------------------------------------------------------------------- #
# Pagina: Planning & profiel (Fase A)
# --------------------------------------------------------------------------- #
_AFSTANDEN = ["5K", "10K", "15K", "10 EM", "Halve marathon", "30K", "Marathon", "Trail", "Anders"]
_PRIOS = ["A", "B", "C"]
_KAL_TYPES = ["Vakantie", "Lichte week", "Druk/weinig tijd", "Niet beschikbaar", "Hoogtestage", "Anders"]
_TYPES = ["Lange duurloop", "Rustige duurloop", "Intervallen", "Tempo/drempel", "Heuvels", "Baan", "Parkrun", "Kracht"]
_RACE_COLS = ["naam", "datum", "afstand", "prioriteit", "doeltijd"]
_KAL_COLS = ["type", "van", "tot", "notitie"]
_ZONE_COLS = ["naam", "laag", "hoog"]
_DEFAULT_ZONES = [
    {"naam": "Z1 Easy", "laag": 100, "hoog": 154},
    {"naam": "Z2 Marathon", "laag": 155, "hoog": 165},
    {"naam": "Z3 Threshold", "laag": 166, "hoog": 176},
    {"naam": "Z4 Interval", "laag": 177, "hoog": 187},
    {"naam": "Z5 Repetition", "laag": 188, "hoog": 196},
]


def render_planning_page() -> None:
    st.title("Planning & profiel")
    st.caption(
        "Je races, kalender en voorkeuren — de basis waarop de coach straks je "
        "periodisering (lange termijn) en weekschema's (korte termijn) maakt."
    )
    gh_token = get_secret("GH_TOKEN")
    athlete_key = get_secret("fs_user_key")
    if not athlete_key:
        st.warning("Vul `fs_user_key` in je secrets in om je planning te kunnen opslaan.")
    plan = store.load_plan(athlete_key, gh_token) if athlete_key else {}

    st.subheader("🎯 Doel-races")
    st.caption("Prioriteit A = je belangrijkste race(s); B/C = tussendoelen.")
    races_edited = st.data_editor(
        pd.DataFrame(plan.get("races") or [], columns=_RACE_COLS),
        num_rows="dynamic",
        width="stretch",
        key="races_ed",
        column_config={
            "naam": st.column_config.TextColumn("Race"),
            "datum": st.column_config.TextColumn("Datum (JJJJ-MM-DD)"),
            "afstand": st.column_config.SelectboxColumn("Afstand", options=_AFSTANDEN),
            "prioriteit": st.column_config.SelectboxColumn("Prioriteit", options=_PRIOS),
            "doeltijd": st.column_config.TextColumn("Doeltijd (optioneel)"),
        },
    )

    st.subheader("📅 Kalender")
    st.caption("Vakanties, lichte of drukke periodes, niet-beschikbare weken.")
    kal_edited = st.data_editor(
        pd.DataFrame(plan.get("kalender") or [], columns=_KAL_COLS),
        num_rows="dynamic",
        width="stretch",
        key="kal_ed",
        column_config={
            "type": st.column_config.SelectboxColumn("Type", options=_KAL_TYPES),
            "van": st.column_config.TextColumn("Van (JJJJ-MM-DD)"),
            "tot": st.column_config.TextColumn("Tot (JJJJ-MM-DD)"),
            "notitie": st.column_config.TextColumn("Notitie"),
        },
    )

    st.subheader("⚙️ Voorkeuren")
    v = plan.get("voorkeuren") or {}
    c1, c2, c3 = st.columns(3)
    dagen = c1.number_input("Trainingsdagen/week", 1, 14, int(v.get("trainingsdagen_per_week", 5)))
    tijd = c2.number_input("Tijd per training (min)", 20, 240, int(v.get("tijd_per_training_min", 60)), step=5)
    _flex_opts = ["streng", "gemiddeld", "flexibel"]
    flex = c3.selectbox(
        "Flexibiliteit",
        _flex_opts,
        index=_flex_opts.index(v.get("flexibiliteit", "gemiddeld")) if v.get("flexibiliteit") in _flex_opts else 1,
    )
    leuk = st.multiselect("Wat je graag doet", _TYPES, default=[t for t in v.get("types_leuk", []) if t in _TYPES])
    niet = st.multiselect("Wat je liever niet doet", _TYPES, default=[t for t in v.get("types_niet_leuk", []) if t in _TYPES])
    overig = st.text_area(
        "Overige voorkeuren / vaste instructies",
        value=v.get("overig", ""),
        placeholder="bv. zondag lange duurloop, geen baan, niet 2 zware dagen op rij",
    )

    st.subheader("❤️ Hartslagzones")
    st.caption("Je traint op hartslag — hierin schrijft de coach je weken.")
    hz = plan.get("hartslagzones") or {}
    cz1, cz2 = st.columns(2)
    thr_bpm = cz1.number_input("Drempel-HS (bpm)", 100, 230, int(hz.get("threshold_bpm", 176)))
    max_bpm = cz2.number_input("Max-HS (bpm)", 120, 240, int(hz.get("max_bpm", 196)))
    zones_edited = st.data_editor(
        pd.DataFrame(hz.get("zones") or _DEFAULT_ZONES, columns=_ZONE_COLS),
        num_rows="dynamic",
        width="stretch",
        key="zones_ed",
        column_config={
            "naam": st.column_config.TextColumn("Zone"),
            "laag": st.column_config.NumberColumn("Laag (bpm)"),
            "hoog": st.column_config.NumberColumn("Hoog (bpm)"),
        },
    )

    def _records(df: pd.DataFrame, key_field: str) -> list[dict]:
        recs = df.fillna("").to_dict("records")
        return [r for r in recs if str(r.get(key_field, "")).strip()]

    live_plan = {
        "races": _records(races_edited, "naam"),
        "kalender": _records(kal_edited, "type"),
        "voorkeuren": {
            "trainingsdagen_per_week": int(dagen),
            "tijd_per_training_min": int(tijd),
            "flexibiliteit": flex,
            "types_leuk": leuk,
            "types_niet_leuk": niet,
            "overig": overig.strip(),
        },
        "hartslagzones": {
            "threshold_bpm": int(thr_bpm),
            "max_bpm": int(max_bpm),
            "zones": [
                {"naam": str(r["naam"]), "laag": int(r.get("laag") or 0), "hoog": int(r.get("hoog") or 0)}
                for r in zones_edited.fillna(0).to_dict("records")
                if str(r.get("naam", "")).strip()
            ],
        },
    }

    if st.button("💾 Planning opslaan", type="primary", disabled=not athlete_key):
        ok, msg = store.save_plan(athlete_key, live_plan, gh_token)
        if ok:
            st.success("Planning opgeslagen." + (f" {msg}" if msg else ""))
        else:
            st.error(f"Opslaan mislukt: {msg}")
    if plan.get("updated_at"):
        st.caption(f"Laatst opgeslagen: {plan['updated_at']}")

    # ----- Periodisering-skelet (Fase B) — leeft mee met wat je hierboven invult #
    st.divider()
    st.subheader("📈 Periodisering — skelet tot je laatste race")
    if not live_plan["races"]:
        st.caption("Voeg races met een datum toe om het skelet te genereren.")
    else:
        try:
            wv = analysis.analyze_history(load_history(28))["volume"]["weeks"]
            cur_km = round(sum(w["km"] for w in wv) / len(wv)) if wv else 60
        except Exception:
            cur_km = 60
        sk = planner.build_skeleton(live_plan, cur_km)
        st.caption(
            f"Basisvolume uit Garmin (~4 wk gem.): {sk['base_km']} km. Fase, doel-km "
            "en focus per week — de basis voor je concrete weken (Fase C)."
        )
        st.dataframe(
            [
                {
                    "week vanaf": w["week_start"],
                    "fase": w["fase"],
                    "doel km": w["doel_km"],
                    "focus": w["focus"],
                    "races": ", ".join(w["races"]),
                    "notitie": w["notitie"],
                }
                for w in sk["weken"]
            ],
            width="stretch",
            hide_index=True,
        )


# --------------------------------------------------------------------------- #
# Pagina: Weekplan (Fase C)
# --------------------------------------------------------------------------- #
def _render_weekplan(wp: dict) -> None:
    for d in wp.get("dagen", []):
        dag = str(d.get("dag", "")).capitalize()
        km = d.get("km")
        km_txt = f" · {km} km" if km not in (None, "", 0) else ""
        st.markdown(f"**{dag} · {d.get('datum', '')}**{km_txt}  \n{d.get('sessie', '')}")
    if wp.get("toelichting"):
        st.caption(wp["toelichting"])


def render_weekplan_page() -> None:
    st.title("Weekplan")
    st.caption("De concrete week — jouw stramien per fase, in hartslag, met je readiness erin.")
    gh_token = get_secret("GH_TOKEN")
    athlete_key = get_secret("fs_user_key")
    api_key = get_secret("anthropic_api_key")
    plan = store.load_plan(athlete_key, gh_token) if athlete_key else {}
    if not plan.get("races"):
        st.info("Vul eerst je races en voorkeuren in op de 🎯 Planning-pagina.")
        return

    try:
        wv = analysis.analyze_history(load_history(28))["volume"]["weeks"]
        cur_km = round(sum(w["km"] for w in wv) / len(wv)) if wv else 90
    except Exception:
        cur_km = 90
    weeks = planner.build_skeleton(plan, cur_km)["weken"]
    if not weeks:
        st.info("Geen weken in het skelet — staan je race-datums goed?")
        return

    labels = [f"{w['week_start']} · {w['fase']}" for w in weeks]
    default_idx = 1 if len(weeks) > 1 else 0
    idx = st.selectbox(
        "Welke week?", range(len(weeks)), index=default_idx, format_func=lambda i: labels[i]
    )
    week = weeks[idx]
    st.caption(f"Fase: **{week['fase']}** · doel {week['doel_km']} km · {week['focus']}")

    readiness_inputs = load_readiness(28)
    readiness = analysis.analyze_readiness(
        readiness_inputs["history"],
        readiness_inputs["today_summary"],
        readiness_inputs.get("today_readiness"),
    )

    week_start = week["week_start"]
    stored = store.load_weekplan(athlete_key, week_start, gh_token) if athlete_key else {}
    today_iso = date.today().isoformat()
    is_current = week_start <= today_iso <= (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()

    activities = analysis.activities_list(load_history(28))
    hartslagzones = plan.get("hartslagzones") or {}

    col_a, col_b = st.columns([1, 1])
    do_generate = col_a.button(
        "🗓️ Genereer week" if not stored.get("dagen") else "↻ Opnieuw genereren",
        type="primary",
    )
    do_revise = False
    if stored.get("dagen") and is_current:
        do_revise = col_b.button("🔄 Herzie rest van deze week (verse data)")

    if (do_generate or do_revise) and not api_key:
        st.warning("Zet `anthropic_api_key` in je secrets om het weekplan te (her)genereren.")
    elif do_generate:
        # Voer de uitvoering van de vorige week mee, zodat de coach ervan leert.
        prev_review = None
        if idx > 0:
            prev_stored = store.load_weekplan(athlete_key, weeks[idx - 1]["week_start"], gh_token)
            if prev_stored.get("dagen"):
                comp = analysis.week_compliance(prev_stored, activities, hartslagzones)
                prev_review = analysis.compliance_review_text(comp)
        with st.spinner("De coach bouwt je week…"):
            try:
                stored = generate_week_plan(
                    week, readiness, plan, api_key, vorige_week_review=prev_review
                )
                store.save_weekplan(athlete_key, week_start, stored, gh_token)
            except CoachError as e:
                st.error(str(e))
    elif do_revise:
        keep = [d for d in stored.get("dagen", []) if d.get("datum", "") < today_iso]
        with st.spinner("De coach herziet de rest van je week…"):
            try:
                stored = generate_week_plan(
                    week, readiness, plan, api_key, vanaf_datum=today_iso, behouden_dagen=keep
                )
                store.save_weekplan(athlete_key, week_start, stored, gh_token)
            except CoachError as e:
                st.error(str(e))

    if stored.get("dagen"):
        _render_weekplan(stored)
        comp = analysis.week_compliance(stored, activities, hartslagzones)
        if comp.get("dagen"):
            with st.expander("📋 Terugblik — gepland vs. werkelijk"):
                st.dataframe(
                    [
                        {
                            "dag": c["dag"],
                            "gepland km": c["gepland_km"],
                            "werkelijk km": c["werkelijk_km"],
                            "gem HS": c["gem_hs"] if c["gem_hs"] is not None else "—",
                            "zone": c["zone"],
                            "status": c["status"],
                        }
                        for c in comp["dagen"]
                    ],
                    width="stretch",
                    hide_index=True,
                )
                st.caption(
                    f"Totaal gepland {comp['totaal_gepland']} / werkelijk "
                    f"{comp['totaal_werkelijk']} km"
                )
    else:
        st.caption("Nog geen weekplan voor deze week — klik op 'Genereer week'.")


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
if page.startswith("🟢"):
    render_today_page()
elif page.startswith("📈"):
    render_dashboard_page()
elif page.startswith("🎯"):
    render_planning_page()
elif page.startswith("🗓️"):
    render_weekplan_page()
elif page.startswith("🧠"):
    render_coach_page()
else:
    render_raw_page()
