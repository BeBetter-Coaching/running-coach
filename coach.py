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
from datetime import date, timedelta
from typing import Optional

import anthropic

import analysis

_DAGEN_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]

WEEK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "dagen": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dag": {"type": "string"},
                    "datum": {"type": "string"},
                    "sessie": {"type": "string"},
                    "type": {"type": "string"},
                    "km": {"type": "number"},
                },
                "required": ["dag", "datum", "sessie", "type", "km"],
            },
        },
        "toelichting": {"type": "string"},
    },
    "required": ["dagen", "toelichting"],
}

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

BELANGRIJK: je krijgt ook de READINESS VAN VANDAAG mee (stoplicht + reden, incl. \
een eventuele zware sessie of wedstrijd van de afgelopen 24–48u). Je advies MOET \
daarop aansluiten — spreek de dagstatus niet tegen. Deed Jip net een zware sessie \
of wedstrijd, dan begint je advies met herstel (vandaag/morgen easy of rust), ook \
al ogen de wekelijkse herstelcijfers groen. Bouw de prikkel pas daarna weer op.
"""


class CoachError(Exception):
    """Foutmelding uit de coachlaag die de app netjes kan tonen."""


def build_flags(report: dict, readiness: Optional[dict] = None) -> list[str]:
    """Gratis Python pre-filter: vertaal de getallen naar aandachtspunten.

    Dit stuurt de AI-duiding en houdt het signaal scherp. Pure drempelwaarden,
    geen AI. Als de readiness van vandaag meekomt, staat die vooraan zodat het
    advies daarop aansluit.
    """
    flags: list[str] = []

    # Readiness van vandaag eerst — dit moet het advies sturen.
    if readiness:
        light = readiness.get("light")
        if light in ("amber", "red"):
            flags.append(
                f"Readiness vandaag: {analysis.READINESS_LABELS.get(light, light)} "
                f"({analysis.READINESS_MEANING.get(light, '')})."
            )
        hard = (readiness.get("signals") or {}).get("last_hard_session")
        if hard and hard.get("hours_ago") is not None and hard["hours_ago"] <= 48:
            flags.append(
                f"Zware sessie {hard['hours_ago']:.0f}u geleden "
                f"({hard.get('name')}, Training Effect {hard.get('aerobic_te')}/"
                f"{hard.get('anaerobic_te')}) — prioriteer eerst herstel."
            )

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


WEEK_SYSTEM_PROMPT = """\
Je bent de ervaren hardloopcoach van Jip en bouwt ÉÉN concrete trainingsweek.

HARDE REGELS:
- Houd je STRIKT aan zijn vaste weekstramien: welke dag wat (inclusief dubbels,
  ochtend/avond). Vul dat in passend bij de FASE van deze week.
- Minimaal 90 km in de week (tenzij het een vakantieweek is). Een deload verlaagt
  de INTENSITEIT, niet de omvang — houd het volume dan op de basis.
- Stem de intensiteit af op zijn READINESS: bij oranje/rood minder of geen
  kwaliteit (verzacht of verschuif sleutelsessies); bij groen is er ruimte.
- Jip traint op HARTSLAG. Schrijf de intensiteit PRIMAIR in hartslag (bpm)
  volgens zijn zones in de invoer (bv. "Z3 drempel, 166–176 bpm"). Tempo mag als
  ruwe richtlijn erbij (afgeleid uit zijn doeltijden), maar de hartslag-zone
  leidt — zeker bij duurlopen en drempel. Voor korte baan-/intervallen mag tempo
  leidend zijn (HS ijlt daar na).

INTRA-WEEK BELASTING (belangrijk):
- Beheer de cumulatieve vermoeidheid OVER de week. Stapel geen topkwaliteit op
  vermoeide benen: ~24–48u na een zware sessie of wedstrijd zijn de benen vaak
  nog taai. Ga zo'n dag beheerst in of verzacht de sessie, en zet kwaliteitsdagen
  niet zonder herstel achter elkaar (bv. de quality op maandag beheerst als
  dinsdag een baansessie is).
- LANGE DUURLOOP (zondag): stem de intensiteit af op de zwaarte van de week. Na
  een kwaliteitsrijke week is dit GEEN derde zware sessie → Z1-basis met
  Z2-blokken (functioneel). Alleen Z3-/drempelblokken in de lange duur in
  rustigere of opbouw-weken waar de week dat toelaat.

Schrijf per dag de sessie(s) concreet: afstand of duur, structuur (bv. 2x15'
threshold Z3) en zone/tempo. Markeer dubbels duidelijk. Sluit af met 1–2 zinnen
waarom de week zo is opgebouwd. Nederlands, bondig, geen emoji.
"""


def generate_week_plan(
    week: dict,
    readiness: dict,
    plan: dict,
    api_key: Optional[str],
    vanaf_datum: Optional[str] = None,
    behouden_dagen: Optional[list] = None,
    vorige_week_review: Optional[str] = None,
    model: str = MODEL_DUIDING,
) -> dict:
    """Genereer een gestructureerde, concrete week (Fase C).

    Bij `vanaf_datum` + `behouden_dagen` worden de al gedane dagen ongewijzigd
    overgenomen en wordt alleen de rest van de week herzien (verse readiness).
    Geeft terug: {week_start, fase, doel_km, dagen:[...], toelichting}.
    """
    client = _client(api_key)
    voork = plan.get("voorkeuren", {}) or {}
    races = plan.get("races", []) or []
    races_txt = (
        "; ".join(
            f"{r.get('naam')} ({r.get('datum')}, {r.get('afstand')}, prio "
            f"{r.get('prioriteit')}, doel {r.get('doeltijd', '?')})"
            for r in races
        )
        or "geen"
    )
    light = {"green": "GROEN", "amber": "ORANJE", "red": "ROOD"}.get(
        readiness.get("light"), readiness.get("light", "")
    )
    reasons = "; ".join((readiness.get("reasons") or [])[:3])

    hz = plan.get("hartslagzones") or {}
    zone_lines = [
        f"- {z.get('naam')}: {z.get('laag')}–{z.get('hoog')} bpm"
        for z in (hz.get("zones") or [])
        if z.get("naam")
    ]
    zones_txt = "\n".join(zone_lines) or "(geen hartslagzones opgegeven)"
    thr_max = ""
    if hz.get("threshold_bpm") or hz.get("max_bpm"):
        thr_max = f" (drempel {hz.get('threshold_bpm', '?')} bpm, max {hz.get('max_bpm', '?')} bpm)"

    try:
        wk0 = date.fromisoformat(week.get("week_start"))
    except Exception:
        wk0 = date.today()
    datums = [wk0 + timedelta(days=i) for i in range(7)]
    datum_lijst = "\n".join(f"- {_DAGEN_NL[i]}: {datums[i].isoformat()}" for i in range(7))

    keep_block = ""
    if vanaf_datum and behouden_dagen:
        done = "\n".join(
            f"- {d.get('dag')} {d.get('datum')}: {d.get('sessie')}" for d in behouden_dagen
        )
        keep_block = (
            "\n\nHERZIENING — de volgende dagen staan VAST (al gedaan) en neem je "
            f"ONGEWIJZIGD over:\n{done}\nHerplan alleen vanaf {vanaf_datum} op basis van de "
            "actuele readiness; houd de rest van de week samenhangend."
        )

    review_block = ""
    if vorige_week_review:
        review_block = (
            "\n\n" + vorige_week_review + "\nWeeg dit mee: liep hij structureel te hard "
            "(HS te hoog voor de zone), plan dan iets rustiger; ging het goed, bouw door; "
            "gemiste sleutelsessies inhalen of laten, afhankelijk van de fase."
        )

    user = f"""FASE-SKELET VAN DEZE WEEK (week vanaf {week.get('week_start')}):
- Fase: {week.get('fase')}
- Doel weekvolume: {week.get('doel_km')} km
- Focus: {week.get('focus')}
- Races deze week: {', '.join(week.get('races', [])) or 'geen'}
- Notitie: {week.get('notitie') or '-'}

VAST WEEKSTRAMIEN (volg dit strikt qua dag-structuur):
{voork.get('overig') or '(geen stramien opgegeven)'}

VOORKEUREN: {voork.get('trainingsdagen_per_week', '?')} trainingsdagen/week, \
~{voork.get('tijd_per_training_min', '?')} min/sessie, flexibiliteit \
{voork.get('flexibiliteit', '?')}. Leuk: {', '.join(voork.get('types_leuk', []))}. \
Liever niet: {', '.join(voork.get('types_niet_leuk', []))}.

RACEDOELEN (voor tempo's): {races_txt}

HARTSLAGZONES{thr_max} — schrijf de intensiteit hierin (bpm):
{zones_txt}

READINESS VANDAAG: {light}{(' — ' + reasons) if reasons else ''}{review_block}

DAGEN VAN DEZE WEEK (gebruik exact deze datums en dagnamen):
{datum_lijst}{keep_block}

Bouw nu de concrete week als gestructureerde dagen (maandag t/m zondag): per dag de
dag, datum, een concrete sessie (intensiteit in hartslag/bpm), het type
(rustig/quality/dubbel/rust/wedstrijd) en de km. Vul ook een korte 'toelichting'
(waarom de week zo is opgebouwd)."""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": WEEK_SCHEMA}},
            system=WEEK_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        raise CoachError("Anthropic API-key ongeldig. Controleer `anthropic_api_key`.")
    except anthropic.RateLimitError:
        raise CoachError("Anthropic rate limit bereikt. Probeer het zo opnieuw.")
    except anthropic.APIError as e:
        raise CoachError(f"Anthropic API-fout: {e}")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    try:
        data = json.loads(text)
    except Exception:
        raise CoachError("Kon het weekplan niet als gestructureerde data lezen.")
    return {
        "week_start": week.get("week_start"),
        "fase": week.get("fase"),
        "doel_km": week.get("doel_km"),
        "dagen": data.get("dagen", []),
        "toelichting": data.get("toelichting", ""),
    }


ADJUST_SYSTEM = """\
Je bent de hardloopcoach van Jip. Je krijgt de sessie die vandaag gepland staat en
zijn actuele readiness. Zeg in maximaal 2 zinnen of hij de sessie zo kan doen, of
hem moet aanpassen (verzachten, korter, of verschuiven naar morgen). Hij traint op
hartslag — gebruik bpm-zones. Bij onderherstel bescherm je hem; bij groen mag hij
gaan. Nederlands, direct, geen emoji.
"""


def adjust_today(session_text: str, readiness: dict, plan: dict, api_key: Optional[str]) -> str:
    """Korte dagelijkse bijsturing van de geplande sessie o.b.v. verse readiness (Haiku)."""
    client = _client(api_key)
    hz = plan.get("hartslagzones") or {}
    zones = "; ".join(
        f"{z.get('naam')} {z.get('laag')}-{z.get('hoog')}"
        for z in (hz.get("zones") or [])
        if z.get("naam")
    )
    light = {"green": "GROEN", "amber": "ORANJE", "red": "ROOD"}.get(
        readiness.get("light"), readiness.get("light", "")
    )
    reasons = "; ".join((readiness.get("reasons") or [])[:3])
    user = (
        f"Gepland vandaag: {session_text}\n"
        f"Readiness: {light}{(' — ' + reasons) if reasons else ''}\n"
        f"HS-zones: {zones}\n\nKan hij dit zo doen, of aanpassen?"
    )
    try:
        response = client.messages.create(
            model=MODEL_FILTER,
            max_tokens=250,
            system=ADJUST_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        raise CoachError("Anthropic API-key ongeldig. Controleer `anthropic_api_key`.")
    except anthropic.RateLimitError:
        raise CoachError("Anthropic rate limit bereikt. Probeer het zo opnieuw.")
    except anthropic.APIError as e:
        raise CoachError(f"Anthropic API-fout: {e}")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or "Ga zoals gepland."


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
    readiness: Optional[dict] = None,
    model: str = MODEL_DUIDING,
) -> str:
    """Eén AI-call die de berekende getallen duidt tot een wekelijks coach-rapport.

    `readiness` (de dagstatus) wordt meegegeven zodat het advies daarop aansluit
    en niet de "Vandaag"-pagina tegenspreekt.
    """
    client = _client(api_key)
    flags = build_flags(report, readiness)

    readiness_block = ""
    if readiness:
        readiness_block = (
            "\n\nREADINESS VANDAAG (dagstatus — je advies moet hierop aansluiten):\n"
            + json.dumps(
                {
                    "light": readiness.get("light"),
                    "reasons": readiness.get("reasons"),
                    "last_hard_session": (readiness.get("signals") or {}).get(
                        "last_hard_session"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    user_content = (
        "Hier zijn de in Python berekende getallen over de afgelopen periode "
        f"(t/m {report.get('end_date', 'onbekend')}). Duid ze volgens je rol.\n\n"
        "AANDACHTSPUNTEN (vooraf bepaald door drempelwaarden):\n"
        + "\n".join(f"- {f}" for f in flags)
        + readiness_block
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
