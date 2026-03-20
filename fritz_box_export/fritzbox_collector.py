#!/usr/bin/env python3
"""
Fritz!Box TR-064 -> InfluxDB Collector
Collects internet speed and WiFi statistics and writes them to InfluxDB.
"""

import argparse
import os
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from fritzconnection.core.fritzconnection import FritzConnection
from fritzconnection.lib.fritzstatus import FritzStatus
from influxdb_client import Point, WritePrecision

# Breaking change in influxdb-client >= 1.40: SECONDS -> S
_WP = getattr(WritePrecision, "S", None) or getattr(WritePrecision, "SECONDS")

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# --- Config (from .env) ---
FRITZ_ADDRESS = os.getenv("FRITZ_ADDRESS", "192.168.178.1")
FRITZ_USER    = os.getenv("FRITZ_USER", "")          # optional, often not needed for TR-064
FRITZ_PASSWORD = os.getenv("FRITZ_PASSWORD", "")     # optional for read-only stats

INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET_FRITZBOX = os.getenv("INFLUX_BUCKET_FRITZBOX")


def collect_internet_stats(fc: FritzStatus) -> list[Point]:
    """Collect current and max downstream/upstream rates."""
    points = []
    now = datetime.now(timezone.utc)

    try:
        # Current throughput in bytes/s
        down_bps, up_bps = fc.transmission_rate  # (downstream, upstream)

        # Total bytes transferred since last connect
        total_down = fc.bytes_received
        total_up   = fc.bytes_sent

        # Connection uptime in seconds
        uptime = fc.connection_uptime

        p = (
            Point("internet")
            .time(now, _WP)
            .field("downstream_bps",      int(down_bps))
            .field("upstream_bps",        int(up_bps))
            .field("total_bytes_down",    int(total_down))
            .field("total_bytes_up",      int(total_up))
            .field("connection_uptime_s", int(uptime))
            .field("is_connected",        int(fc.is_connected))
        )
        points.append(p)
        log.info(
            "Internet: down=%d kbps  up=%d kbps",
            down_bps // 1000,
            up_bps // 1000,
        )
    except Exception as e:
        log.error("Failed to collect internet stats: %s", e)

    return points


def collect_wifi_stats(fc: FritzConnection) -> list[Point]:
    """Collect per-band WiFi statistics via FritzConnection (authenticated)."""
    points = []
    now = datetime.now(timezone.utc)

    # TR-064 exposes up to 4 WLAN services (wlanconfig1..4)
    for index in range(1, 5):
        service = f"WLANConfiguration:{index}"
        try:
            info = fc.call_action(service, "GetInfo")

            if not info.get("NewEnable", False):
                log.debug("WLAN service %d is disabled, skipping.", index)
                continue

            ssid     = info.get("NewSSID", f"wlan{index}")
            channel  = info.get("NewChannel", 0)
            standard = info.get("NewStandard", "")

            # Active host count for this band
            assoc = fc.call_action(service, "GetTotalAssociations")
            active_hosts = int(assoc.get("NewTotalAssociations", 0))

            p = (
                Point("wifi")
                .time(now, _WP)
                .tag("ssid",     ssid)
                .tag("standard", standard)
                .field("channel",      int(channel))
                .field("active_hosts", active_hosts)
            )
            points.append(p)
            log.info(
                "WiFi [%s] band=%s ch=%s active_hosts=%d",
                ssid, standard, channel, active_hosts,
            )
        except Exception as e:
            log.debug("WLAN service %d not available: %s", index, e)
            break

    return points


def print_points(points: list[Point]) -> None:
    """Pretty-print collected points to stdout (dry-run mode)."""
    print("\n" + "=" * 60)
    print(f"  DRY RUN — {len(points)} point(s) collected (not written to InfluxDB)")
    print("=" * 60)
    for p in points:
        # Access the internal line-protocol representation for readable output
        line = p.to_line_protocol()
        # Split into measurement+tags, fields, timestamp for readability
        parts = line.split(" ")
        print(f"\n  Measurement : {parts[0]}")
        if len(parts) >= 2:
            for field in parts[1].split(","):
                print(f"  Field       : {field}")
    print("=" * 60 + "\n")


def write_to_influx(points: list[Point]) -> None:
    """Write a list of Points to InfluxDB."""
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    if not points:
        log.warning("No points to write.")
        return

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=INFLUX_BUCKET_FRITZBOX, record=points)
        log.info("Wrote %d point(s) to InfluxDB.", len(points))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fritz!Box -> InfluxDB collector")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data from Fritz!Box but print to console instead of writing to InfluxDB",
    )
    args = parser.parse_args()

    log.info("Connecting to Fritz!Box at %s ...", FRITZ_ADDRESS)

    fritz_kwargs = {"address": FRITZ_ADDRESS}
    if FRITZ_USER:
        fritz_kwargs["user"] = FRITZ_USER
    if FRITZ_PASSWORD:
        fritz_kwargs["password"] = FRITZ_PASSWORD

    try:
        fc_conn = FritzConnection(**fritz_kwargs)
        fc = FritzStatus(**fritz_kwargs)
    except Exception as e:
        log.error("Cannot reach Fritz!Box: %s", e)
        raise SystemExit(1)

    points = []
    points += collect_internet_stats(fc)
    points += collect_wifi_stats(fc_conn)

    if args.dry_run:
        print_points(points)
    else:
        write_to_influx(points)


if __name__ == "__main__":
    main()