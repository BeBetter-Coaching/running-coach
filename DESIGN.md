# DESIGN — AI Hardloopcoach

Persoonlijke AI-hardloopcoach voor één gebruiker. Analyseert Garmin-data,
coacht dagelijks, en (later) zet schema's in FinalSurge en geeft feedback op
uitgevoerde trainingen. Dit document legt de architectuurkeuzes vast zodat een
latere sessie begrijpt *waarom* iets zo is.

## Techniekkeuzes (vast)
- **Python + Streamlit**, gedeployed op **Streamlit Cloud**.
- **Garmin-data** via `python-garminconnect` (inofficieel; logt in met het
  account). Kan breken bij Garmin-wijzigingen → volledig geïsoleerd achter één
  client-laag.
- **AI** via de Anthropic API: nieuwste Claude voor duiding/analyse, Haiku voor
  goedkope filterstappen. (Pas vanaf Fase 2.)
- **Persistente data** (baselines, geschiedenis, coach-notities) GitHub-backed,
  want het Streamlit-bestandssysteem is vluchtig. (Wordt in Fase 2 ingericht.)

## Architectuurprincipes (streng)
1. **CLIENT-LAAG** — `garmin_client.py` is de enige plek die Garmin aanroept.
   Login, tokens, retry, foutafhandeling en caching zitten daar. Breekt Garmin?
   Eén plek repareren.
2. **REKENEN ≠ AI** — alles wat rekenkundig kan (trends, HRV-baseline, acuut vs.
   chronisch, slaapgemiddelden) gebeurt in pure Python. De AI krijgt kant-en-
   klare getallen en mag die alleen *duiden*, niet berekenen.
3. **TWEE-TRAPS AI** — duur model alleen bij reden. Eerst gratis pre-filter
   (drempelwaarden), dan pas AI.
4. **SIGNAAL BOVEN RUIS** — geen dashboard dat alles toont; de waarde zit in de
   koppeling DATA → TRAININGSBESLISSING.
5. **COACH-FILOSOFIE** — kritisch en eerlijk, beschermt tegen overtraining en
   obsessie, durft rust te adviseren, remt net zo vaak als het pusht.

## Client-laag — implementatiekeuzes
`garmin_client.py`:
- Importeert **geen** Streamlit → puur en los testbaar. De app leest secrets en
  geeft credentials door aan de constructor; env vars als fallback.
- **Login-volgorde** (van goedkoop/veilig naar duur):
  1. `garmin_token_b64` (base64 van de sessietoken-JSON) uit secret/env — voor
     Streamlit Cloud, waar de schijf vluchtig is.
  2. Tokenmap op schijf (`data/garmin_tokens/garmin_tokens.json`) — lokaal.
  3. Verse login met e-mail/wachtwoord (+ 2FA-prompt indien nodig); tokens
     worden daarna opgeslagen.
  Reden: Garmin blokkeert accounts bij te vaak opnieuw inloggen. Tokens
  hergebruiken voorkomt dat.
- **Foutafhandeling per metric**: elke Garmin-call wordt los afgevangen en geeft
  een `MetricResult(ok/error/from_cache)` terug. Eén kapot endpoint laat de rest
  van het scherm werken en toont *wat* stuk is.
- **Retry**: alleen op transiente verbindingsfouten (1 herhaling). Bij
  `TooManyRequests` direct stoppen — opnieuw proberen maakt de blokkade erger.
- **Caching per dag** (`data/cache/<metric>_<datum>.json`): historische dagen
  worden permanent gecachet (data verandert niet); de huidige dag heeft een TTL
  (standaard 120 min). Bespaart calls en houdt de kosten/limieten laag.

### Let op: `garminconnect` 0.3.x
De geïnstalleerde versie (0.3.2) gebruikt een **eigen HTTP-client** (`curl_cffi`,
browser-impersonation) en **niet** meer `garth`. Gevolgen:
- HTTP-fouttype is `requests.HTTPError`; geen `garth.exc`.
- Tokens via `client.dumps()/dump(path)/loads()`; `dumps()` geeft **JSON**
  (geen base64). Wij base64-encoden die zelf voor een quote-vrije Cloud-secret.
- `login(arg)` accepteert óf een map-pad óf een tokenstring (detectie op lengte
  > 512). Bij een map: laadt tokens als aanwezig, anders verse login + opslaan.

## Datavelden (Fase 1, waargenomen via de app)
Opgehaald per dag: `get_sleep_data`, `get_hrv_data`, `get_heart_rates`,
`get_stress_data`, `get_user_summary`. Over de hele 7-daagse range:
`get_body_battery`, `get_activities_by_date`.
> Vul deze sectie aan met de exacte veldnamen zodra we de eerste echte data
> hebben gezien (bv. `dailySleepDTO.sleepTimeSeconds`, `hrvSummary.lastNightAvg`,
> `restingHeartRate`, `totalSteps`, `averageStressLevel`).

## Modules (Fase 2/3)
- `analysis.py` — pure Python berekeningen (geen AI, geen Streamlit). HRV-baseline
  + afwijking/z-score/streak, slaaptrend, ruwe ACWR (acuut 7d : chronisch
  weekgemiddelde 28d, o.b.v. `activityTrainingLoad`), weekvolume, en de
  dagelijkse readiness (`analyze_readiness`).
- `coach.py` — AI-laag. Wekelijkse duiding via het nieuwste model
  (`claude-opus-4-8`, adaptive thinking). Dagelijks readiness-advies via het
  goedkoopste model (`claude-haiku-4-5`) en alleen bij het oranje twijfelgeval;
  groen/rood krijgen een gratis getemplate advies (twee-traps).
- Kostenbeheersing: het wekelijkse rapport zit achter een knop + dag-cache;
  readiness roept Haiku alleen aan bij oranje.

## Bouwvolgorde
- **Fase 1:** Garmin-data binnenhalen + ruw tonen (7 dagen). ✅ klaar + gepusht.
- **Fase 2:** weekanalyse → wekelijks coach-rapport. ✅ gebouwd (AI-call wacht op
  Anthropic-key voor live test).
- **Fase 3:** dagelijks readiness-advies (go/no-go-stoplicht). ✅ gebouwd.
- **Fase 4 (nog niet):** trainingsschema's in FinalSurge — vereist FinalSurge-
  login (zelfde isolatie als Garmin) + een racedoel.
- **Fase 5 (nog niet):** feedback op uitgevoerde trainingen (gepland vs.
  werkelijk).

## Beveiliging
- Credentials/tokens nooit in code of git. `secrets.toml`, `.env`,
  `data/garmin_tokens/` en `data/cache/` staan in `.gitignore`.
- Lokaal: `.streamlit/secrets.toml`. Cloud: Streamlit Secrets-manager.
