#!/usr/bin/env python3
"""
Tuya Smart Plug -> InfluxDB Collector
Reads power, voltage, current and energy from multiple Antela Smart Plugs
via local Tuya protocol (no cloud required).
"""

import argparse
import os
import logging
from datetime import datetime, timezone

import tinytuya
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

# --- InfluxDB config (from .env) ---
INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET_TUYA", "tuya-strom")

# --- Device list ---
# Add more devices here or via .env
DEVICES = [
    {
        "id":      os.getenv("TUYA_STECKDOSE3_ID",   "bf5ac3a19f3d70391eaqqd"),
        "ip":      os.getenv("TUYA_STECKDOSE3_IP",   "192.168.178.170"),
        "key":     os.getenv("TUYA_STECKDOSE3_KEY"),
        "name":    os.getenv("TUYA_STECKDOSE3_NAME",  "Kaffeemaschine"),
        "version": os.getenv("TUYA_STECKDOSE3_VER",   "3.4"),
    },
    {
        "id":      os.getenv("TUYA_FAHRRAD_ID",   "bf988fce854ac6a1dclefm"),
        "ip":      os.getenv("TUYA_FAHRRAD_IP",   "192.168.178.168"),
        "key":     os.getenv("TUYA_FAHRRAD_KEY"),
        "name":    os.getenv("TUYA_FAHRRAD_NAME",  "Fahrrad"),
        "version": os.getenv("TUYA_FAHRRAD_VER",   "3.4"),
    },
]

# --- DP scaling (from Tuya device mapping) ---
# DP 17 add_ele:     scale=3 -> raw / 1000 = kWh
# DP 18 cur_current: scale=0 -> raw = mA
# DP 19 cur_power:   scale=1 -> raw / 10 = W
# DP 20 cur_voltage: scale=1 -> raw / 10 = V


def read_device(device: dict) -> dict | None:
    """Connect to a Tuya device locally and read current DPS values."""
    log.info("Connecting to %s at %s ...", device["name"], device["ip"])
    try:
        d = tinytuya.OutletDevice(
            dev_id=device["id"],
            address=device["ip"],
            local_key=device["key"],
            version=float(device["version"]),
        )
        d.set_socketPersistent(False)
        data = d.status()

        if "Error" in data:
            raise RuntimeError(data["Error"])

        dps = data.get("dps", {})
        log.debug("Raw DPS for %s: %s", device["name"], dps)
        return dps
    except Exception as e:
        log.error("Failed to read %s: %s", device["name"], e)
        return None


def parse(dps: dict, name: str) -> dict:
    """Convert raw DPS values to human-readable fields with correct scaling."""
    result = {
        "switch":     bool(dps.get("1", False)),
        "current_ma": float(dps.get("18", 0)),
        "power_w":    round(float(dps.get("19", 0)) / 10.0, 1),
        "voltage_v":  round(float(dps.get("20", 0)) / 10.0, 1),
        "energy_kwh": round(float(dps.get("17", 0)) / 1000.0, 3),
    }
    log.info(
        "%s: %s  %.1fV  %.0fmA  %.1fW  %.3f kWh total",
        name,
        "ON" if result["switch"] else "OFF",
        result["voltage_v"],
        result["current_ma"],
        result["power_w"],
        result["energy_kwh"],
    )
    return result


def build_point(data: dict, device: dict) -> Point:
    """Build an InfluxDB Point from parsed device data."""
    device_tag = device["name"].replace(" ", "_")
    return (
        Point("smart_plug")
        .time(datetime.now(timezone.utc), _WP)
        .tag("device",    device_tag)
        .tag("device_id", device["id"])
        .field("switch",      int(data["switch"]))
        .field("current_ma",  data["current_ma"])
        .field("power_w",     data["power_w"])
        .field("voltage_v",   data["voltage_v"])
        .field("energy_kwh",  data["energy_kwh"])
    )


def print_point(p: Point, name: str) -> None:
    """Pretty-print a Point for dry-run mode."""
    line = p.to_line_protocol()
    first_space = line.index(" ")
    second_space = line.index(" ", first_space + 1)
    measurement_tags = line[:first_space]
    fields = line[first_space + 1:second_space]

    print("\n" + "=" * 60)
    print(f"  DRY RUN — {name} (not written to InfluxDB)")
    print("=" * 60)
    print(f"  Measurement : {measurement_tags}")
    for field in fields.split(","):
        print(f"  Field       : {field}")
    print("=" * 60 + "\n")


def write_to_influx(points: list[Point]) -> None:
    """Write a list of Points to InfluxDB."""
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=INFLUX_BUCKET, record=points)
        log.info("Written %d point(s) to InfluxDB bucket '%s'.", len(points), INFLUX_BUCKET)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tuya Smart Plug -> InfluxDB collector")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read devices but print to console instead of writing to InfluxDB",
    )
    args = parser.parse_args()

    points = []
    for device in DEVICES:
        dps = read_device(device)
        if dps is None:
            continue
        data = parse(dps, device["name"])
        point = build_point(data, device)
        if args.dry_run:
            print_point(point, device["name"])
        else:
            points.append(point)

    if not args.dry_run:
        if points:
            write_to_influx(points)
        else:
            log.warning("No data collected — nothing written to InfluxDB.")


if __name__ == "__main__":
    main()