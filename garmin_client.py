"""garmin_client.py — De ENIGE plek die direct met Garmin Connect praat.

Architectuurprincipe #1 (CLIENT-LAAG): alle Garmin-aanroepen lopen via deze
laag. Login (met hergebruik van sessie-tokens), retry, foutafhandeling per
metric en caching per dag zitten hier. Breekt Garmin door een wijziging aan hun
kant? Dan repareren we op precies één plek; de rest van de app weet niets van
Garmin-internals.

Bewuste keuzes:
- Geen `import streamlit` hier. Deze module is puur en los testbaar. De
  Streamlit-app leest secrets en geeft credentials door aan de constructor.
- Login probeert eerst opgeslagen tokens (base64-secret -> tokenmap op schijf)
  en valt alleen terug op e-mail/wachtwoord als dat moet. Garmin blokkeert
  accounts bij te vaak opnieuw inloggen; tokens voorkomen dat.
- Elke metric wordt apart opgehaald en afgevangen: één kapot Garmin-endpoint
  laat de rest van het scherm gewoon werken (en laat zien WAT er stuk is).

Geschreven tegen `garminconnect` 0.3.x (eigen HTTP-client, geen garth meer).
De tokenstring uit `client.dumps()` is JSON; wij base64-encoden die voor de
Streamlit-secret zodat het één regel zonder quotes is.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
import os
from typing import Any, Callable, Optional

import requests
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


# --------------------------------------------------------------------------- #
# Resultaat-objecten
# --------------------------------------------------------------------------- #
@dataclass
class MetricResult:
    """Het resultaat van één Garmin-aanroep voor één metric op één dag.

    `error` is None als het gelukt is. `from_cache` zegt of de data uit de
    lokale dagcache kwam (dan is er geen Garmin-call gedaan).
    """

    metric: str
    date: str
    data: Any = None
    error: Optional[str] = None
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


class GarminClientError(Exception):
    """Foutmelding uit de client-laag die de app netjes kan tonen."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GarminClient:
    """Dunne, geïsoleerde client rond `python-garminconnect` (0.3.x).

    Gebruik:
        client = GarminClient(email=..., password=..., token_b64=...)
        week = client.get_last_7_days()
    """

    # Metrics die per dag worden opgehaald (één call per stuk per dag).
    DAILY_METRICS = ("sleep", "hrv", "heart_rate", "stress", "summary")

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token_b64: Optional[str] = None,
        is_cn: bool = False,
        token_dir: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        today_ttl_minutes: int = 120,
        mfa_callback: Optional[Callable[[], str]] = None,
    ) -> None:
        base = Path(__file__).resolve().parent
        self._email = email or os.environ.get("GARMIN_EMAIL")
        self._password = password or os.environ.get("GARMIN_PASSWORD")
        self._token_b64 = token_b64 or os.environ.get("GARMIN_TOKEN_B64")
        self._is_cn = is_cn
        self._token_dir = token_dir or (base / "data" / "garmin_tokens")
        self._cache_dir = cache_dir or (base / "data" / "cache")
        self._today_ttl = timedelta(minutes=today_ttl_minutes)
        self._mfa_callback = mfa_callback
        self._api: Optional[Garmin] = None

        self._token_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------- Login ----------------------------------- #
    def _mfa(self) -> str:
        if self._mfa_callback is not None:
            return self._mfa_callback()
        raise GarminClientError(
            "Garmin vraagt om een 2FA-code, maar er is geen manier om die in te "
            "voeren. Draai eenmalig lokaal `python scripts/garmin_login.py`, en "
            "zet de getoonde token-string als `garmin_token_b64` in je secrets."
        )

    def _new_api(self) -> Garmin:
        return Garmin(
            email=self._email,
            password=self._password,
            is_cn=self._is_cn,
            prompt_mfa=self._mfa,
        )

    def _build_api(self) -> Garmin:
        api = self._new_api()

        # 1) Token-string uit secret/env (base64 van client.dumps()) — ideaal
        #    voor Streamlit Cloud, waar de schijf vluchtig is.
        if self._token_b64:
            try:
                decoded = base64.b64decode(self._token_b64).decode("utf-8")
                api.login(decoded)  # login() ziet aan de lengte dat dit een token is
                return api
            except Exception:
                # Token verlopen/ongeldig -> probeer de volgende methode.
                api = self._new_api()

        # 2) Tokenmap op schijf: login() laadt bestaande tokens, en bij afwezigheid
        #    doet hij een verse login (met e-mail/wachtwoord + evt. 2FA) en slaat
        #    de tokens daarna op in de map.
        have_token_file = (self._token_dir / "garmin_tokens.json").exists()
        if not have_token_file and not (self._email and self._password):
            raise GarminClientError(
                "Geen geldige tokens en geen e-mail/wachtwoord gevonden. Vul "
                "`garmin_email` en `garmin_password` in je secrets in, of zet "
                "een `garmin_token_b64`."
            )
        try:
            api.login(str(self._token_dir))
            return api
        except GarminConnectAuthenticationError as e:
            raise GarminClientError(f"Inloggen mislukt (verkeerde gegevens?): {e}")
        except GarminConnectTooManyRequestsError as e:
            raise GarminClientError(
                f"Garmin blokkeert tijdelijk wegens te veel logins: {e}. "
                "Wacht even en gebruik bij voorkeur opgeslagen tokens."
            )
        except GarminClientError:
            raise
        except Exception as e:
            if not (self._email and self._password):
                raise GarminClientError(
                    "Geen geldige tokens en geen e-mail/wachtwoord gevonden. Vul "
                    "`garmin_email` en `garmin_password` in je secrets in, of zet "
                    "een `garmin_token_b64`."
                )
            raise GarminClientError(f"Login mislukt: {e}")

    def login(self) -> None:
        """Bouw (lazy) de Garmin-sessie op. Veilig om vaker te roepen."""
        if self._api is None:
            self._api = self._build_api()

    def export_token_b64(self) -> str:
        """Geef de huidige sessie als base64-string (voor de Cloud-secret)."""
        self.login()
        assert self._api is not None
        raw = self._api.client.dumps()  # JSON-string met de sessietokens
        return base64.b64encode(raw.encode("utf-8")).decode("utf-8")

    def get_display_name(self) -> Optional[str]:
        """Korte sanity-check dat de sessie werkt; geeft je Garmin-naam terug."""
        self.login()
        assert self._api is not None
        try:
            return self._api.get_full_name()
        except Exception:
            return None

    # ----------------------------- Caching --------------------------------- #
    @staticmethod
    def _today_str() -> str:
        return date.today().isoformat()

    def _cache_path(self, metric: str, date_str: str) -> Path:
        return self._cache_dir / f"{metric}_{date_str}.json"

    def _read_cache(self, metric: str, date_str: str) -> Optional[Any]:
        path = self._cache_path(metric, date_str)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        # Historische dagen veranderen niet -> cache is altijd goed.
        if date_str < self._today_str():
            return payload.get("data")
        # Vandaag: alleen gebruiken als recent genoeg opgehaald.
        try:
            fetched = datetime.fromisoformat(payload["fetched_at"])
        except (KeyError, ValueError):
            return None
        if datetime.now() - fetched < self._today_ttl:
            return payload.get("data")
        return None

    def _write_cache(self, metric: str, date_str: str, data: Any) -> None:
        path = self._cache_path(metric, date_str)
        try:
            path.write_text(
                json.dumps({"fetched_at": datetime.now().isoformat(), "data": data})
            )
        except (OSError, TypeError):
            pass  # cache is best effort; nooit de app laten vallen hierop

    def clear_cache(self, date_str: Optional[str] = None) -> int:
        """Verwijder cachebestanden (alle, of alleen die van één datum). Aantal terug."""
        removed = 0
        for path in self._cache_dir.glob("*.json"):
            if date_str is None or path.name.endswith(f"_{date_str}.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    # ----------------------------- Fetch ----------------------------------- #
    def _with_retry(self, fn: Callable[[], Any], attempts: int = 2) -> Any:
        last_err: Optional[Exception] = None
        for i in range(attempts):
            try:
                return fn()
            except GarminConnectTooManyRequestsError:
                raise  # nooit opnieuw proberen: maakt de blokkade erger
            except (
                GarminConnectConnectionError,
                requests.exceptions.RequestException,
            ) as e:
                last_err = e
                time.sleep(1.0 * (i + 1))
        assert last_err is not None
        raise last_err

    def _fetch(self, metric: str, date_str: str, fn: Callable[[], Any]) -> MetricResult:
        cached = self._read_cache(metric, date_str)
        if cached is not None:
            return MetricResult(metric, date_str, cached, None, from_cache=True)
        try:
            self.login()
            data = self._with_retry(fn)
            self._write_cache(metric, date_str, data)
            return MetricResult(metric, date_str, data, None, from_cache=False)
        except GarminClientError as e:
            return MetricResult(metric, date_str, None, str(e))
        except Exception as e:  # noqa: BLE001 - per-metric isoleren, niet crashen
            return MetricResult(metric, date_str, None, f"{type(e).__name__}: {e}")

    # ----------------------- Per-metric ophalen ---------------------------- #
    def _api_or_raise(self) -> Garmin:
        self.login()
        assert self._api is not None
        return self._api

    def get_sleep(self, date_str: str) -> MetricResult:
        return self._fetch("sleep", date_str, lambda: self._api_or_raise().get_sleep_data(date_str))

    def get_hrv(self, date_str: str) -> MetricResult:
        return self._fetch("hrv", date_str, lambda: self._api_or_raise().get_hrv_data(date_str))

    def get_heart_rate(self, date_str: str) -> MetricResult:
        return self._fetch("heart_rate", date_str, lambda: self._api_or_raise().get_heart_rates(date_str))

    def get_stress(self, date_str: str) -> MetricResult:
        return self._fetch("stress", date_str, lambda: self._api_or_raise().get_stress_data(date_str))

    def get_summary(self, date_str: str) -> MetricResult:
        """Dagsamenvatting: o.a. stappen, totaal-stress, Body Battery, calorieën."""
        return self._fetch("summary", date_str, lambda: self._api_or_raise().get_user_summary(date_str))

    def get_training_readiness(self, date_str: str) -> MetricResult:
        """Garmin's eigen Training Readiness (score 0–100 + level) — primaire bron."""
        return self._fetch(
            "training_readiness",
            date_str,
            lambda: self._api_or_raise().get_training_readiness(date_str),
        )

    def get_body_battery(self, start_str: str, end_str: str) -> MetricResult:
        # date_str = einddatum (meestal vandaag): zo geldt de dag-TTL en pakt
        # "Data verversen (vandaag)" deze range ook mee.
        return self._fetch(
            f"body_battery_{start_str}",
            end_str,
            lambda: self._api_or_raise().get_body_battery(start_str, end_str),
        )

    def get_activities(self, start_str: str, end_str: str) -> MetricResult:
        # date_str = einddatum (meestal vandaag), zodat nieuwe trainingen van
        # vandaag wél ververst worden i.p.v. permanent gecachet op de startdatum.
        return self._fetch(
            f"activities_{start_str}",
            end_str,
            lambda: self._api_or_raise().get_activities_by_date(start_str, end_str),
        )

    def get_activity_splits(self, activity_id) -> MetricResult:
        # Per-lap splits (bij auto-lap ~ per km). Gecachet op activity-id: een
        # afgeronde activiteit verandert niet meer, dus permanente cache is prima.
        return self._fetch(
            "splits",
            str(activity_id),
            lambda: (self._api_or_raise().get_activity_splits(activity_id) or {}).get(
                "lapDTOs", []
            ),
        )

    def get_daily_metric(self, metric: str, date_str: str) -> MetricResult:
        dispatch = {
            "sleep": self.get_sleep,
            "hrv": self.get_hrv,
            "heart_rate": self.get_heart_rate,
            "stress": self.get_stress,
            "summary": self.get_summary,
        }
        if metric not in dispatch:
            raise ValueError(f"Onbekende metric: {metric}")
        return dispatch[metric](date_str)

    def get_daily_series(
        self, metric: str, start_str: str, end_str: str
    ) -> dict[str, MetricResult]:
        """Eén metric per dag, van start t/m end (inclusief). Gebruikt de dagcache."""
        d0 = date.fromisoformat(start_str)
        d1 = date.fromisoformat(end_str)
        out: dict[str, MetricResult] = {}
        cur = d0
        while cur <= d1:
            ds = cur.isoformat()
            out[ds] = self.get_daily_metric(metric, ds)
            cur += timedelta(days=1)
        return out

    # ----------------------- Samengestelde views --------------------------- #
    def get_history(self, days: int = 28, end: Optional[date] = None) -> dict:
        """Historie voor de wekelijkse analyse (Fase 2).

        HRV en slaap per dag (voor baseline/trend) + alle trainingen over de
        range in één call (voor ACWR en weekvolume).

        Geeft terug:
            {
              "dates": [oudste ... nieuwste],
              "hrv":   {datum: MetricResult},
              "sleep": {datum: MetricResult},
              "activities": MetricResult,   # hele range
            }
        """
        end = end or date.today()
        start = end - timedelta(days=days - 1)
        start_str, end_str = start.isoformat(), end.isoformat()
        dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        return {
            "dates": dates,
            "hrv": self.get_daily_series("hrv", start_str, end_str),
            "sleep": self.get_daily_series("sleep", start_str, end_str),
            "activities": self.get_activities(start_str, end_str),
        }

    def get_readiness_inputs(self, days: int = 28, end: Optional[date] = None) -> dict:
        """Input voor het dagelijkse readiness-advies (Fase 3).

        Historie (voor HRV-baseline, slaaptrend, ACWR) + de dagsamenvatting van
        vandaag (voor Body Battery bij ontwaken en rust-hartslag vs. baseline).
        """
        end = end or date.today()
        return {
            "date": end.isoformat(),
            "history": self.get_history(days=days, end=end),
            "today_summary": self.get_summary(end.isoformat()),
            "today_readiness": self.get_training_readiness(end.isoformat()),
        }

    def get_last_7_days(self, end: Optional[date] = None) -> dict:
        """Haal de ruwe data van de laatste 7 dagen op (Fase 1-scherm).

        Geeft terug:
            {
              "dates": [oudste ... nieuwste],          # 7 datumstrings
              "days":  {datum: {metric: MetricResult}},
              "body_battery": MetricResult,            # hele range
              "activities":   MetricResult,            # hele range
            }
        """
        end = end or date.today()
        dates = [(end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        start_str, end_str = dates[0], dates[-1]

        days: dict[str, dict[str, MetricResult]] = {}
        for d in dates:
            days[d] = {m: self.get_daily_metric(m, d) for m in self.DAILY_METRICS}

        return {
            "dates": dates,
            "days": days,
            "body_battery": self.get_body_battery(start_str, end_str),
            "activities": self.get_activities(start_str, end_str),
        }
