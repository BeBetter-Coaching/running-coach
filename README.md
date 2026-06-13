# 🏃 AI Hardloopcoach

Persoonlijke AI-hardloopcoach (één gebruiker) die Garmin-data analyseert en
coacht. Zie [DESIGN.md](DESIGN.md) voor de architectuur en keuzes.

**Status:** Fase 1 — Garmin-data binnenhalen en ruw tonen (laatste 7 dagen).

## Lokaal draaien

```bash
# 1. Virtuele omgeving (eenmalig)
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 2. Secrets klaarzetten (eenmalig)
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
#   → vul garmin_email en garmin_password in (dit bestand wordt niet gecommit)

# 3. App starten
.venv/bin/python -m streamlit run app.py
```

De eerste keer logt de app in en bewaart sessie-tokens in
`data/garmin_tokens/`. Heeft je account 2FA, gebruik dan eerst het login-script
(zie hieronder) zodat je de code kunt invoeren.

## Sessietoken voor Streamlit Cloud

Op Streamlit Cloud is de schijf vluchtig, dus log je niet telkens met je
wachtwoord in. Genereer eenmalig lokaal een token:

```bash
.venv/bin/python scripts/garmin_login.py
```

Dit print een regel `garmin_token_b64 = "..."`. Plak die in Streamlit Cloud
onder **Settings → Secrets**. De app gebruikt dan dat token i.p.v. je wachtwoord.

## Structuur

| Bestand | Rol |
|---|---|
| `garmin_client.py` | **Client-laag** — enige plek die Garmin aanroept. |
| `app.py` | Streamlit-scherm: ruwe data van de laatste 7 dagen. |
| `scripts/garmin_login.py` | Eenmalige login → tokens + Cloud-token-string. |
| `.streamlit/secrets.toml` | Je credentials (lokaal, niet in git). |
| `data/` | Sessie-tokens en dagcache (niet in git). |

## Veiligheid
Credentials en tokens staan nooit in code of git. `secrets.toml`, `.env` en
`data/` staan in `.gitignore`.
