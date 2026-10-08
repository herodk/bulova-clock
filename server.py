"""Local stand-in for the Bulova Connect clock's upstream APIs.

Root cause: the clock's real Baidu IP-geolocation key is
over quota, and its AccuWeather calls get a 301 to https that the clock's
HTTP client never follows. This app impersonates Baidu and AccuWeather so
the clock can sync: canned geolocation for LOCATION (Copenhagen unless
--city says otherwise), the AccuWeather TimeZone fields computed live for
its time zone, and live weather from Open-Meteo. Each reply goes out in one
TCP segment (SingleWriteRequestHandler).

Run as `python server.py --port N [--city NAME]`, not `flask run`, which
bypasses the single-segment handler; point a DNAT rule at that port. The clock also
calls http://<bulova-cloud-ip>:4000/getWifiInfo; it gets a deliberate 404
(a 200 stalls the clock for ~5 min).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import ssl
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import certifi
from flask import Flask, Response, request
from werkzeug.serving import WSGIRequestHandler


@dataclasses.dataclass(frozen=True)
class Location:
    """Where the clock is: feeds the Baidu and AccuWeather bodies, the
    TimeZone fields and the Open-Meteo query."""

    city: str
    region: str
    country_code: str
    country: str
    latitude: float
    longitude: float
    elevation: float
    timezone: str  # IANA name, e.g. "Europe/Copenhagen"


COPENHAGEN = Location(
    "Copenhagen", "Capital Region", "DK", "Denmark", 55.6761, 12.5683, 5.0, "Europe/Copenhagen"
)
LOCATION = COPENHAGEN  # replaced at startup by --city etc.

app = Flask(__name__)
log = logging.getLogger("clock-stub")


class SingleWriteRequestHandler(WSGIRequestHandler):
    """Send headers and body in one sendall(), so a small reply is one TCP
    segment as from a real server. The stock handler's unbuffered socket
    sends the headers first, and a firmware doing a single recv() would then
    see an empty body."""

    wbufsize = -1  # buffered wfile: Werkzeug's flush() after the body sends both


def _gmt_offset(offset: dt.timedelta) -> int | float:
    """Hours as AccuWeather writes them: int for whole hours (Copenhagen's
    1/2, which the clock accepted), float for e.g. India's 5.5."""
    hours = offset / dt.timedelta(hours=1)
    return int(hours) if hours.is_integer() else hours


def _tz() -> ZoneInfo:
    return ZoneInfo(LOCATION.timezone)  # ZoneInfo caches instances by key


def _next_offset_change(now_utc: dt.datetime) -> dt.datetime | None:
    """First instant after `now_utc` with a different UTC offset, to the
    second; None if there is none in the next year (zones without DST).
    zoneinfo has no transition API, so step a day at a time, then bisect."""
    tz = _tz()
    offset = now_utc.astimezone(tz).utcoffset()
    lo = now_utc.replace(microsecond=0)
    for _ in range(366):
        hi = lo + dt.timedelta(days=1)
        if hi.astimezone(tz).utcoffset() != offset:
            break
        lo = hi
    else:
        return None
    while hi - lo > dt.timedelta(seconds=1):
        mid = lo + (hi - lo) // 2
        if mid.astimezone(tz).utcoffset() == offset:
            lo = mid
        else:
            hi = mid
    return hi


def timezone_fields(now_utc: dt.datetime) -> dict:
    """Compute AccuWeather's TimeZone object for LOCATION right now."""
    local = now_utc.astimezone(_tz())
    next_change = _next_offset_change(now_utc)
    return {
        "Code": local.tzname(),
        "Name": LOCATION.timezone,
        "GmtOffset": _gmt_offset(local.utcoffset()),
        "IsDaylightSaving": bool(local.dst()),
        "NextOffsetChange": next_change and next_change.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def accuweather_geoposition_body() -> dict:
    """Trimmed to the core fields, in AccuWeather's real field order (not
    alphabetised), so the reply fits one TCP segment."""
    loc = LOCATION
    tz = timezone_fields(dt.datetime.now(dt.timezone.utc))
    return {
        "Version": 1,
        "Key": "999999",
        "Type": "City",
        "Rank": 15,
        "LocalizedName": loc.city,
        "EnglishName": loc.city,
        "Country": {"ID": loc.country_code, "LocalizedName": loc.country, "EnglishName": loc.country},
        "AdministrativeArea": {
            "ID": "84",  # Copenhagen's; canned like Key, never seen to matter
            "LocalizedName": loc.region,
            "EnglishName": loc.region,
        },
        "TimeZone": tz,
        "GeoPosition": {
            "Latitude": loc.latitude,
            "Longitude": loc.longitude,
            "Elevation": loc.elevation,
        },
    }


# Live weather for the AccuWeather bodies comes from Open-Meteo (free, no
# key). The weather dict the bodies are built from:
#   {"current": {"temp_c", "humidity", "is_day", "icon", "text", "precip"},
#    "days": [{"min_c", "max_c", "day": (icon, phrase, precip)}, ...]}
def _open_meteo_url() -> str:
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LOCATION.latitude}&longitude={LOCATION.longitude}"
        "&current=temperature_2m,relative_humidity_2m,is_day,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min"
        f"&timezone={LOCATION.timezone}&forecast_days=5"
    )


WEATHER_MAX_AGE = dt.timedelta(minutes=30)
WEATHER_STALE_LIMIT = dt.timedelta(hours=6)  # serve stale data this long if Open-Meteo is down
WEATHER_RETRY_AFTER = dt.timedelta(minutes=5)  # after a failed fetch, don't retry sooner

# WMO weather code -> (AccuWeather icon, phrase, precip type or None).
# Phrases are AccuWeather's own, capped at 13 characters to keep the
# forecast inside one TCP segment (see test_worst_case_forecast_fits).
WMO_TO_ACCUWEATHER = {
    0: (1, "Sunny", None),
    1: (2, "Mostly sunny", None),
    2: (3, "Partly sunny", None),
    3: (7, "Cloudy", None),
    45: (11, "Fog", None),
    48: (11, "Fog", None),
    51: (12, "Showers", "Rain"),
    53: (12, "Showers", "Rain"),
    55: (12, "Showers", "Rain"),
    56: (26, "Freezing rain", "Ice"),
    57: (26, "Freezing rain", "Ice"),
    61: (18, "Rain", "Rain"),
    63: (18, "Rain", "Rain"),
    65: (18, "Rain", "Rain"),
    66: (26, "Freezing rain", "Ice"),
    67: (26, "Freezing rain", "Ice"),
    71: (22, "Snow", "Snow"),
    73: (22, "Snow", "Snow"),
    75: (22, "Snow", "Snow"),
    77: (22, "Snow", "Snow"),
    80: (12, "Showers", "Rain"),
    81: (12, "Showers", "Rain"),
    82: (12, "Showers", "Rain"),
    85: (19, "Flurries", "Snow"),
    86: (22, "Snow", "Snow"),
    95: (15, "T-storms", "Rain"),
    96: (15, "T-storms", "Rain"),
    99: (15, "T-storms", "Rain"),
}
# Clear-ish skies have separate night icons in AccuWeather.
WMO_NIGHT = {0: (33, "Clear", None), 1: (34, "Mostly clear", None), 2: (35, "Partly cloudy", None)}


def _accuweather_condition(wmo_code: int, is_day: bool = True) -> tuple:
    if not is_day and wmo_code in WMO_NIGHT:
        return WMO_NIGHT[wmo_code]
    if wmo_code not in WMO_TO_ACCUWEATHER:
        log.warning("Unknown WMO weather code %s -- reporting Cloudy", wmo_code)
    return WMO_TO_ACCUWEATHER.get(wmo_code, WMO_TO_ACCUWEATHER[3])


def weather_from_open_meteo(data: dict) -> dict:
    """Raises ValueError on nulls (Open-Meteo's missing values), so a gap
    counts as a failed fetch rather than being cached."""
    cur, daily = data["current"], data["daily"]
    for name, value in [*cur.items(), *daily.items()]:
        if value is None or (isinstance(value, list) and None in value):
            raise ValueError(f"Open-Meteo returned null for {name}")
    is_day = bool(cur["is_day"])
    icon, text, precip = _accuweather_condition(cur["weather_code"], is_day)
    return {
        "current": {
            "temp_c": cur["temperature_2m"],
            "humidity": cur["relative_humidity_2m"],
            "is_day": is_day,
            "icon": icon,
            "text": text,
            "precip": precip,
        },
        "days": [
            {"min_c": lo, "max_c": hi, "day": _accuweather_condition(code)}
            for code, lo, hi in zip(
                daily["weather_code"], daily["temperature_2m_min"], daily["temperature_2m_max"]
            )
        ],
    }


# certifi's CA bundle: python.org's macOS Python has no system CAs by default.
_TLS = ssl.create_default_context(cafile=certifi.where())


def _fetch_open_meteo() -> dict:
    with urllib.request.urlopen(_open_meteo_url(), timeout=5, context=_TLS) as resp:
        return json.load(resp)


_weather_cache: tuple[dt.datetime, dict] | None = None
_weather_retry_at: dt.datetime | None = None


def current_weather() -> dict | None:
    """Open-Meteo weather, cached for WEATHER_MAX_AGE. If a refresh fails,
    serve the last good data up to WEATHER_STALE_LIMIT old, else None, and
    skip refreshing for WEATHER_RETRY_AFTER: a failing fetch can block for
    seconds, and the clock's own HTTP timeout is unknown."""
    global _weather_cache, _weather_retry_at
    now = dt.datetime.now(dt.timezone.utc)
    if _weather_cache and now - _weather_cache[0] < WEATHER_MAX_AGE:
        return _weather_cache[1]
    if _weather_retry_at is None or now >= _weather_retry_at:
        try:
            _weather_cache = (now, weather_from_open_meteo(_fetch_open_meteo()))
            _weather_retry_at = None
            return _weather_cache[1]
        except Exception:
            log.exception("Open-Meteo fetch failed")
            _weather_retry_at = now + WEATHER_RETRY_AFTER
    if _weather_cache and now - _weather_cache[0] < WEATHER_STALE_LIMIT:
        return _weather_cache[1]
    return None


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _temperature(c: float, metric: bool) -> dict:
    if metric:
        return {"Value": round(c, 1), "Unit": "C", "UnitType": 17}
    return {"Value": round(_c_to_f(c), 1), "Unit": "F", "UnitType": 18}


def _local_now() -> dt.datetime:
    return dt.datetime.now(_tz())


def _local_at_7(day: dt.date) -> dt.datetime:
    """07:00 local time on `day`, with that day's own UTC offset."""
    return dt.datetime.combine(day, dt.time(7), tzinfo=_tz())


def accuweather_current_conditions_body(weather: dict) -> list:
    """`currentconditions/v1/<key>`: a one-element array, as the real API
    returns. Trimmed to the core fields so the reply fits one TCP segment."""
    now = _local_now().replace(second=0, microsecond=0)
    cur = weather["current"]
    return [
        {
            "LocalObservationDateTime": now.isoformat(),
            "EpochTime": int(now.timestamp()),
            "WeatherText": cur["text"],
            "WeatherIcon": cur["icon"],
            "HasPrecipitation": cur["precip"] is not None,
            "PrecipitationType": cur["precip"],
            "IsDayTime": cur["is_day"],
            "Temperature": {
                "Metric": _temperature(cur["temp_c"], metric=True),
                # Whole-degree int, unlike Metric: as the real API (bieniu/accuweather fixture).
                "Imperial": {"Value": round(_c_to_f(cur["temp_c"])), "Unit": "F", "UnitType": 18},
            },
            "RelativeHumidity": cur["humidity"],
        }
    ]


def _day_part(part: tuple) -> dict:
    """Fixed shape for size: the real API would add PrecipitationType and
    PrecipitationIntensity when HasPrecipitation is true."""
    icon, phrase, precip = part
    return {"Icon": icon, "IconPhrase": phrase, "HasPrecipitation": precip is not None}


def accuweather_daily_forecast_body(weather: dict, metric: bool) -> dict:
    """`forecasts/v1/daily/5day/<key>`: Imperial unless `metric=true`, as the
    real API. Cut harder than the other bodies to fit one TCP segment: no
    Headline and no Night, as well as no Sources/links."""
    today = _local_now().date()
    days = []
    for i, day in enumerate(weather["days"][:5]):
        date = _local_at_7(today + dt.timedelta(days=i))
        days.append(
            {
                "Date": date.isoformat(),
                "EpochDate": int(date.timestamp()),
                "Temperature": {
                    "Minimum": _temperature(day["min_c"], metric),
                    "Maximum": _temperature(day["max_c"], metric),
                },
                "Day": _day_part(day["day"]),
            }
        )
    return {"DailyForecasts": days}


def baidu_location_ip_body() -> dict:
    loc = LOCATION
    return {
        "address": f"{loc.country_code}|{loc.region}|{loc.city}|None|None|0",
        "content": {
            "address": loc.city,
            "address_detail": {
                "province": loc.region,
                "city": loc.city,
                "city_code": "999",
                "district": "",
                "street": "",
                "street_number": "",
                "adcode": 0,
            },
            "point": {
                "x": str(loc.longitude),
                "y": str(loc.latitude),
            },
        },
        "status": 0,
    }


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


def geocode(city: str, country_code: str | None = None) -> Location:
    """Look `city` up with Open-Meteo's geocoder (free, no key); the most
    populous match wins, so pass `country_code` to pick e.g. a US Portland."""
    query = {"name": city, "count": 1, "language": "en", "format": "json"}
    if country_code:
        query["countryCode"] = country_code
    url = f"{GEOCODING_URL}?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=10, context=_TLS) as resp:
        results = json.load(resp).get("results")  # absent when nothing matches
    if not results:
        raise LookupError(f"no place called {city!r}" + (f" in {country_code}" if country_code else ""))
    r = results[0]
    return Location(
        city=r["name"],
        region=r.get("admin1", ""),
        country_code=r["country_code"],
        country=r["country"],
        latitude=r["latitude"],
        longitude=r["longitude"],
        elevation=float(r.get("elevation", 0.0)),
        timezone=r["timezone"],
    )


def location_from_args(args: argparse.Namespace) -> Location:
    """COPENHAGEN, or --city looked up, with --lat/--lon/--tz overriding."""
    loc = geocode(args.city, args.country_code) if args.city else COPENHAGEN
    overrides = {"latitude": args.lat, "longitude": args.lon, "timezone": args.tz}
    loc = dataclasses.replace(loc, **{k: v for k, v in overrides.items() if v is not None})
    ZoneInfo(loc.timezone)  # fail at startup, not on the clock's first request
    return loc


def _json_response(data: dict | list) -> Response:
    """json.dumps() preserves dict insertion order; Flask's jsonify() sorts
    keys alphabetically by default, which we deliberately avoid here.
    Compact separators match the real APIs' bytes (no spaces)."""
    return Response(json.dumps(data, separators=(",", ":")), mimetype="application/json")


@app.before_request
def log_every_request() -> None:
    log.info(
        "%s %s from %s\nHeaders: %s",
        request.method,
        request.full_path if request.query_string else request.path,
        request.remote_addr,
        dict(request.headers),
    )


@app.route("/location/ip")
def baidu_location_ip() -> Response:
    return _json_response(baidu_location_ip_body())


@app.route("/locations/v1/cities/geoposition/search")
def accuweather_geoposition_search() -> Response:
    return _json_response(accuweather_geoposition_body())


@app.route("/currentconditions/v1/<key>")
def accuweather_current_conditions(key: str) -> Response:
    weather = current_weather()
    if weather is None:
        return Response(status=404)
    return _json_response(accuweather_current_conditions_body(weather))


@app.route("/forecasts/v1/daily/5day/<key>")
def accuweather_daily_forecast(key: str) -> Response:
    weather = current_weather()
    if weather is None:
        return Response(status=404)
    metric = request.args.get("metric", "").lower() == "true"
    return _json_response(accuweather_daily_forecast_body(weather, metric))


@app.route("/getWifiInfo")
def get_wifi_info() -> Response:
    log.warning("getWifiInfo requested -- no known response schema, returning 404")
    return Response(status=404)


@app.route("/locations/v1/cities/geoposition/<path:_rest>")
def accuweather_near_miss(_rest: str) -> Response:
    log.warning("AccuWeather near-miss path %s -- serving canned response anyway", request.path)
    return _json_response(accuweather_geoposition_body())


@app.route("/<path:unmatched>")
def catch_all(unmatched: str) -> Response:
    log.warning("Unmatched path %s -- no canned response, returning 404", unmatched)
    return Response(status=404)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="Interface to bind (default 0.0.0.0)")
    parser.add_argument("--city", help="Where the clock is, looked up online (default Copenhagen)")
    parser.add_argument("--country-code", help="ISO country code to narrow --city, e.g. US")
    parser.add_argument("--lat", type=float, help="Override the latitude")
    parser.add_argument("--lon", type=float, help="Override the longitude")
    parser.add_argument("--tz", help="Override the IANA time zone, e.g. America/New_York")
    args = parser.parse_args()
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon go together")
    if args.lat is not None and not args.city:
        parser.error("--lat/--lon need --city too, for the place names in the replies")
    if args.country_code and not args.city:
        parser.error("--country-code narrows --city; give --city too")
    try:
        LOCATION = location_from_args(args)
    except (OSError, LookupError, ValueError) as e:  # LookupError covers an unknown --tz
        parser.error(f"location: {e}")
    log.info("Location: %s", LOCATION)
    app.run(host=args.host, port=args.port, request_handler=SingleWriteRequestHandler)
