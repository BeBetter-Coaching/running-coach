#!/usr/bin/env python3
"""Eenmalige lokale login bij Garmin om sessie-tokens te genereren.

Draai dit één keer lokaal vanuit de projectmap:

    .venv/bin/python scripts/garmin_login.py

Wat het doet:
  1. Logt in bij Garmin (vraagt om je 2FA-code als Garmin daarom vraagt).
  2. Bewaart de sessie-tokens in data/garmin_tokens/ voor lokaal hergebruik.
  3. Print een base64 token-string die je in Streamlit Cloud onder
     `garmin_token_b64` kunt zetten, zodat de app in de cloud nooit met je
     wachtwoord hoeft in te loggen.

Je wachtwoord wordt nooit getoond of opgeslagen in code/git.
"""

import getpass
import os
import sys
from pathlib import Path

# Maak garmin_client importeerbaar ongeacht vanwaar je het script start.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_client import GarminClient, GarminClientError  # noqa: E402


def load_secret(key: str):
    """Lees een secret uit env var of uit .streamlit/secrets.toml (indien aanwezig)."""
    if os.environ.get(key.upper()):
        return os.environ[key.upper()]
    secrets_path = ROOT / ".streamlit" / "secrets.toml"
    if secrets_path.exists():
        try:
            import tomllib

            data = tomllib.loads(secrets_path.read_text())
            if data.get(key):
                return data[key]
        except Exception:
            pass
    return None


def main() -> None:
    email = load_secret("garmin_email") or input("Garmin e-mail: ").strip()
    password = load_secret("garmin_password") or getpass.getpass("Garmin wachtwoord: ")

    def mfa_prompt() -> str:
        return input("Garmin 2FA-code (uit je app/sms): ").strip()

    client = GarminClient(email=email, password=password, mfa_callback=mfa_prompt)

    try:
        client.login()
    except GarminClientError as e:
        print(f"\n[FOUT] {e}")
        sys.exit(1)

    name = client.get_display_name()
    print("\n[OK] Ingelogd bij Garmin" + (f" als: {name}" if name else "") + ".")
    print(f"[OK] Tokens opgeslagen in: {ROOT / 'data' / 'garmin_tokens'}")

    token = client.export_token_b64()
    print("\n--- Kopieer onderstaande regel naar Streamlit Cloud -> Settings -> Secrets ---\n")
    print(f'garmin_token_b64 = "{token}"')
    print("\n--- (dit is een sessietoken, GEEN wachtwoord) ---")


if __name__ == "__main__":
    main()
