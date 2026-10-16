"""Kickoff-window weather forecasts for outdoor games from the US National Weather Service (api.weather.gov).

Provider choice (docs/kicker_projection_engine.md, "Weather"): NWS data is a US-government work in the public
domain, free for any use including a monetized site, and needs no key (only an identifying User-Agent). It covers
US venues only and forecasts about 7 days ahead. Open-Meteo's free tier is licensed for NON-commercial use, so it
is not used. No paid provider is used. Games abroad, games beyond the forecast range, and games under a fixed or
retractable roof get no forecast; the model then uses its no-weather inputs and says so.

Window: the game is sampled from kickoff to kickoff + GAME_HOURS (hourly periods overlapping the window), never a
daily average. Values are aggregated over that window (SUMMARY_FIELDS). Wind DIRECTION is recorded for display but
is never a model input: stadium orientation and the kicking direction are not verified, so no head/tail-wind is
derived.

Cache and integrity: every retrieval is appended to `weather_forecasts` (write-once, never updated) with our
retrieval time, the provider's issue time and the window. A forecast is reused while younger than MAX_AGE; an
older one is refetched. If the provider fails, the latest successful snapshot is returned with `stale=True` when it
is younger than STALE_LIMIT, else nothing. Pregame only: a game whose kickoff has passed is never fetched (an
observation must never be passed off as a forecast).
"""

from __future__ import annotations

import hashlib
import json
import re
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

PROVIDER = "nws"
PROVIDER_MODEL = "nws:forecastHourly+gridpoints"
BASE = "https://api.weather.gov"
USER_AGENT = "fantasy-intel kicker projections (github.com/hhavynn/fantasy)"
GAME_HOURS = 3.25
MAX_AGE = timedelta(hours=6)
FORECAST_RANGE = timedelta(hours=156)    # NWS hourly forecasts reach ~6.5 days; later kickoffs are not requested
STALE_LIMIT = timedelta(hours=36)
RETRIES = 3
TIMEOUT_S = 10
SUMMARY_FIELDS = ("wind_mph_mean", "wind_mph_max", "gust_mph_max", "temp_f_mean", "precip_prob_max",
                  "precip_mm_total", "snow_mm_total", "humidity_pct_mean", "visibility_m_min", "wind_direction",
                  "short_forecast", "periods")
UNITS = {"wind_mph_mean": "mph", "wind_mph_max": "mph", "gust_mph_max": "mph", "temp_f_mean": "F",
         "precip_prob_max": "%", "precip_mm_total": "mm", "snow_mm_total": "mm", "humidity_pct_mean": "%",
         "visibility_m_min": "m"}


class WeatherUnavailable(RuntimeError):
    pass


def _utc(dt) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _naive(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def default_http_get(url: str) -> dict:
    import requests

    r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}, timeout=TIMEOUT_S)
    if r.status_code in (429, 500, 502, 503, 504):
        raise ConnectionError(f"NWS {r.status_code} for {url}")
    if r.status_code != 200:
        raise WeatherUnavailable(f"NWS {r.status_code} for {url}")
    return r.json()


def _get(http_get: Callable[[str], dict], url: str, sleep: Callable[[float], None]) -> dict:
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            return http_get(url)
        except WeatherUnavailable:
            raise
        except Exception as err:          # transient: rate limit, 5xx, network
            last = err
            sleep(1.5 * (attempt + 1))
    raise WeatherUnavailable(f"NWS request failed after {RETRIES} attempts: {last}")


def _mph(text) -> list[float]:
    """NWS hourly windSpeed: '10 mph' or '10 to 15 mph'."""
    if text is None:
        return []
    if isinstance(text, (int, float)):
        return [float(text)]
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(text))]


def _duration_hours(iso: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", iso)
    if not m:
        return 1.0
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 24 + h + mi / 60


def _grid_values(grid: dict, field: str, start: datetime, end: datetime) -> list[tuple[float, float]]:
    """(value, overlap hours) for gridpoint layer `field` intervals overlapping [start, end)."""
    out = []
    for v in ((grid.get("properties") or {}).get(field) or {}).get("values") or []:
        if v.get("value") is None:
            continue
        t0s, dur = v["validTime"].split("/")
        t0 = _utc(t0s)
        t1 = t0 + timedelta(hours=_duration_hours(dur))
        overlap = (min(t1, end) - max(t0, start)).total_seconds() / 3600
        if overlap > 0:
            out.append((float(v["value"]), overlap, (t1 - t0).total_seconds() / 3600))
    return out


def summarize(hourly: dict, grid: dict | None, kickoff: datetime) -> dict:
    """Aggregate NWS hourly periods (and gridpoint layers when given) over the game window."""
    start, end = kickoff, kickoff + timedelta(hours=GAME_HOURS)
    periods = []
    for p in (hourly.get("properties") or {}).get("periods") or []:
        t0, t1 = _utc(p["startTime"]), _utc(p["endTime"])
        if t1 > start and t0 < end:
            periods.append(p)
    if not periods:
        raise WeatherUnavailable("kickoff is outside the published hourly forecast range")
    winds = [w for p in periods for w in _mph(p.get("windSpeed"))]
    temps = []
    for p in periods:
        t = p.get("temperature")
        if isinstance(t, dict):
            t = t.get("value")
        if t is not None:
            temps.append(float(t) if (p.get("temperatureUnit") or "F") == "F" else float(t) * 9 / 5 + 32)
    pops = [((p.get("probabilityOfPrecipitation") or {}).get("value")) for p in periods]
    hums = [((p.get("relativeHumidity") or {}).get("value")) for p in periods]
    dirs = [p.get("windDirection") for p in periods if p.get("windDirection")]
    out = {"wind_mph_mean": round(sum(winds) / len(winds), 1) if winds else None,
           "wind_mph_max": max(winds) if winds else None,
           "gust_mph_max": None, "temp_f_mean": round(sum(temps) / len(temps), 1) if temps else None,
           "precip_prob_max": max((x for x in pops if x is not None), default=None),
           "precip_mm_total": None, "snow_mm_total": None,
           "humidity_pct_mean": round(sum(x for x in hums if x is not None) / len([x for x in hums if x is not None]), 1)
           if any(x is not None for x in hums) else None,
           "visibility_m_min": None, "wind_direction": max(set(dirs), key=dirs.count) if dirs else None,
           "short_forecast": sorted({p.get("shortForecast") for p in periods if p.get("shortForecast")}),
           "periods": len(periods)}
    if grid:
        gust = _grid_values(grid, "windGust", start, end)
        if gust:   # km/h -> mph
            out["gust_mph_max"] = round(max(v for v, _, _ in gust) * 0.621371, 1)
        for field, key in (("quantitativePrecipitation", "precip_mm_total"), ("snowfallAmount", "snow_mm_total")):
            vals = _grid_values(grid, field, start, end)
            if vals:   # interval totals, prorated by the overlapping share of each interval
                out[key] = round(sum(v * ov / max(full, 1e-9) for v, ov, full in vals), 1)
        vis = _grid_values(grid, "visibility", start, end)
        if vis:
            out["visibility_m_min"] = round(min(v for v, _, _ in vis))
    return out


def fetch(lat: float, lon: float, kickoff: datetime, *, http_get: Callable[[str], dict] | None = None,
          sleep: Callable[[float], None] = _time.sleep) -> dict:
    """{issued_at, values} for the game window. Raises WeatherUnavailable."""
    get = http_get or default_http_get
    point = _get(get, f"{BASE}/points/{lat:.4f},{lon:.4f}", sleep)
    props = point.get("properties") or {}
    if not props.get("forecastHourly"):
        raise WeatherUnavailable("NWS has no forecast office for this location")
    hourly = _get(get, props["forecastHourly"], sleep)
    grid = None
    if props.get("forecastGridData"):
        try:
            grid = _get(get, props["forecastGridData"], sleep)
        except WeatherUnavailable:
            grid = None        # gusts / precipitation amounts are optional
    issued = (hourly.get("properties") or {}).get("updateTime") or (hourly.get("properties") or {}).get("generatedAt")
    return {"issued_at": _utc(issued) if issued else None, "values": summarize(hourly, grid, kickoff)}


# ----------------------------------------------------------------------------- cache


def _row_id(game_id: str | None, venue_id: str, kickoff: datetime, retrieved: datetime) -> str:
    """Unique per retrieval (a nonce keeps two retrievals in the same instant apart: rows are never overwritten)."""
    key = f"{PROVIDER}|{game_id}|{venue_id}|{kickoff.isoformat()}|{retrieved.isoformat()}|{uuid.uuid4().hex}"
    return hashlib.sha256(key.encode()).hexdigest()


def latest_snapshot(con, game_id: str, kickoff: datetime) -> dict | None:
    from ..db.connection import fetch_dicts, table_exists

    if not table_exists(con, "weather_forecasts"):
        return None
    rows = fetch_dicts(con, """SELECT * FROM weather_forecasts WHERE game_id = ? AND kickoff_utc = ? AND status = 'ok'
                               ORDER BY retrieved_at DESC LIMIT 1""", [game_id, _naive(kickoff)])
    if not rows:
        return None
    r = rows[0]
    if isinstance(r.get("forecast_values"), str):
        r["forecast_values"] = json.loads(r["forecast_values"])
    return r


def _store(con, row: dict) -> None:
    from ..db.connection import insert_rows

    insert_rows(con, "weather_forecasts", [row])


def game_forecast(con, game: dict, venue: dict, kickoff: datetime | None, now: datetime, *,
                  http_get: Callable[[str], dict] | None = None, sleep: Callable[[float], None] = _time.sleep,
                  allow_fetch: bool = True) -> dict:
    """The forecast to use for one game, as {status, used_for_model?, values, retrieved_at, issued_at, age_hours,
    stale, provider, reason}. status: ok | stale | unavailable | not_applicable. Never raises for provider
    problems; never fetches after kickoff; never fetches for an indoor, retractable or non-US venue."""
    now = _utc(now)
    base = {"provider": PROVIDER, "provider_model": PROVIDER_MODEL, "values": None, "retrieved_at": None,
            "issued_at": None, "age_hours": None, "stale": False}
    if venue.get("roof_class") == "fixed":
        return {**base, "status": "not_applicable", "reason": "fixed roof: indoor game"}
    if venue.get("roof_class") == "retractable":
        return {**base, "status": "not_applicable",
                "reason": "retractable roof: game-day roof position unknown, outdoor weather not applied"}
    if venue.get("roof_class") is None or venue.get("lat") is None:
        return {**base, "status": "unavailable", "reason": "venue unknown"}
    if venue.get("country") != "US":
        return {**base, "status": "unavailable", "reason": f"venue outside NWS coverage ({venue.get('country')})"}
    if kickoff is None:
        return {**base, "status": "unavailable", "reason": "kickoff time unknown"}
    kickoff = _utc(kickoff)
    if kickoff <= now:
        return {**base, "status": "unavailable", "reason": "kickoff has passed: no pregame forecast is fetched"}
    if kickoff + timedelta(hours=GAME_HOURS) - now > FORECAST_RANGE:
        return {**base, "status": "unavailable",
                "reason": "kickoff is beyond the NWS hourly forecast range (~6.5 days); a later run fetches it"}
    gid = game.get("game_id")
    cached = latest_snapshot(con, gid, kickoff)

    def shaped(r: dict, stale: bool) -> dict:
        retrieved = _utc(r["retrieved_at"])
        return {**base, "status": "stale" if stale else "ok", "values": r["forecast_values"],
                "retrieved_at": retrieved.isoformat(), "issued_at": _utc(r["issued_at"]).isoformat() if r.get("issued_at") else None,
                "age_hours": round((now - retrieved).total_seconds() / 3600, 1), "stale": stale,
                "reason": None if not stale else "provider unavailable: latest earlier forecast used"}

    if cached and now - _utc(cached["retrieved_at"]) <= MAX_AGE:
        return shaped(cached, False)
    if not allow_fetch:
        if cached and now - _utc(cached["retrieved_at"]) <= STALE_LIMIT:
            return shaped(cached, True)
        return {**base, "status": "unavailable", "reason": "no cached forecast and fetching disabled"}
    window_end = kickoff + timedelta(hours=GAME_HOURS)
    row = {"forecast_id": _row_id(gid, venue["venue_id"], kickoff, now), "provider": PROVIDER, "provider_model": PROVIDER_MODEL,
           "game_id": gid, "venue_id": venue["venue_id"], "latitude": venue["lat"], "longitude": venue["lon"],
           "kickoff_utc": _naive(kickoff), "window_start_utc": _naive(kickoff), "window_end_utc": _naive(window_end),
           "retrieved_at": _naive(now), "units": UNITS}
    try:
        got = fetch(venue["lat"], venue["lon"], kickoff, http_get=http_get, sleep=sleep)
    except Exception as err:      # WeatherUnavailable or anything unexpected from the provider: never fatal
        _store(con, {**row, "status": "error", "error": f"{type(err).__name__}: {err}"[:500], "issued_at": None,
                     "forecast_values": None})
        if cached and now - _utc(cached["retrieved_at"]) <= STALE_LIMIT:
            return shaped(cached, True)
        return {**base, "status": "unavailable", "reason": f"weather provider unavailable ({type(err).__name__})"}
    _store(con, {**row, "status": "ok", "error": None, "issued_at": _naive(got["issued_at"]),
                 "forecast_values": got["values"]})
    return {**base, "status": "ok", "values": got["values"], "retrieved_at": now.isoformat(),
            "issued_at": got["issued_at"].isoformat() if got["issued_at"] else None, "age_hours": 0.0, "reason": None}
