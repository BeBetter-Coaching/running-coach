"""AI Hardloopcoach — Fase 1: ruwe Garmin-data van de laatste 7 dagen.

Doel van dit scherm: bevestigen dat de verbinding met Garmin werkt en zien
welke velden Garmin precies teruggeeft. Nog GEEN analyse of AI — dat is Fase 2.

Alle Garmin-aanroepen lopen via garmin_client.GarminClient (de client-laag).
Deze app leest alleen secrets en toont het resultaat.
"""

from datetime import date
from typing import Any, Optional

import streamlit as st

from garmin_client import GarminClient, GarminClientError

st.set_page_config(
    page_title="AI Hardloopcoach · ruwe data",
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


# --------------------------------------------------------------------------- #
# Hulpjes
# --------------------------------------------------------------------------- #
def dig(obj: Any, *path: str, default: Any = None) -> Any:
    """Veilig door geneste dicts navigeren; geeft default bij ontbreken."""
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
# Sidebar
# --------------------------------------------------------------------------- #
st.sidebar.title("🏃 Hardloopcoach")
st.sidebar.caption("Fase 1 — ruwe Garmin-data")

if st.sidebar.button("🔄 Data verversen (vandaag)"):
    try:
        get_client().clear_cache(date.today().isoformat())
    except Exception:
        pass
    load_week.clear()
    st.rerun()

st.sidebar.divider()

# --------------------------------------------------------------------------- #
# Setup-check: zijn er credentials/tokens?
# --------------------------------------------------------------------------- #
has_login = bool(
    (get_secret("garmin_email") and get_secret("garmin_password"))
    or get_secret("garmin_token_b64")
)

st.title("Ruwe Garmin-data — laatste 7 dagen")

if not has_login:
    st.warning("Nog geen Garmin-inloggegevens gevonden.")
    st.markdown(
        """
**Zo zet je ze veilig klaar (lokaal):**

1. Kopieer `.streamlit/secrets.toml.example` naar `.streamlit/secrets.toml`.
2. Vul `garmin_email` en `garmin_password` in. (`secrets.toml` staat in
   `.gitignore` en wordt nooit gecommit.)
3. Herlaad deze pagina.

Voor Streamlit Cloud draai je eenmalig lokaal
`python scripts/garmin_login.py` en zet je de getoonde `garmin_token_b64`
in **Settings → Secrets**.
        """
    )
    st.stop()

# --------------------------------------------------------------------------- #
# Login-status
# --------------------------------------------------------------------------- #
try:
    name = get_client().get_display_name()
    if name:
        st.sidebar.success(f"Ingelogd als {name}")
    else:
        st.sidebar.info("Sessie actief")
except GarminClientError as e:
    st.sidebar.error("Inloggen mislukt")
    st.error(f"Kon niet bij Garmin inloggen: {e}")
    st.stop()
except Exception as e:  # noqa: BLE001
    st.sidebar.error("Inloggen mislukt")
    st.error(f"Onverwachte fout bij inloggen: {type(e).__name__}: {e}")
    st.stop()

# --------------------------------------------------------------------------- #
# Data laden
# --------------------------------------------------------------------------- #
week = load_week()
dates = week["dates"]

# ----- Trainingen ---------------------------------------------------------- #
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

# ----- Per dag ------------------------------------------------------------- #
st.header("Per dag")
st.caption("Nieuwste dag bovenaan. Klap een metric open voor de ruwe Garmin-velden.")

for d in reversed(dates):  # nieuwste eerst
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

# ----- Body Battery -------------------------------------------------------- #
st.header("Body Battery (7 dagen)")
render_raw("Body Battery — ruwe data", week["body_battery"])
