"""publish.py — Stuurt de "athlete state" van running-coach naar BeBetter.

Eénrichtingsverkeer: running-coach → de privé-repo BeBetter-Coaching/bebetter-data,
in een NIEUW bestand `garmin_state.json` dat de BeBetter-app nu niet gebruikt.
Daardoor kan dit de bestaande klantdata (intakes.json, notes.json, ...) niet raken.

Veiligheidsontwerp:
- Schrijft uitsluitend naar `garmin_state.json` (vaste bestandsnaam), nergens anders.
- Bewaart de staat in één dict gekeyd op athlete-key (zelfde patroon als BeBetters
  intakes), dus meerdere atleten = meerdere keys in één bestand; bestaande keys
  van anderen blijven staan (load → merge → save).
- Publiceren VEREIST een GitHub-token; zonder token gebeurt er niets extern.
- `save_local_preview()` schrijft een lokaal voorbeeld zodat je altijd eerst kunt
  zien wat verstuurd zou worden, zonder dat er iets de deur uit gaat.

Spiegelt bewust de aanpak van BeBetters intake_store.py (zelfde repo, zelfde
GitHub-API-conventies).
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

DEFAULT_REPO = "BeBetter-Coaching/bebetter-data"
STATE_FILE = "garmin_state.json"  # NIEUW bestand; BeBetter gebruikt dit (nog) niet


class PublishError(Exception):
    """Foutmelding bij het publiceren die de app netjes kan tonen."""


def _safe_key(athlete_key: str) -> str:
    key = (athlete_key or "").strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9_.\-]+", key):
        raise PublishError(
            "Ongeldige athlete-key. Vul je FinalSurge user_key in (alleen letters, "
            "cijfers, '-', '_', '.')."
        )
    return key


def build_athlete_state(
    athlete_key: str,
    readiness: Optional[dict] = None,
    weekly_metrics: Optional[dict] = None,
    weekly_report_md: Optional[str] = None,
) -> dict:
    """Bouw het 'briefje' dat naar BeBetter gaat: één compacte, leesbare staat."""
    return {
        "athlete_key": _safe_key(athlete_key),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "running-coach",
        "readiness": readiness or {},
        "weekly": {
            "metrics": weekly_metrics or {},
            "report_md": weekly_report_md or "",
        },
    }


def save_local_preview(state_entry: dict, base_dir: Optional[Path] = None) -> Path:
    """Schrijf een lokaal voorbeeld (gaat nergens heen). Geeft het pad terug."""
    base = base_dir or (Path(__file__).resolve().parent / "data" / "published")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{_safe_key(state_entry.get('athlete_key', 'preview'))}.json"
    path.write_text(json.dumps(state_entry, ensure_ascii=False, indent=2))
    return path


# --------------------------------------------------------------------------- #
# GitHub (spiegelt intake_store.py)
# --------------------------------------------------------------------------- #
def _headers(token: str) -> dict:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def _api_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{STATE_FILE}"


def _load_state_file(repo: str, token: str) -> tuple[dict, Optional[str]]:
    """Haal het huidige garmin_state.json + sha op (leeg dict als het nog niet bestaat)."""
    resp = requests.get(_api_url(repo), headers=_headers(token), timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        try:
            return json.loads(content), data.get("sha")
        except json.JSONDecodeError:
            return {}, data.get("sha")
    if resp.status_code == 404:
        return {}, None
    raise PublishError(f"GitHub API: {resp.status_code} — {resp.text[:200]}")


def publish(
    athlete_key: str,
    state_entry: dict,
    gh_token: Optional[str],
    repo: str = DEFAULT_REPO,
) -> tuple[bool, str]:
    """Publiceer de staat naar BeBetter. VEREIST een token; anders niets extern.

    Leest het huidige `garmin_state.json`, zet/vervangt alleen de eigen athlete-key
    en schrijft terug. Andere atleten blijven onaangeroerd.
    """
    key = _safe_key(athlete_key)
    token = (gh_token or os.environ.get("GH_TOKEN", "")).strip()
    if not token:
        return False, "Geen GH_TOKEN — er is niets verstuurd (alleen het lokale voorbeeld is gemaakt)."

    try:
        current, sha = _load_state_file(repo, token)
        if not isinstance(current, dict):
            current = {}
        current[key] = state_entry  # alleen onze eigen key zetten

        body: dict[str, Any] = {
            "message": f"Update garmin_state voor {key} via running-coach",
            "content": base64.b64encode(
                json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8")
            ).decode("ascii"),
        }
        if sha:
            body["sha"] = sha

        put = requests.put(_api_url(repo), headers=_headers(token), json=body, timeout=15)
        if put.status_code in (200, 201):
            return True, "Gepubliceerd naar BeBetter."
        return False, f"GitHub API: {put.status_code} — {put.text[:200]}"
    except PublishError:
        raise
    except Exception as e:  # noqa: BLE001
        return False, f"Publiceren mislukt: {type(e).__name__}: {e}"
