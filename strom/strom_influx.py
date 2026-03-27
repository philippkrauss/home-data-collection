#!/usr/bin/env python3
"""
Easymeter Stromzähler -> InfluxDB Collector
Reads electricity meter via IR/SML and writes to InfluxDB.
"""

import argparse
import os
import logging
import serial
from datetime import datetime, timezone

from dotenv import load_dotenv
from influxdb_client import Point, WritePrecision

# Nutzt den bewährten eigenen SML-Parser aus strom_lesen.py (kein smllib,
# da dessen CRC-Prüfung mit diesem Zähler nicht kompatibel ist)
from strom_lesen import read_sml_datagram, extract_values, SML_TIMEOUT as _DEFAULT_TIMEOUT

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Breaking change in influxdb-client >= 1.40: SECONDS -> S
_WP = getattr(WritePrecision, "S", None) or getattr(WritePrecision, "SECONDS")

# --- Config (from .env) ---
SERIAL_PORT   = os.getenv("STROM_PORT", "/dev/ttyUSB0")
SML_TIMEOUT   = int(os.getenv("STROM_TIMEOUT", str(_DEFAULT_TIMEOUT)))

INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET_STROM", "strom")

# OBIS-Code für Wirkenergie Bezug gesamt (1.8.0)
_OBIS_ENERGIE = bytes([0x01, 0x00, 0x01, 0x08, 0x00, 0xFF])


def read_easymeter() -> float:
    """Read Wirkenergie Bezug gesamt (kWh) from Easymeter via SML/IR."""
    log.info("Opening serial port %s ...", SERIAL_PORT)
    ser = serial.Serial(SERIAL_PORT, baudrate=9600, timeout=1)
    ser.setDTR(True)
    ser.setRTS(True)

    try:
        content = read_sml_datagram(ser, timeout=SML_TIMEOUT)
    finally:
        ser.close()

    if content is None:
        raise TimeoutError(
            f"Kein vollständiges SML-Datagramm empfangen nach {SML_TIMEOUT}s "
            f"(Port: {SERIAL_PORT})"
        )

    values = extract_values(content)

    if _OBIS_ENERGIE not in values:
        raise ValueError(
            f"OBIS Wirkenergie Bezug gesamt (01 00 01 08 00 FF) nicht im Datagramm. "
            f"Gefundene Codes: {[b.hex() for b in values]}"
        )

    value_wh, _unit = values[_OBIS_ENERGIE]
    kwh = float(value_wh) / 1000.0
    log.info("Easymeter: Wirkenergie Bezug = %.3f kWh", kwh)
    return kwh


def build_point(energie_kwh: float) -> Point:
    """Build an InfluxDB Point from parsed Easymeter data."""
    return (
        Point("easymeter")
        .field("energie_kwh", energie_kwh)
        .time(datetime.now(timezone.utc), _WP)
    )


def print_point(p: Point) -> None:
    """Pretty-print a Point for dry-run mode."""
    line = p.to_line_protocol()
    parts = line.split(" ")
    print("\n" + "=" * 60)
    print("  DRY RUN — easymeter point (not written to InfluxDB)")
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
    parser = argparse.ArgumentParser(description="Easymeter -> InfluxDB collector")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read from meter but print to console instead of writing to InfluxDB",
    )
    args = parser.parse_args()

    try:
        energie_kwh = read_easymeter()
    except Exception as e:
        log.error("Failed to read Easymeter: %s", e)
        raise SystemExit(1)

    point = build_point(energie_kwh)

    if args.dry_run:
        print_point(point)
    else:
        write_to_influx(point)


if __name__ == "__main__":
    main()