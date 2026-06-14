"""AI Hardloopcoach — Streamlit-app.

Twee pagina's:
- Coach-rapport (Fase 2): Python berekent HRV-baseline/afwijking, slaaptrend,
  ruwe ACWR en weekvolume; één AI-call duidt die getallen tot een wekelijks
  coach-rapport.
- Ruwe data (Fase 1): de ruwe Garmin-velden van de laatste 7 dagen.

Alle Garmin-aanroepen lopen via garmin_client.GarminClient (de client-laag);
alle berekeningen via analysis.py; de duiding via coach.py.
"""

from datetime import date
from typing import Any, Optional

import streamlit as st

from garmin_client import GarminClient, GarminClientError
import analysis
import publish
from coach import (
    generate_coach_report,
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


@st.cache_resource(show_spinner=False)
def get_client() -> GarminClient:
    return GarminClient(
        email=get_secret("garmin_email"),
        password=get_secret("garmin_password"),
        token_b64=get_secret("garmin_token_b64"),
    )


@st.cache_data(ttl=3600, show_spinner="Garmin-data ophalen…")
def load_week() -> dict:
    return get_client().get_last_7_days()


@st.cache_data(ttl=3600, show_spinner="Garmin-historie ophalen (28 dagen)…")
def load_history(days: int = 28) -> dict:
    return get_client().get_history(days=days)


@st.cache_data(ttl=3600, show_spinner="Readiness-data ophalen…")
def load_readiness(days: int = 28) -> dict:
    return get_client().get_readiness_inputs(days=days)


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
    ["🟢 Vandaag", "🧠 Coach-rapport", "📊 Ruwe data (7 dagen)"],
)

if st.sidebar.button("🔄 Data verversen (vandaag)"):
    try:
        get_client().clear_cache(date.today().isoformat())
    except Exception:
        pass
    load_week.clear()
    load_history.clear()
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
    readiness = analysis.analyze_readiness(inputs["history"], inputs["today_summary"])
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

    with st.expander("ℹ️ Wat betekent dit stoplicht?"):
        st.markdown(
            "**🟢 Groen** — hersteld; ruimte voor een zware of intensieve training.\n\n"
            "**🟡 Oranje** — gemengde signalen óf een recente zware sessie/wedstrijd; "
            "pas de intensiteit aan, vaak een rustige duurloop.\n\n"
            "**🔴 Rood** — onderherstel; rust of zeer rustig.\n\n"
            "Het stoplicht weegt: je **HRV** t.o.v. je baseline, je **slaap**, je "
            "**rust-hartslag** t.o.v. je weekgemiddelde, je **Body Battery** bij "
            "ontwaken, je **acute:chronische belasting (ACWR)**, én **zware "
            "trainingen van de afgelopen 24–48u** (Garmin Training Effect)."
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
        readiness_inputs["history"], readiness_inputs["today_summary"]
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
        use_container_width=True,
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
    with st.expander("📤 Publiceer naar BeBetter (jouw dossier)", expanded=False):
        st.caption(
            "Dit 'briefje' (readiness + weekcijfers + rapport) kan naar je eigen "
            "dossier in BeBetter. Je ziet eerst het voorbeeld — er gaat pas iets de "
            "deur uit als je op Publiceren klikt, en alleen naar het nieuwe bestand "
            "garmin_state.json (je klantdata blijft onaangeroerd)."
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
            elif st.button("📤 Publiceer naar BeBetter", type="primary"):
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
            st.dataframe(rows, use_container_width=True, hide_index=True)
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
# Router
# --------------------------------------------------------------------------- #
if page.startswith("🟢"):
    render_today_page()
elif page.startswith("🧠"):
    render_coach_page()
else:
    render_raw_page()
