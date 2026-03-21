#!/usr/bin/env python3
"""
Tuya Smart Plug -> InfluxDB Collector
Reads power, voltage, current and energy from Antela Smart Steckdose 3
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

# --- Device config (from .env) ---
DEVICE_ID   = os.getenv("TUYA_STECKDOSE3_ID",  "bf5ac3a19f3d70391eaqqd")
DEVICE_IP   = os.getenv("TUYA_STECKDOSE3_IP",  "192.168.178.170")
DEVICE_KEY  = os.getenv("TUYA_STECKDOSE3_KEY")
DEVICE_NAME = os.getenv("TUYA_STECKDOSE3_NAME", "Steckdose 3")
DEVICE_VER  = os.getenv("TUYA_STECKDOSE3_VER",  "3.4")

# --- InfluxDB config (from .env) ---
INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET_TUYA", "tuya")

# --- DP scaling (from Tuya device mapping) ---
# DP 17 add_ele:    scale=3 -> raw / 1000 = kWh
# DP 18 cur_current: scale=0 -> raw mA
# DP 19 cur_power:  scale=1 -> raw / 10 = W
# DP 20 cur_voltage: scale=1 -> raw / 10 = V


def read_device() -> dict:
    """Connect to Tuya device locally and read current DPS values."""
    log.info("Connecting to %s at %s ...", DEVICE_NAME, DEVICE_IP)

    device = tinytuya.OutletDevice(
        dev_id=DEVICE_ID,
        address=DEVICE_IP,
        local_key=DEVICE_KEY,
        version=float(DEVICE_VER),
    )
    device.set_socketPersistent(False)

    data = device.status()

    if "Error" in data:
        raise RuntimeError(f"Device error: {data['Error']}")

    dps = data.get("dps", {})
    log.debug("Raw DPS: %s", dps)
    return dps


def parse(dps: dict) -> dict:
    """Convert raw DPS values to human-readable fields with correct scaling."""
    result = {
        "switch":          bool(dps.get("1", False)),
        "current_ma":      float(dps.get("18", 0)),
        "power_w":         round(float(dps.get("19", 0)) / 10.0, 1),
        "voltage_v":       round(float(dps.get("20", 0)) / 10.0, 1),
        "energy_kwh":      round(float(dps.get("17", 0)) / 1000.0, 3),
    }

    log.info(
        "%s: %s  %.1fV  %.0fmA  %.1fW  %.3f kWh total",
        DEVICE_NAME,
        "ON" if result["switch"] else "OFF",
        result["voltage_v"],
        result["current_ma"],
        result["power_w"],
        result["energy_kwh"],
    )
    return result


def build_point(data: dict) -> Point:
    """Build an InfluxDB Point from parsed device data."""
    # Tags must not contain spaces — use underscored version for tag,
    # keep original name as a field for display purposes
    device_tag = DEVICE_NAME.replace(" ", "_")
    return (
        Point("smart_plug")
        .time(datetime.now(timezone.utc), _WP)
        .tag("device", device_tag)
        .tag("device_id", DEVICE_ID)
        .field("switch",      int(data["switch"]))
        .field("current_ma",  data["current_ma"])
        .field("power_w",     data["power_w"])
        .field("voltage_v",   data["voltage_v"])
        .field("energy_kwh",  data["energy_kwh"])
    )


def print_point(p: Point) -> None:
    """Pretty-print a Point for dry-run mode."""
    line = p.to_line_protocol()
    # Line protocol format: "measurement,tag=val field=val timestamp"
    # Split on first space to separate measurement+tags from fields
    first_space = line.index(" ")
    second_space = line.index(" ", first_space + 1)
    measurement_tags = line[:first_space]
    fields = line[first_space + 1:second_space]

    print("\n" + "=" * 60)
    print("  DRY RUN — smart_plug point (not written to InfluxDB)")
    print("=" * 60)
    print(f"  Measurement : {measurement_tags}")
    for field in fields.split(","):
        print(f"  Field       : {field}")
    print("=" * 60 + "\n")


def write_to_influx(point: Point) -> None:
    """Write a Point to InfluxDB."""
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        log.info("Written to InfluxDB bucket '%s'.", INFLUX_BUCKET)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tuya Smart Plug -> InfluxDB collector")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read device but print to console instead of writing to InfluxDB",
    )
    args = parser.parse_args()

    try:
        dps = read_device()
    except Exception as e:
        log.error("Failed to read device: %s", e)
        raise SystemExit(1)

    data = parse(dps)
    point = build_point(data)

    if args.dry_run:
        print_point(point)
    else:
        write_to_influx(point)


if __name__ == "__main__":
    main()