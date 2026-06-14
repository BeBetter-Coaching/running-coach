"""store.py — GitHub-backed opslag voor de hardloopcoach (profiel/planning e.d.).

Spiegelt de aanpak van de coachingsapp: JSON in de privé-repo bebetter-data via
de GitHub API, met een lokale fallback als er geen GH_TOKEN is (handig bij
lokaal draaien). Op Streamlit Cloud is de schijf vluchtig, dus daar is de
GitHub-opslag nodig.

Pure module: geen Streamlit-import. De app leest GH_TOKEN uit secrets en geeft
die mee (met env-var fallback).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

DEFAULT_REPO = "BeBetter-Coaching/bebetter-data"
PLANS_FILE = "running_plans.json"  # dict: user_key -> plan
_LOCAL_DIR = Path(__file__).resolve().parent / "data"


def _token(gh_token: Optional[str]) -> str:
    return (gh_token or os.environ.get("GH_TOKEN", "")).strip()


def _headers(token: str) -> dict:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def _url(repo: str, file: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{file}"


def _load_all(gh_token: Optional[str], repo: str = DEFAULT_REPO, file: str = PLANS_FILE) -> dict:
    token = _token(gh_token)
    if token:
        try:
            r = requests.get(_url(repo, file), headers=_headers(token), timeout=10)
            if r.status_code == 200:
                return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
            if r.status_code == 404:
                return {}
        except Exception:
            pass
    # Lokale fallback
    p = _LOCAL_DIR / f".{file}"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_all(
    data: dict, gh_token: Optional[str], repo: str = DEFAULT_REPO, file: str = PLANS_FILE
) -> tuple[bool, str]:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    token = _token(gh_token)
    if token:
        try:
            sha = None
            r = requests.get(_url(repo, file), headers=_headers(token), timeout=10)
            if r.status_code == 200:
                sha = r.json().get("sha")
            body = {
                "message": "Update running_plans via hardloopcoach",
                "content": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
            }
            if sha:
                body["sha"] = sha
            put = requests.put(_url(repo, file), headers=_headers(token), json=body, timeout=15)
            if put.status_code in (200, 201):
                return True, ""
            return False, f"GitHub API: {put.status_code} — {put.text[:150]}"
        except Exception as e:  # noqa: BLE001
            return False, str(e)
    # Lokale fallback
    try:
        _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        (_LOCAL_DIR / f".{file}").write_text(payload)
        return True, "(lokaal opgeslagen — geen GH_TOKEN)"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def load_plan(user_key: str, gh_token: Optional[str] = None) -> dict:
    """Laad het planning-profiel van deze atleet ({} als er nog niets is)."""
    if not user_key:
        return {}
    return (_load_all(gh_token) or {}).get(user_key, {})


def save_plan(user_key: str, plan: dict, gh_token: Optional[str] = None) -> tuple[bool, str]:
    """Sla het planning-profiel op. Geeft (gelukt, melding) terug."""
    if not user_key:
        return False, "Geen athlete-key (fs_user_key) — kan niet opslaan."
    allp = _load_all(gh_token)
    if not isinstance(allp, dict):
        allp = {}
    allp[user_key] = {**plan, "updated_at": datetime.now().isoformat(timespec="seconds")}
    return _save_all(allp, gh_token)
