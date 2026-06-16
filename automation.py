"""Headless coach-run voor de automatisering (GitHub Actions cron).

Draait buiten Streamlit: pakt verse Garmin-data, berekent readiness + een recap
van de laatste run, en (wekelijks, of met --weekly) het volledige coach-oordeel.
Publiceert de staat naar je BeBetter-app (garmin_state.json).

Config via omgevingsvariabelen (NOOIT in code/logs):
  GARMIN_TOKEN_B64   - Garmin-sessietoken (base64), zoals in Streamlit-secrets.
  ANTHROPIC_API_KEY  - voor de AI-duiding.
  GH_TOKEN           - schrijfrechten op bebetter-data (publiceren).
  FS_USER_KEY        - jouw athlete-key.

Gebruik:
  python automation.py            # dagelijks; weekoordeel alleen op maandag
  python automation.py --weekly   # forceer ook het weekoordeel
  python automation.py --dry-run  # alles berekenen, NIETS publiceren
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import analysis
import coach
import publish
import store
from garmin_client import GarminClient


def _log(msg: str) -> None:
    print(f"[coach] {msg}", flush=True)


def _build_run_recap(client, api_key, athlete_key, gh_token, plan) -> dict:
    """Stats + coach's take van de meest recente run (of {} als er geen is)."""
    activities = analysis.activities_list(client.get_history(days=28))
    latest = analysis.latest_run(activities)
    if not latest:
        return {}
    stats = analysis.run_stats(latest, plan.get("hartslagzones"))
    splits = []
    try:
        splits = analysis.run_splits(client.get_activity_splits(stats["activity_id"]).data or [])
    except Exception as e:  # noqa: BLE001
        _log(f"splits overslaan: {type(e).__name__}")

    planned = ""
    run_date = stats["datum"][:10]
    if athlete_key and run_date:
        try:
            rd = date.fromisoformat(run_date)
            rmon = (rd - timedelta(days=rd.weekday())).isoformat()
            rwp = store.load_weekplan(athlete_key, rmon, gh_token)
            pdag = next((d for d in rwp.get("dagen", []) if d.get("datum") == run_date), None)
            planned = pdag.get("sessie", "") if pdag else ""
        except Exception as e:  # noqa: BLE001
            _log(f"geplande sessie overslaan: {type(e).__name__}")

    take = ""
    try:
        take = coach.run_recap(stats, planned, plan.get("hartslagzones", {}), api_key, splits)
    except coach.CoachError as e:
        _log(f"coach's take niet beschikbaar: {e}")
    return {"stats": stats, "take": take, "splits": splits}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true", help="forceer het weekoordeel")
    ap.add_argument("--dry-run", action="store_true", help="niets publiceren")
    args = ap.parse_args()

    token = os.environ.get("GARMIN_TOKEN_B64", "").strip()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    gh_token = os.environ.get("GH_TOKEN", "").strip()
    athlete_key = os.environ.get("FS_USER_KEY", "").strip()
    if not token:
        _log("GARMIN_TOKEN_B64 ontbreekt — afgebroken.")
        return 2

    client = GarminClient(token_b64=token)
    plan = store.load_plan(athlete_key, gh_token) if athlete_key else {}

    # 1) Readiness (Garmin Training Readiness als primaire bron).
    ri = client.get_readiness_inputs(days=28)
    readiness = analysis.analyze_readiness(
        ri["history"], ri["today_summary"], ri.get("today_readiness")
    )
    _log(f"readiness: {readiness.get('light')} ({readiness.get('date')})")

    # 2) Recap van de laatste run.
    run_recap = _build_run_recap(client, api_key, athlete_key, gh_token, plan)
    if run_recap.get("stats"):
        _log(f"laatste run: {run_recap['stats'].get('naam')} ({run_recap['stats'].get('datum')})")

    # 3) Weekoordeel: op maandag, of als --weekly meegegeven is.
    do_weekly = args.weekly or date.today().weekday() == 0
    weekly_metrics: dict = {}
    weekly_report = ""
    if do_weekly:
        history = client.get_history(days=28)
        weekly_metrics = analysis.analyze_history(history)
        if api_key:
            try:
                weekly_report = coach.generate_coach_report(
                    weekly_metrics, api_key, readiness=readiness
                )
                _log("weekoordeel gegenereerd.")
            except coach.CoachError as e:
                _log(f"weekoordeel niet beschikbaar: {e}")
    elif gh_token and athlete_key:
        # Geen weekdag: behoud het laatste weekoordeel i.p.v. het te wissen.
        try:
            current, _ = publish._load_state_file(publish.DEFAULT_REPO, gh_token)
            prev = (current.get(publish._safe_key(athlete_key)) or {}).get("weekly", {})
            weekly_metrics = prev.get("metrics", {})
            weekly_report = prev.get("report_md", "")
        except Exception as e:  # noqa: BLE001
            _log(f"vorig weekoordeel niet opgehaald: {type(e).__name__}")

    # 4) Publiceren naar BeBetter (tenzij dry-run).
    state = publish.build_athlete_state(
        athlete_key or "preview",
        readiness=readiness,
        weekly_metrics=weekly_metrics,
        weekly_report_md=weekly_report,
        run_recap=run_recap,
    )
    if args.dry_run:
        _log("dry-run: niets gepubliceerd. Staat opgebouwd met sleutels: " + ", ".join(state))
        return 0
    if not (athlete_key and gh_token):
        _log("FS_USER_KEY of GH_TOKEN ontbreekt — niets gepubliceerd.")
        return 1
    ok, msg = publish.publish(athlete_key, state, gh_token)
    _log(("OK: " if ok else "FOUT: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
