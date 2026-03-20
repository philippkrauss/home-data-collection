#!/usr/bin/env python3
"""
Landis+Gyr UH50 -> InfluxDB Collector
Reads heat meter via serial and writes to InfluxDB.
"""

import argparse
import os
import logging
import re
import time
import serial
from datetime import datetime, timezone

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
SERIAL_PORT   = os.getenv("UH50_PORT", "/dev/ttyUSB0")

INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET_WAERME", "waerme")


def read_uh50() -> str:
    """Read raw IEC 62056-21 data from UH50 via serial."""
    log.info("Opening serial port %s ...", SERIAL_PORT)
    ser = serial.Serial(
        SERIAL_PORT, baudrate=300, bytesize=7,
        parity="E", stopbits=1, timeout=2,
    )
    ser.write(b"\x00" * 40)
    ser.write(b"/?!\x0D\x0A")
    ser.flush()
    time.sleep(0.5)
    ser.readline()  # skip identification line
    ser.baudrate = 2400

    raw = ""
    try:
        while True:
            line = ser.readline().decode("ascii", errors="ignore")
            raw += line
            if "!" in line:
                break
    finally:
        ser.close()

    log.info("Read %d bytes from UH50.", len(raw))
    return raw


def parse(raw: str) -> dict:
    """Parse UH50 raw data into a dict of field -> float."""
    def val(pattern):
        m = re.search(pattern, raw)
        return float(m.group(1)) if m else None

    data = {
        "energie_kwh":       val(r"6\.8\((\d+)\*kWh\)"),
        "volumen_m3":        val(r"6\.26\((\d+\.\d+)\*m3\)"),
        "leistung_kw":       val(r"6\.6\((\d+\.\d+)\*kW\)"),
        "vorlauf_c":         val(r"9\.4\((\d+\.\d+)\*C&"),
        "ruecklauf_c":       val(r"9\.4\([^)]*&(\d+\.\d+)\*C\)"),
        "durchfluss_m3ph":   val(r"6\.33\((\d+\.\d+)\*m3ph\)"),
        "betriebsstunden_h": val(r"6\.31\((\d+)\*h\)"),
        "fehlerzeit_h":      val(r"6\.32\((\d+)\*h\)"),
    }

    missing = [k for k, v in data.items() if v is None]
    if missing:
        log.warning("Could not parse fields: %s", missing)

    log.info(
        "UH50: energie=%.0f kWh  leistung=%.2f kW  vorlauf=%.1f°C  ruecklauf=%.1f°C",
        data.get("energie_kwh") or 0,
        data.get("leistung_kw") or 0,
        data.get("vorlauf_c") or 0,
        data.get("ruecklauf_c") or 0,
    )
    return data


def build_point(data: dict) -> Point:
    """Build an InfluxDB Point from parsed UH50 data."""
    p = Point("uh50").time(datetime.now(timezone.utc), _WP)
    for key, value in data.items():
        if value is not None:
            p = p.field(key, value)
    return p


def print_point(p: Point) -> None:
    """Pretty-print a Point for dry-run mode."""
    line = p.to_line_protocol()
    parts = line.split(" ")
    print("\n" + "=" * 60)
    print("  DRY RUN — uh50 point (not written to InfluxDB)")
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
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        log.info("Written to InfluxDB bucket '%s'.", INFLUX_BUCKET)


def main() -> None:
    parser = argparse.ArgumentParser(description="UH50 -> InfluxDB collector")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read from UH50 but print to console instead of writing to InfluxDB",
    )
    args = parser.parse_args()

    try:
        raw = read_uh50()
    except Exception as e:
        log.error("Failed to read UH50: %s", e)
        raise SystemExit(1)

    data = parse(raw)
    point = build_point(data)

    if args.dry_run:
        print_point(point)
    else:
        write_to_influx(point)


if __name__ == "__main__":
    main()