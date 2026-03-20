#!/usr/bin/env python3
"""
Open-Meteo -> InfluxDB Weather Collector
Fetches current weather data and writes it to InfluxDB.
No API key required.
"""

import argparse
import os
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from influxdb_client import Point, WritePrecision

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Breaking change in influxdb-client >= 1.40: SECONDS -> S
_WP = getattr(WritePrecision, "S", None) or getattr(WritePrecision, "SECONDS")

# --- Config (from .env) ---
LATITUDE  = float(os.getenv("WEATHER_LAT", "50.1109"))   # Frankfurt default
LONGITUDE = float(os.getenv("WEATHER_LON", "8.6821"))
LOCATION  = os.getenv("WEATHER_LOCATION", "Frankfurt")

INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET_WEATHER = os.getenv("INFLUX_BUCKET_WEATHER", "waerme")

# Open-Meteo current weather variables to fetch
# Full list: https://open-meteo.com/en/docs
VARIABLES = ",".join([
    "temperature_2m",               # Air temperature at 2m (°C)
    "apparent_temperature",         # Feels-like temperature (°C)
    "relative_humidity_2m",         # Relative humidity (%)
    "dew_point_2m",                 # Dew point (°C)
    "precipitation",                # Precipitation last hour (mm)
    "rain",                         # Rain last hour (mm)
    "snowfall",                     # Snowfall last hour (cm)
    "weather_code",                 # WMO weather code
    "cloud_cover",                  # Total cloud cover (%)
    "surface_pressure",             # Atmospheric pressure (hPa)
    "wind_speed_10m",               # Wind speed at 10m (km/h)
    "wind_direction_10m",           # Wind direction (°)
    "wind_gusts_10m",               # Wind gusts (km/h)
    "is_day",                       # 1=day, 0=night
    "sunshine_duration",            # Sunshine duration last hour (s)
    "uv_index",                     # UV index
])

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_weather() -> dict:
    """Fetch current weather from Open-Meteo API."""
    params = {
        "latitude":        LATITUDE,
        "longitude":       LONGITUDE,
        "current":         VARIABLES,
        "timezone":        "auto",
        "forecast_days":   1,
    }
    response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    current = data.get("current", {})
    log.info(
        "Weather at %s: %.1f°C (feels like %.1f°C), humidity=%d%%, wind=%.1f km/h",
        LOCATION,
        current.get("temperature_2m", 0),
        current.get("apparent_temperature", 0),
        current.get("relative_humidity_2m", 0),
        current.get("wind_speed_10m", 0),
    )
    return current


def build_point(current: dict) -> Point:
    """Build an InfluxDB Point from Open-Meteo current weather data."""
    now = datetime.now(timezone.utc)

    p = (
        Point("weather")
        .time(now, _WP)
        .tag("location", LOCATION)
        .tag("latitude",  str(LATITUDE))
        .tag("longitude", str(LONGITUDE))
    )

    field_map = {
        "temperature_2m":       ("temperature_c",        float),
        "apparent_temperature": ("apparent_temperature_c", float),
        "relative_humidity_2m": ("humidity_pct",          float),
        "dew_point_2m":         ("dew_point_c",           float),
        "precipitation":        ("precipitation_mm",      float),
        "rain":                 ("rain_mm",               float),
        "snowfall":             ("snowfall_cm",           float),
        "weather_code":         ("weather_code",          int),
        "cloud_cover":          ("cloud_cover_pct",       float),
        "surface_pressure":     ("pressure_hpa",          float),
        "wind_speed_10m":       ("wind_speed_kmh",        float),
        "wind_direction_10m":   ("wind_direction_deg",    float),
        "wind_gusts_10m":       ("wind_gusts_kmh",        float),
        "is_day":               ("is_day",                int),
        "sunshine_duration":    ("sunshine_duration_s",   float),
        "uv_index":             ("uv_index",              float),
    }

    for api_key, (field_name, cast) in field_map.items():
        value = current.get(api_key)
        if value is not None:
            p = p.field(field_name, cast(value))

    return p


def print_point(p: Point) -> None:
    """Pretty-print a Point for dry-run mode."""
    line = p.to_line_protocol()
    parts = line.split(" ")
    print("\n" + "=" * 60)
    print("  DRY RUN — weather point (not written to InfluxDB)")
    print("=" * 60)
    print(f"  Measurement : {parts[0]}")
    if len(parts) >= 2:
        for field in parts[1].split(","):
            print(f"  Field       : {field}")
    print("=" * 60 + "\n")


def write_to_influx(point: Point) -> None:
    """Write a Point to InfluxDB."""
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=INFLUX_BUCKET_WEATHER, record=point)
        log.info("Written to InfluxDB bucket '%s'.", INFLUX_BUCKET_WEATHER)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open-Meteo -> InfluxDB weather collector")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch weather but print to console instead of writing to InfluxDB",
    )
    args = parser.parse_args()

    try:
        current = fetch_weather()
    except requests.RequestException as e:
        log.error("Failed to fetch weather: %s", e)
        raise SystemExit(1)

    point = build_point(current)

    if args.dry_run:
        print_point(point)
    else:
        write_to_influx(point)


if __name__ == "__main__":
    main()