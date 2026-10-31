"""Weather normalisation for usage anomalies (B12).

Degree days are computed per bill period from daily mean temperatures (degrees F):
CDD = max(0, T - base), HDD = max(0, base - T), summed over every day of the period
(bill_from through bill_to inclusive). A usage_spike (increase) is marked
`weather_consistent` when the period's total degree days (CDD + HDD) reach the top
quartile of the account's periods; otherwise it stays `unexplained`. This is context
for a reviewer, not a verdict.
"""

import argparse
import copy
from datetime import date, timedelta
from decimal import Decimal
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEATHER_PATH = ROOT / "data" / "external" / "weather-daily.json"
BASE_TEMPERATURE_F = Decimal("65")
TOP_QUARTILE = Decimal("0.75")
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

WEATHER_CONSISTENT = "weather_consistent"
UNEXPLAINED = "unexplained"


class WeatherError(ValueError):
    """The weather data cannot be used."""


def load_weather(path):
    """Read an Open-Meteo style daily file into {date: Decimal mean temperature F}."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        times = payload["daily"]["time"]
        temps = payload["daily"]["temperature_2m_mean"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise WeatherError(f"Weather file {path} is not a usable daily series: {error!r}") from error
    if len(times) != len(temps):
        raise WeatherError(f"Weather file {path} has {len(times)} dates but {len(temps)} temperatures.")
    series = {}
    for day, temp in zip(times, temps):
        if temp is not None:
            series[date.fromisoformat(day)] = Decimal(str(temp))
    return series


def load_default_weather():
    """The fetched series when its file exists, else None (annotation is then skipped)."""
    if not Path(DEFAULT_WEATHER_PATH).exists():
        return None
    return load_weather(DEFAULT_WEATHER_PATH)


def degree_days(weather, bill_from, bill_to, base=BASE_TEMPERATURE_F):
    """(cdd, hdd, missing_days) for the inclusive period; sums are None if any day is missing."""
    start = date.fromisoformat(str(bill_from))
    end = date.fromisoformat(str(bill_to))
    cdd = hdd = Decimal(0)
    missing = 0
    day = start
    while day <= end:
        temp = weather.get(day)
        if temp is None:
            missing += 1
        else:
            cdd += max(Decimal(0), temp - base)
            hdd += max(Decimal(0), base - temp)
        day += timedelta(days=1)
    if missing:
        return None, None, missing
    return cdd, hdd, 0


def quantile(values, q):
    """Linear-interpolation quantile of Decimals (inclusive method)."""
    ordered = sorted(values)
    if not ordered:
        raise WeatherError("No values to take a quantile of.")
    position = (len(ordered) - 1) * Decimal(q)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def annotate(anomalies, weather, base=BASE_TEMPERATURE_F, top_quartile=TOP_QUARTILE):
    """Return a copy of the anomaly result with degree days per bill and spike annotations."""
    result = copy.deepcopy(anomalies)
    periods = {}
    for bill in result["bills"]:
        cdd, hdd, missing = degree_days(weather, bill["bill_from"], bill["bill_to"], base)
        bill["weather"] = {
            "cdd": cdd,
            "hdd": hdd,
            "degree_days": None if cdd is None else cdd + hdd,
            "missing_days": missing,
        }
        if cdd is not None:
            periods[bill["bill_id"]] = cdd + hdd

    summary = {
        "base_temperature_f": base,
        "periods_with_data": len(periods),
        "periods_total": len(result["bills"]),
        "rule": (
            "A usage_spike increase is weather_consistent when the period's degree days "
            "(CDD + HDD) are at or above the top-quartile value across the account's periods."
        ),
    }
    threshold = None
    if periods:
        values = list(periods.values())
        threshold = quantile(values, top_quartile)
        summary.update(
            q1=quantile(values, Decimal("0.25")),
            median=quantile(values, Decimal("0.5")),
            top_quartile_threshold=threshold,
        )
    result["weather"] = summary

    for bill in result["bills"]:
        for flag in bill["flags"]:
            if flag["flag"] != "usage_spike":
                continue
            dd = periods.get(bill["bill_id"])
            if flag["numbers"]["direction"] != "increase":
                flag["weather"] = {"annotation": UNEXPLAINED, "why": "a decrease is not weather-annotated"}
            elif dd is None:
                flag["weather"] = {
                    "annotation": UNEXPLAINED,
                    "why": f"weather data incomplete for the period ({bill['weather']['missing_days']} day(s) missing)",
                    "degree_days": None,
                }
            elif dd >= threshold:
                flag["weather"] = {
                    "annotation": WEATHER_CONSISTENT,
                    "why": f"period degree days {dd} >= top-quartile value {threshold}",
                    "degree_days": dd,
                }
            else:
                flag["weather"] = {
                    "annotation": UNEXPLAINED,
                    "why": f"period degree days {dd} < top-quartile value {threshold}",
                    "degree_days": dd,
                }
    return result


def fetch_weather(start, end, latitude, longitude, path=DEFAULT_WEATHER_PATH):
    """Fetch daily mean temperatures (F) from the Open-Meteo archive once and store them as JSON."""
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start,
            "end_date": end,
            "daily": "temperature_2m_mean",
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
        }
    )
    with urllib.request.urlopen(f"{ARCHIVE_URL}?{query}", timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    daily = payload.get("daily") or {}
    if not daily.get("time") or "temperature_2m_mean" not in daily:
        raise WeatherError("The archive response has no daily temperature series.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, len(daily["time"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch daily mean temperatures once into data/external/.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--lat", required=True)
    parser.add_argument("--lon", required=True)
    parser.add_argument("--out", default=str(DEFAULT_WEATHER_PATH))
    args = parser.parse_args(argv)
    try:
        path, days = fetch_weather(args.start, args.end, args.lat, args.lon, args.out)
    except (OSError, ValueError) as error:
        print(f"Weather fetch failed: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {path} ({days} daily values)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
