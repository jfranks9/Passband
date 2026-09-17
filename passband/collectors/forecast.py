"""Daily NWS point forecast for the email header. Deterministic, no LLM.

Two calls: /points/{lat},{lon} resolves the gridpoint forecast URL, then the
forecast itself. Failure of either degrades to no header line — the forecast
must never break a send. Severe weather is the Alerts layer's job; this line
covers ordinary days.
"""
from __future__ import annotations

import requests

from ..config import config

HEADERS = {"User-Agent": "passband (personal newsletter pipeline)",
           "Accept": "application/geo+json"}


def forecast_line(timeout: int = 20) -> str | None:
    geo = config().get("geo", {})
    point = geo.get("forecast_point")
    if not point or len(point) != 2:
        return None
    try:
        lat, lon = point
        meta = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                            headers=HEADERS, timeout=timeout)
        meta.raise_for_status()
        url = meta.json()["properties"]["forecast"]
        fc = requests.get(url, headers=HEADERS, timeout=timeout)
        fc.raise_for_status()
        periods = fc.json()["properties"]["periods"]
        if not periods:
            return None
        p = periods[0]
        wind = f", wind {p['windSpeed']} {p['windDirection']}" if p.get("windSpeed") else ""
        return (f"{p['name']}: {p['shortForecast']}, "
                f"{p['temperature']}°{p['temperatureUnit']}{wind}")
    except Exception as exc:
        print(f"forecast: skipped ({exc})")
        return None
