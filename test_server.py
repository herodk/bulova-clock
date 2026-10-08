"""Replay the clock's captured requests against the stub server.

Headers/paths below are copied verbatim from packet captures and request
logs of a real clock, to catch any response shape/transport
regression the clock would actually hit.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import io
import json
import socket
import threading
from zoneinfo import ZoneInfo

import pytest
from werkzeug.serving import WSGIRequestHandler, make_server

import server
from server import SingleWriteRequestHandler, app

# Real Open-Meteo reply for Copenhagen, probed 2026-10-08 (_open_meteo_url()).
OPEN_METEO_SAMPLE = json.loads(
    '{"latitude":55.6785,"longitude":12.570435,"generationtime_ms":0.52,"utc_offset_seconds":7200,'
    '"timezone":"Europe/Copenhagen","timezone_abbreviation":"GMT+2","elevation":10.0,'
    '"current_units":{"time":"iso8601","interval":"seconds","temperature_2m":"\u00b0C",'
    '"relative_humidity_2m":"%","is_day":"","weather_code":"wmo code"},'
    '"current":{"time":"2026-10-08T19:00","interval":900,"temperature_2m":10.9,'
    '"relative_humidity_2m":72,"is_day":0,"weather_code":3},'
    '"daily_units":{"time":"iso8601","weather_code":"wmo code","temperature_2m_max":"\u00b0C",'
    '"temperature_2m_min":"\u00b0C"},"daily":{"time":["2026-10-08","2026-10-09","2026-10-10",'
    '"2026-10-11","2026-10-12"],"weather_code":[61,53,61,55,51],'
    '"temperature_2m_max":[15.1,13.6,14.6,12.0,12.1],"temperature_2m_min":[10.1,10.2,11.5,8.9,8.6]}}'
)


@pytest.fixture(autouse=True)
def canned_open_meteo(monkeypatch):
    """No network in tests: serve the probed Open-Meteo sample, fresh cache."""
    monkeypatch.setattr(server, "_fetch_open_meteo", lambda: OPEN_METEO_SAMPLE)
    monkeypatch.setattr(server, "_weather_cache", None)
    monkeypatch.setattr(server, "_weather_retry_at", None)

CLOCK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 5.1) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/49.0.2623.112 Safari/537.36"
    ),
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Accept": "*/*",
}


@pytest.fixture
def client():
    app.testing = True
    return app.test_client()


@pytest.mark.parametrize(
    "path",
    # The clock's real paths, with its built-in API keys masked to the same length.
    [
        "/location/ip?ak=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&coor=bd09ll",
        "/locations/v1/cities/geoposition/search?apikey=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&q=22.54,114.06",
        "/currentconditions/v1/999999?apikey=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&details=true",
        "/forecasts/v1/daily/5day/999999?apikey=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    ],
)
def test_canned_response_shape(client, path):
    resp = client.get(path, headers=CLOCK_HEADERS)

    assert resp.status_code == 200
    assert resp.content_type == "application/json"
    assert "Content-Length" in resp.headers
    assert "Transfer-Encoding" not in resp.headers  # no chunked encoding
    assert int(resp.headers["Content-Length"]) == len(resp.data)

    json.loads(resp.data)  # body must parse as JSON


@pytest.mark.parametrize(
    "path",
    [
        "/location/ip?ak=x&coor=bd09ll",
        "/locations/v1/cities/geoposition/search?apikey=x&q=22.54,114.06",
        "/currentconditions/v1/999999?apikey=x&details=true",
        "/forecasts/v1/daily/5day/999999?apikey=x",
    ],
)
def test_canned_response_is_compact(client, path):
    """Real Baidu/AccuWeather replies are compact JSON;
    a naive firmware parser may look for e.g. '"x":"' literally."""
    resp = client.get(path, headers=CLOCK_HEADERS)

    assert b'", "' not in resp.data and b'": ' not in resp.data
    assert resp.data == json.dumps(json.loads(resp.data), separators=(",", ":")).encode()


def test_accuweather_timezone_fields_present(client):
    resp = client.get(
        "/locations/v1/cities/geoposition/search?apikey=x&q=22.54,114.06",
        headers=CLOCK_HEADERS,
    )
    body = json.loads(resp.data)
    tz = body["TimeZone"]

    assert tz["Name"] == "Europe/Copenhagen"
    assert isinstance(tz["GmtOffset"], int)
    assert isinstance(tz["IsDaylightSaving"], bool)
    assert tz["NextOffsetChange"].endswith("Z")


def test_baidu_near_miss_and_unknown_paths(client):
    near_miss = client.get("/locations/v1/cities/geoposition/other")
    assert near_miss.status_code == 200
    json.loads(near_miss.data)

    unknown = client.get("/getWifiInfo")
    assert unknown.status_code == 404

    totally_unknown = client.get("/some/other/path")
    assert totally_unknown.status_code == 404


def _first_recv(handler_class, path: str = "/location/ip?ak=x&coor=bd09ll") -> bytes:
    """Serve the app with `handler_class` on loopback and return what a
    client blocked in a single recv() gets, as the clock's firmware may."""
    srv = make_server("127.0.0.1", 0, app, request_handler=handler_class)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", srv.server_port), timeout=5) as s:
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: stub\r\n\r\n".encode())
            return s.recv(4096)
    finally:
        srv.server_close()


def test_control_stock_werkzeug_splits_headers_and_body():
    """Control: proves the probe can see a split. If this starts failing,
    the test below no longer tells us anything."""
    first = _first_recv(WSGIRequestHandler)
    assert first.endswith(b"\r\n\r\n") and b'"status"' not in first


# A whole reply must fit one Ethernet TCP segment (MSS 1460) for the
# single-segment fix to hold on the real path, not just on loopback.
MSS = 1460


@pytest.mark.parametrize(
    "path",
    [
        "/location/ip?ak=x&coor=bd09ll",
        "/locations/v1/cities/geoposition/search?apikey=x&q=22.54,114.06",
        "/currentconditions/v1/999999?apikey=x&details=true",
        "/forecasts/v1/daily/5day/999999?apikey=x",
    ],
)
def test_reply_arrives_in_one_recv(path):
    first = _first_recv(SingleWriteRequestHandler, path)
    head, _, body = first.partition(b"\r\n\r\n")
    headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:])
    assert len(body) == int(headers[b"Content-Length"]) > 0
    assert len(first) <= MSS


def test_weather_bodies_match_real_api_shapes(client):
    current = json.loads(client.get("/currentconditions/v1/999999?details=true").data)
    assert isinstance(current, list) and len(current) == 1  # real API wraps it
    assert current[0]["Temperature"]["Metric"]["Unit"] == "C"
    assert current[0]["Temperature"]["Imperial"]["Unit"] == "F"

    forecast = json.loads(client.get("/forecasts/v1/daily/5day/999999").data)
    assert len(forecast["DailyForecasts"]) == 5
    assert forecast["DailyForecasts"][0]["Temperature"]["Maximum"]["Unit"] == "F"  # default
    metric = json.loads(client.get("/forecasts/v1/daily/5day/999999?metric=true").data)
    assert metric["DailyForecasts"][0]["Temperature"]["Maximum"]["Unit"] == "C"


def test_open_meteo_sample_maps_to_accuweather(client):
    current = json.loads(client.get("/currentconditions/v1/999999?details=true").data)[0]
    assert (current["WeatherIcon"], current["WeatherText"], current["IsDayTime"]) == (7, "Cloudy", False)
    assert current["Temperature"]["Metric"]["Value"] == 10.9
    assert current["RelativeHumidity"] == 72

    days = json.loads(client.get("/forecasts/v1/daily/5day/999999?metric=true").data)["DailyForecasts"]
    assert [d["Day"]["Icon"] for d in days] == [18, 12, 18, 12, 12]  # WMO 61,53,61,55,51
    assert days[0]["Day"]["HasPrecipitation"] is True
    assert days[0]["Temperature"]["Maximum"]["Value"] == 15.1


def test_worst_case_forecast_fits_one_segment(monkeypatch):
    """Longest phrase and widest temperatures on every day must still fit
    one segment, through the real socket path."""
    longest = max(
        list(server.WMO_TO_ACCUWEATHER.values()) + list(server.WMO_NIGHT.values()),
        key=lambda c: len(c[1]),
    )
    assert len(longest[1]) <= 13
    worst = {
        "current": {"temp_c": -10.4, "humidity": 100, "is_day": True,
                    "icon": longest[0], "text": longest[1], "precip": longest[2]},
        "days": [{"min_c": -40.4, "max_c": 40.3, "day": longest}] * 5,  # -40.7 F: widest both ways
    }
    monkeypatch.setattr(server, "current_weather", lambda: worst)
    for path in ("/forecasts/v1/daily/5day/999999?apikey=x",
                 "/forecasts/v1/daily/5day/999999?apikey=x&metric=true",
                 "/currentconditions/v1/999999?apikey=x&details=true"):
        first = _first_recv(SingleWriteRequestHandler, path)
        assert b"\r\n\r\n" in first and len(first) <= MSS, (path, len(first))


def test_weather_falls_back_to_stale_cache_then_404(client, monkeypatch):
    assert client.get("/currentconditions/v1/999999").status_code == 200  # fills cache

    def down():
        raise OSError("Open-Meteo unreachable")

    monkeypatch.setattr(server, "_fetch_open_meteo", down)
    fetched_at, weather = server._weather_cache
    monkeypatch.setattr(server, "_weather_cache", (fetched_at - server.WEATHER_MAX_AGE, weather))
    assert client.get("/currentconditions/v1/999999").status_code == 200  # stale but served

    monkeypatch.setattr(server, "_weather_cache", (fetched_at - server.WEATHER_STALE_LIMIT, weather))
    assert client.get("/currentconditions/v1/999999").status_code == 404
    assert client.get("/forecasts/v1/daily/5day/999999").status_code == 404


def test_unknown_wmo_code_reports_cloudy():
    assert server._accuweather_condition(1234) == server.WMO_TO_ACCUWEATHER[3]


def test_forecast_dates_follow_dst_change(monkeypatch):
    """A forecast built before DST ends (2026-10-25) must give every day's
    Date at 07:00 Copenhagen wall-clock time, with that day's own offset.
    zoneinfo is the independent reference for the offset."""
    built_at = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    monkeypatch.setattr(server, "_local_now", lambda: built_at)
    weather = {"current": {}, "days": [{"min_c": 1, "max_c": 2, "day": (1, "Sunny", None)}] * 5}
    days = server.accuweather_daily_forecast_body(weather, metric=True)["DailyForecasts"]

    copenhagen = ZoneInfo("Europe/Copenhagen")
    for i, day in enumerate(days):
        date = dt.datetime.fromisoformat(day["Date"])
        local = date.astimezone(copenhagen)
        assert (local.date(), local.hour) == (dt.date(2026, 10, 23 + i), 7), day["Date"]
        assert date.utcoffset() == local.utcoffset(), day["Date"]
        assert day["EpochDate"] == int(date.timestamp())


def test_outage_backs_off_and_serves_stale_without_fetching(client, monkeypatch):
    """After a failed refresh, requests within WEATHER_RETRY_AFTER must serve
    the stale cache at once, without another (slow) fetch attempt."""
    assert client.get("/currentconditions/v1/999999").status_code == 200  # fills cache
    attempts = []

    def down():
        attempts.append(1)
        raise OSError("Open-Meteo unreachable")

    monkeypatch.setattr(server, "_fetch_open_meteo", down)
    fetched_at, weather = server._weather_cache
    monkeypatch.setattr(server, "_weather_cache", (fetched_at - server.WEATHER_MAX_AGE, weather))
    for path in ("/currentconditions/v1/999999", "/forecasts/v1/daily/5day/999999",
                 "/currentconditions/v1/999999"):
        assert client.get(path).status_code == 200  # stale but served
    assert len(attempts) == 1

    monkeypatch.setattr(server, "_weather_retry_at", server._weather_retry_at - server.WEATHER_RETRY_AFTER)
    assert client.get("/currentconditions/v1/999999").status_code == 200
    assert len(attempts) == 2  # backoff over: tried again


@pytest.mark.parametrize("section, field", [("current", "temperature_2m"), ("daily", "temperature_2m_min")])
def test_open_meteo_nulls_count_as_failed_fetch(client, monkeypatch, section, field):
    """Open-Meteo sends null for missing values. That must count as a failed
    fetch (404 with no cache), not get cached and crash the route (500)."""
    sample = json.loads(json.dumps(OPEN_METEO_SAMPLE))
    if section == "daily":
        sample["daily"][field][2] = None
    else:
        sample["current"][field] = None
    monkeypatch.setattr(server, "_fetch_open_meteo", lambda: sample)
    assert client.get("/currentconditions/v1/999999").status_code == 404
    assert client.get("/forecasts/v1/daily/5day/999999").status_code == 404
    assert server._weather_cache is None


def _eu_transitions(year: int) -> list[dt.datetime]:
    """Independent reference: the EU rule, 01:00 UTC on the last Sunday of
    March and October."""
    out = []
    for month in (3, 10):
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
        last -= dt.timedelta(days=(last.weekday() + 1) % 7)
        out.append(dt.datetime.combine(last, dt.time(1), tzinfo=dt.timezone.utc))
    return out


def test_copenhagen_timezone_follows_eu_rule():
    """Hourly through every transition week, 2026-2035, the TimeZone fields
    must match the EU rule, including NextOffsetChange to the second."""
    for year in range(2026, 2036):
        transitions = _eu_transitions(year) + _eu_transitions(year + 1)
        for start in transitions[:2]:
            for h in range(-4 * 24, 4 * 24):
                t = start + dt.timedelta(hours=h, minutes=30)
                start_, end = _eu_transitions(t.year)
                is_dst = start_ <= t < end
                tz = server.timezone_fields(t)
                assert (tz["Code"], tz["GmtOffset"], tz["IsDaylightSaving"]) == (
                    ("CEST", 2, True) if is_dst else ("CET", 1, False)), t
                nxt = min(x for x in transitions if x > t)
                assert tz["NextOffsetChange"] == nxt.strftime("%Y-%m-%dT%H:%M:%SZ"), t


def test_timezone_fields_other_zones(monkeypatch):
    """Half-hour offsets, the southern hemisphere, and zones without DST."""
    jan = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(server, "LOCATION", dataclasses.replace(server.COPENHAGEN, timezone="Asia/Kolkata"))
    assert server.timezone_fields(jan) == {"Code": "IST", "Name": "Asia/Kolkata", "GmtOffset": 5.5,
                                           "IsDaylightSaving": False, "NextOffsetChange": None}
    # Adelaide: DST ends 03:00 ACDT on the first Sunday of April = 16:30 UTC Saturday.
    monkeypatch.setattr(server, "LOCATION", dataclasses.replace(server.COPENHAGEN, timezone="Australia/Adelaide"))
    assert server.timezone_fields(jan) == {"Code": "ACDT", "Name": "Australia/Adelaide", "GmtOffset": 10.5,
                                           "IsDaylightSaving": True, "NextOffsetChange": "2026-04-04T16:30:00Z"}


# Real geocoder reply, probed 2026-10-08 (name=Portland&countryCode=US&count=1).
GEOCODE_PORTLAND = (
    '{"results":[{"id":5746545,"name":"Portland","latitude":45.52345,"longitude":-122.67621,'
    '"elevation":12.0,"feature_code":"PPLA2","country_code":"US","admin1_id":5744337,'
    '"admin2_id":5742126,"timezone":"America/Los_Angeles","population":652503,'
    '"country_id":6252001,"country":"United States","admin1":"Oregon","admin2":"Multnomah"}],'
    '"generationtime_ms":0.77}'
)
PORTLAND = server.Location("Portland", "Oregon", "US", "United States",
                           45.52345, -122.67621, 12.0, "America/Los_Angeles")


def _fake_urlopen(monkeypatch, body: str) -> list[str]:
    urls = []

    def urlopen(url, timeout, context):
        urls.append(url)
        return io.BytesIO(body.encode())

    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    return urls


def test_geocode_parses_real_reply(monkeypatch):
    urls = _fake_urlopen(monkeypatch, GEOCODE_PORTLAND)
    assert server.geocode("Portland", "US") == PORTLAND
    assert "name=Portland" in urls[0] and "countryCode=US" in urls[0]


def test_geocode_no_match_raises(monkeypatch):
    _fake_urlopen(monkeypatch, '{"generationtime_ms":0.11}')  # real no-match reply
    with pytest.raises(LookupError):
        server.geocode("Xyzzyqqq")


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"city": None, "country_code": None, "lat": None, "lon": None, "tz": None, **kw})


def test_location_from_args(monkeypatch):
    _fake_urlopen(monkeypatch, GEOCODE_PORTLAND)
    assert server.location_from_args(_args()) == server.COPENHAGEN
    assert server.location_from_args(_args(city="Portland", country_code="US")) == PORTLAND
    moved = server.location_from_args(_args(city="Portland", lat=45.5, lon=-122.6, tz="America/Boise"))
    assert (moved.latitude, moved.longitude, moved.timezone, moved.city) == (45.5, -122.6, "America/Boise", "Portland")
    with pytest.raises(LookupError):
        server.location_from_args(_args(city="Portland", tz="Mars/Olympus"))


def test_location_reaches_every_reply(client, monkeypatch):
    """No Copenhagen left anywhere once LOCATION is elsewhere."""
    monkeypatch.setattr(server, "LOCATION", PORTLAND)
    url = server._open_meteo_url()
    assert "latitude=45.52345&longitude=-122.67621" in url and "timezone=America/Los_Angeles" in url

    baidu = json.loads(client.get("/location/ip").data)
    assert baidu["address"] == "US|Oregon|Portland|None|None|0"
    assert baidu["content"]["point"] == {"x": "-122.67621", "y": "45.52345"}

    geo = json.loads(client.get("/locations/v1/cities/geoposition/search").data)
    assert (geo["EnglishName"], geo["Country"]["ID"], geo["AdministrativeArea"]["EnglishName"]) == (
        "Portland", "US", "Oregon")
    assert geo["TimeZone"]["Name"] == "America/Los_Angeles"
    assert geo["TimeZone"]["GmtOffset"] in (-7, -8)

    current = json.loads(client.get("/currentconditions/v1/999999").data)[0]
    assert current["LocalObservationDateTime"][-6:] in ("-07:00", "-08:00")
    for path in ("/location/ip", "/locations/v1/cities/geoposition/search",
                 "/currentconditions/v1/999999", "/forecasts/v1/daily/5day/999999"):
        assert b"Copenhagen" not in client.get(path).data and b"+02:00" not in client.get(path).data, path


def test_long_place_names_fit_one_segment(monkeypatch):
    """Geocoded names are not ours to cap: a long, non-ASCII one (escaped to
    6 bytes a character) must still leave the location replies in one segment."""
    monkeypatch.setattr(server, "LOCATION", server.Location(
        "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch", "Région Île-de-France ÉÉÉÉÉÉÉÉÉÉ",
        "GB", "United Kingdom of Great Britain and Northern Ireland",
        -53.123456, -170.123456, 4000.0, "Australia/Lord_Howe"))
    for path in ("/location/ip?ak=x&coor=bd09ll", "/locations/v1/cities/geoposition/search?apikey=x&q=1,2"):
        first = _first_recv(SingleWriteRequestHandler, path)
        assert b"\r\n\r\n" in first and len(first) <= MSS, (path, len(first))
