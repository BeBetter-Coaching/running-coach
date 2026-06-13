"""coach.py — De AI-coachlaag (Fase 2).

Architectuurprincipes:
- #2 REKENEN ≠ AI: de AI krijgt UITSLUITEND de in Python berekende getallen
  (uit analysis.analyze_history) en mag die DUIDEN, niet herberekenen. Het
  systeem-prompt verbiedt expliciet het verzinnen van nieuwe cijfers.
- #3 TWEE-TRAPS: een gratis Python pre-filter (`build_flags`) bepaalt de
  aandachtspunten vóór de dure AI-call. Voor het wekelijkse rapport is er altijd
  een reden om te draaien (vaste cadans); de flags sturen de duiding.
- #4 SIGNAAL BOVEN RUIS + #5 COACH-FILOSOFIE: zie het systeem-prompt.

Gebruikt de officiële Anthropic SDK met het nieuwste Claude-model voor duiding.
"""

from __future__ import annotations

import json
from typing import Optional

import anthropic

# Nieuwste Claude voor duiding/analyse; Haiku (goedkoopst) voor simpele
# filterstappen zoals het dagelijkse readiness-advies bij twijfelgevallen.
MODEL_DUIDING = "claude-opus-4-8"
MODEL_FILTER = "claude-haiku-4-5"

SYSTEM_PROMPT = """\
Je bent de persoonlijke hardloopcoach van Jip. Je bent kritisch en eerlijk, maar \
je beschermt Jip tegen overtraining en obsessie. Je durft rust te adviseren en \
je remt net zo vaak als je pusht. Je gebruikt zijn data om hem tegen zichzelf te \
beschermen — niet om hem op te jagen.

HARDE REGEL: je krijgt al BEREKENDE getallen aangeleverd. Je mag die uitsluitend \
DUIDEN. Verzin nooit nieuwe cijfers, herbereken niets, en noem geen getallen die \
niet in de input staan. Weet je iets niet, zeg dat dan.

SIGNAAL BOVEN RUIS: dit is geen dashboard. Som niet alle cijfers op. Koppel de \
data aan een concrete trainingsbeslissing. Niet "je HRV is 74", maar wat het \
betekent voor wat Jip de komende dagen wel of niet moet doen.

Schrijf in het Nederlands, bondig en direct. Geen overdreven enthousiasme, geen \
emoji. Gebruik exact deze structuur met deze kopjes:

**Oordeel** — één zin: hoe staat Jip ervoor deze week.
**Wat ging goed** — 1 à 2 punten.
**Aandachtspunt** — 1 à 2 punten; benoem het eerlijk, ook als dat "te veel, te \
hard" of "te weinig rust" is.
**Advies voor de komende dagen** — één concrete trainingsaanbeveling (wat, hoe \
zwaar, en waarom), passend bij de cijfers. Durf rust of rustig te zeggen als de \
data daarom vraagt.
"""


class CoachError(Exception):
    """Foutmelding uit de coachlaag die de app netjes kan tonen."""


def build_flags(report: dict) -> list[str]:
    """Gratis Python pre-filter: vertaal de getallen naar aandachtspunten.

    Dit stuurt de AI-duiding en houdt het signaal scherp. Pure drempelwaarden,
    geen AI.
    """
    flags: list[str] = []

    hrv = report.get("hrv", {})
    if hrv.get("available"):
        streak = hrv.get("days_below_baseline_streak", 0)
        z = hrv.get("z_score")
        if streak >= 3:
            flags.append(f"HRV ligt al {streak} dagen onder de baseline.")
        if z is not None and z <= -1.0:
            flags.append("HRV duidelijk onder baseline (mogelijke vermoeidheid).")
        if z is not None and z >= 1.0:
            flags.append("HRV duidelijk boven baseline (goed hersteld).")

    sleep = report.get("sleep", {})
    if sleep.get("available"):
        if sleep.get("short_nights_under_7h_last7", 0) >= 3:
            flags.append(
                f"{sleep['short_nights_under_7h_last7']} korte nachten (<7u) deze week."
            )
        if sleep.get("avg_7d_h", 99) < 7.0:
            flags.append("Slaapgemiddelde deze week onder 7 uur.")

    acwr = report.get("acwr", {})
    if acwr.get("available"):
        ratio = acwr.get("acwr")
        zone = acwr.get("zone", "")
        if ratio is not None and (ratio > 1.3 or ratio < 0.8):
            flags.append(f"ACWR {ratio} — {zone}.")

    if not flags:
        flags.append("Geen opvallende waarschuwingssignalen in de cijfers.")
    return flags


READINESS_SYSTEM = """\
Je bent de hardloopcoach van Jip en geeft één kort, concreet readiness-advies \
voor vandaag. Je krijgt al BEREKENDE signalen en een stoplicht (groen/oranje/ \
rood). Je mag die alleen duiden, niet herberekenen, en geen nieuwe getallen \
verzinnen. Bescherm Jip tegen te hard trainen bij onderherstel, maar benoem ook \
eerlijk als er ruimte is om te pushen. Schrijf in het Nederlands, maximaal twee \
zinnen, direct en zonder emoji. Zeg concreet wat hij vandaag wel of niet moet \
doen (bv. "rustige duurloop", "intervallen kunnen door", "rustdag").
"""


def readiness_template(readiness: dict) -> str:
    """Gratis, AI-loos advies voor de duidelijke gevallen (groen/rood)."""
    light = readiness.get("light")
    reasons = readiness.get("reasons", [])
    why = (" Reden: " + reasons[0]) if reasons else ""
    if light == "green":
        return "Groen — je bent hersteld. Een zware of intensieve training kan vandaag door." + why
    if light == "red":
        return "Rood — onderherstel. Houd het rustig of neem een rustdag; forceer vandaag niets." + why
    return "Oranje — gemengde signalen. Pas de intensiteit aan op hoe je je voelt." + why


def daily_readiness_advice(readiness: dict, api_key: Optional[str]) -> str:
    """Eén goedkope Haiku-call die het oranje twijfelgeval duidt tot advies."""
    client = _client(api_key)
    user_content = (
        f"Stoplicht: {readiness.get('light')}\n"
        "Signalen (vooraf in Python berekend):\n"
        + "\n".join(f"- {r}" for r in readiness.get("reasons", []))
        + "\n\nBerekende waarden (JSON):\n"
        + json.dumps(readiness.get("signals", {}), ensure_ascii=False, indent=2)
    )
    try:
        response = client.messages.create(
            model=MODEL_FILTER,
            max_tokens=300,
            system=READINESS_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        raise CoachError("Anthropic API-key ongeldig. Controleer `anthropic_api_key`.")
    except anthropic.RateLimitError:
        raise CoachError("Anthropic rate limit bereikt. Probeer het zo opnieuw.")
    except anthropic.APIError as e:
        raise CoachError(f"Anthropic API-fout: {e}")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or readiness_template(readiness)


def _client(api_key: Optional[str]) -> anthropic.Anthropic:
    if not api_key:
        raise CoachError(
            "Geen Anthropic API-key gevonden. Zet `anthropic_api_key` in je "
            "secrets (of de omgevingsvariabele ANTHROPIC_API_KEY)."
        )
    return anthropic.Anthropic(api_key=api_key)


def generate_coach_report(
    report: dict,
    api_key: Optional[str],
    model: str = MODEL_DUIDING,
) -> str:
    """Eén AI-call die de berekende getallen duidt tot een wekelijks coach-rapport."""
    client = _client(api_key)
    flags = build_flags(report)

    user_content = (
        "Hier zijn de in Python berekende getallen over de afgelopen periode "
        f"(t/m {report.get('end_date', 'onbekend')}). Duid ze volgens je rol.\n\n"
        "AANDACHTSPUNTEN (vooraf bepaald door drempelwaarden):\n"
        + "\n".join(f"- {f}" for f in flags)
        + "\n\nBEREKENDE CIJFERS (JSON):\n"
        + json.dumps(report, ensure_ascii=False, indent=2)
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2500,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        raise CoachError("Anthropic API-key ongeldig. Controleer `anthropic_api_key`.")
    except anthropic.RateLimitError:
        raise CoachError("Anthropic rate limit bereikt. Probeer het zo opnieuw.")
    except anthropic.APIError as e:
        raise CoachError(f"Anthropic API-fout: {e}")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise CoachError("De AI gaf een leeg antwoord terug.")
    return text
