#!/usr/bin/env python3
"""
Internet-Uptime-Collector -> InfluxDB
======================================
Misst die Erreichbarkeit des Internetanschlusses (Deutsche Glasfaser)
und schreibt die Werte nach InfluxDB. Laeuft minuetlich
per Cron auf waerme-pi.

Konzept: siehe interne Projektdokumentation.

  pip install fritzconnection influxdb-client python-dotenv --break-system-packages

Aufruf:
  python3 uptime_influx.py              # normaler Lauf, schreibt nach InfluxDB
  python3 uptime_influx.py --dry-run    # misst, schreibt aber nicht

Gemessene Ziele (Measurement `connectivity`, ein Punkt je Ziel und Lauf):
  fritzbox        192.168.178.1              LAN / Pi selbst
  cloudflare_v4   1.1.1.1                    IPv4 nach aussen (DS-Lite-Tunnel)
  google_v4       8.8.8.8                    zweites Netz gegen Einzelausfall
  cloudflare_v6   2606:4700:4700::1111       natives IPv6
  dns             Aufloesung von DNS_CHECK_HOST   DG-Resolver / Anwendungsebene

Daraus abgeleitet (Measurement `wan`, ein Punkt je Lauf):
  internet_up, internet_up_v4, internet_up_v6, is_connected, uptime_s,
  box_uptime_s, influx_write_ok

Ausserdem `outage` (bei Uebergang down->up) und `gap` (nach einer Luecke im
Messbetrieb, mit Klassifizierung power/wan/collector, siehe Abschnitt 6 im
Konzept). Bei einer als "wan" klassifizierten Luecke wird der Ausfall anhand
der Fritz!Box-Verbindungsdauer rueckwirkend als `outage`-Punkt rekonstruiert.

Schreibfehler (VPS nicht erreichbar, das ist gerade der Normalfall waehrend
eines Ausfalls) landen als Line Protocol im lokalen Spool (`spool.lp`) und
werden beim naechsten erfolgreichen Lauf zuerst nachgeschoben.
"""

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from fritzconnection.core.fritzconnection import FritzConnection
from fritzconnection.lib.fritzstatus import FritzStatus

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Breaking change in influxdb-client >= 1.40: SECONDS -> S
_WP = getattr(WritePrecision, "S", None) or getattr(WritePrecision, "SECONDS")

# --- Config (aus /home/admin/.env, geteilt mit den anderen Collectors) ---
FRITZ_ADDRESS = os.getenv("FRITZ_ADDRESS", "192.168.178.1")
FRITZ_USER = os.getenv("FRITZ_USER") or None
FRITZ_PASSWORD = os.getenv("FRITZ_PASSWORD") or None

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG = os.getenv("INFLUX_ORG")
INFLUX_BUCKET_INTERNET = os.getenv("INFLUX_BUCKET_INTERNET", "internet")
# Eigenes Write-Only-Token fuer den Bucket "internet" (separat von INFLUX_TOKEN,
# das die anderen Collectors auf diesem Pi schon fuer ihre Buckets nutzen -
# gleicher Variablenname wuerde das in der gemeinsamen .env ueberschreiben).
INFLUX_TOKEN_INTERNET = os.getenv("INFLUX_TOKEN_INTERNET") or os.getenv("INFLUX_TOKEN")

DNS_CHECK_HOST = os.getenv("DNS_CHECK_HOST", "dein-vps.example.com")
PING_TIMEOUT_S = int(os.getenv("PING_TIMEOUT_S", "2"))
GAP_THRESHOLD_S = int(os.getenv("GAP_THRESHOLD_S", "180"))

STATE_DIR = os.getenv("STATE_DIR", "/home/admin/internet_uptime_data")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SPOOL_FILE = os.path.join(STATE_DIR, "spool.lp")
SPOOL_MAX_BYTES = int(os.getenv("SPOOL_MAX_BYTES", str(10 * 1024 * 1024)))

TARGETS = [
    {"tag": "fritzbox", "type": "ping", "address": FRITZ_ADDRESS, "external": False, "family": "v4"},
    {"tag": "cloudflare_v4", "type": "ping", "address": "1.1.1.1", "external": True, "family": "v4"},
    {"tag": "google_v4", "type": "ping", "address": "8.8.8.8", "external": True, "family": "v4"},
    {"tag": "cloudflare_v6", "type": "ping", "address": "2606:4700:4700::1111", "external": True, "family": "v6"},
    {"tag": "dns", "type": "dns", "address": DNS_CHECK_HOST, "external": True, "family": None},
]

_RTT_RE = re.compile(r"time[=<]([\d.]+)")


# --------------------------------------------------------------------------
# Messungen
# --------------------------------------------------------------------------

def ping(address: str, timeout_s: int, family: str) -> tuple[bool, float | None]:
    """Ein einzelner ICMP-Ping. Gibt (erreichbar, rtt_ms) zurueck."""
    cmd = ["ping"]
    if family == "v6":
        cmd.append("-6")
    elif family == "v4":
        cmd.append("-4")
    cmd += ["-c", "1", "-W", str(timeout_s), address]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s + 3
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug("Ping %s fehlgeschlagen: %s", address, e)
        return False, None

    if result.returncode != 0:
        return False, None

    m = _RTT_RE.search(result.stdout)
    rtt = float(m.group(1)) if m else None
    return True, rtt


def check_dns(hostname: str, timeout_s: int) -> tuple[bool, float | None]:
    """Aufloesung von hostname ueber den System-Resolver. Zeit selbst gestoppt."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_s)
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo(hostname, None)
        return True, (time.perf_counter() - t0) * 1000
    except (socket.gaierror, socket.timeout, OSError) as e:
        log.debug("DNS-Aufloesung %s fehlgeschlagen: %s", hostname, e)
        return False, None
    finally:
        socket.setdefaulttimeout(old_timeout)


def collect_fritz_data() -> dict:
    """TR-064-Werte, jeder Block einzeln abgesichert (siehe Konzept Abschnitt 4).
    Fehlende Werte werden weggelassen, nie auf 0 gesetzt."""
    data: dict = {}

    try:
        kwargs = {"address": FRITZ_ADDRESS}
        if FRITZ_USER:
            kwargs["user"] = FRITZ_USER
        if FRITZ_PASSWORD:
            kwargs["password"] = FRITZ_PASSWORD
        status = FritzStatus(**kwargs)
        data["is_connected"] = int(bool(status.is_connected))
        data["uptime_s"] = int(status.connection_uptime)
    except Exception as e:
        log.warning("FritzStatus nicht erreichbar: %s", e)

    try:
        kwargs = {"address": FRITZ_ADDRESS}
        if FRITZ_USER:
            kwargs["user"] = FRITZ_USER
        if FRITZ_PASSWORD:
            kwargs["password"] = FRITZ_PASSWORD
        fc = FritzConnection(**kwargs)
        info = fc.call_action("DeviceInfo:1", "GetInfo")
        data["box_uptime_s"] = int(info.get("NewUpTime", 0))
    except Exception as e:
        log.warning("Box-Uptime (DeviceInfo) nicht abrufbar: %s", e)

    return data


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# InfluxDB + Spool
# --------------------------------------------------------------------------

def _client() -> InfluxDBClient:
    return InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN_INTERNET, org=INFLUX_ORG, timeout=10_000)


def try_write(points: list) -> bool:
    if not points:
        return True
    try:
        with _client() as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=INFLUX_BUCKET_INTERNET, record=points)
        return True
    except Exception as e:
        log.error("InfluxDB-Write fehlgeschlagen: %s", e)
        return False


def check_spool_cap() -> None:
    if not os.path.exists(SPOOL_FILE):
        return
    if os.path.getsize(SPOOL_FILE) <= SPOOL_MAX_BYTES:
        return
    with open(SPOOL_FILE) as f:
        lines = f.read().splitlines()
    keep = lines[len(lines) // 2 :]
    with open(SPOOL_FILE, "w") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))
    log.warning(
        "Spool-Cap (%d Bytes) erreicht, aelteste %d von %d Zeilen verworfen.",
        SPOOL_MAX_BYTES, len(lines) - len(keep), len(lines),
    )


def spool_append(points: list) -> None:
    if not points:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    check_spool_cap()
    with open(SPOOL_FILE, "a") as f:
        for p in points:
            f.write(p.to_line_protocol() + "\n")
    log.warning("%d Punkt(e) in Spool geschrieben (%s).", len(points), SPOOL_FILE)


def flush_spool() -> None:
    """Vorhandenen Spool zuerst nachschieben, bevor der aktuelle Lauf schreibt."""
    if not os.path.exists(SPOOL_FILE):
        return
    sending = SPOOL_FILE + ".sending"
    os.replace(SPOOL_FILE, sending)
    with open(sending) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    if not lines:
        os.remove(sending)
        return

    sent = 0
    try:
        with _client() as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            for i in range(0, len(lines), 500):
                batch = lines[i : i + 500]
                write_api.write(
                    bucket=INFLUX_BUCKET_INTERNET, record="\n".join(batch), write_precision=_WP
                )
                sent += len(batch)
    except Exception as e:
        log.error("Spool-Flush unterbrochen nach %d/%d Zeilen: %s", sent, len(lines), e)
        remaining = lines[sent:]
        with open(SPOOL_FILE, "a") as f:
            f.write("\n".join(remaining) + "\n")
        os.remove(sending)
        return

    os.remove(sending)
    log.info("Spool geleert: %d Zeile(n) nachgetragen.", sent)


# --------------------------------------------------------------------------
# Ausgabe (dry-run)
# --------------------------------------------------------------------------

def print_points(points: list) -> None:
    print("\n" + "=" * 60)
    print(f"  DRY RUN - {len(points)} Punkt(e) (nicht geschrieben)")
    print("=" * 60)
    for p in points:
        print("  " + p.to_line_protocol())
    print("=" * 60 + "\n")


# --------------------------------------------------------------------------
# Hauptlauf
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Internet-Uptime -> InfluxDB Collector")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Messen, aber nicht nach InfluxDB schreiben und State nicht aktualisieren",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    state = load_state()

    last_run_ts = state.get("last_run_ts")
    gap_s = (now_ts - last_run_ts) if last_run_ts else None

    # 1. Erreichbarkeit pruefen
    connectivity_points = []
    target_status: dict[str, bool] = {}
    for t in TARGETS:
        if t["type"] == "ping":
            up, rtt = ping(t["address"], PING_TIMEOUT_S, t["family"])
        else:
            up, rtt = check_dns(t["address"], PING_TIMEOUT_S)
        target_status[t["tag"]] = up

        p = Point("connectivity").tag("target", t["tag"]).time(now, _WP).field("up", 1 if up else 0)
        if up and rtt is not None:
            p.field("rtt_ms", round(rtt, 1))
        connectivity_points.append(p)
        log.info(
            "%-14s %s%s", t["tag"], "up" if up else "down",
            f"  ({rtt:.1f} ms)" if up and rtt is not None else "",
        )

    external_tags = [t["tag"] for t in TARGETS if t["external"]]
    v4_tags = [t["tag"] for t in TARGETS if t["family"] == "v4" and t["external"]]
    v6_tags = [t["tag"] for t in TARGETS if t["family"] == "v6" and t["external"]]

    internet_up = any(target_status[tag] for tag in external_tags)
    internet_up_v4 = any(target_status[tag] for tag in v4_tags) if v4_tags else False
    internet_up_v6 = any(target_status[tag] for tag in v6_tags) if v6_tags else False

    # 2. Fritz!Box
    fritz = collect_fritz_data()

    wan_point = Point("wan").time(now, _WP)
    if "is_connected" in fritz:
        wan_point.field("is_connected", fritz["is_connected"])
    if "uptime_s" in fritz:
        wan_point.field("uptime_s", fritz["uptime_s"])
    if "box_uptime_s" in fritz:
        wan_point.field("box_uptime_s", fritz["box_uptime_s"])
    wan_point.field("internet_up", 1 if internet_up else 0)
    wan_point.field("internet_up_v4", 1 if internet_up_v4 else 0)
    wan_point.field("internet_up_v6", 1 if internet_up_v6 else 0)
    wan_point.field("influx_write_ok", 1)  # optimistisch, wird bei Schreibfehler auf 0 korrigiert

    # 3. Messluecke seit letztem Lauf?
    gap_points = []
    outage_points = []
    if gap_s is not None and gap_s > GAP_THRESHOLD_S:
        box_uptime = fritz.get("box_uptime_s")
        conn_uptime = fritz.get("uptime_s")
        cause = "unknown"
        if box_uptime is not None and box_uptime < gap_s:
            cause = "power"
        elif box_uptime is not None and conn_uptime is not None and box_uptime > gap_s and conn_uptime < gap_s:
            cause = "wan"
        elif box_uptime is not None and conn_uptime is not None and box_uptime > gap_s and conn_uptime > gap_s:
            cause = "collector"

        gap_points.append(
            Point("gap").time(now, _WP).field("duration_s", int(gap_s)).field("cause", cause)
        )
        log.warning("Messluecke erkannt: %ds seit letztem Lauf, cause=%s", gap_s, cause)

        if cause == "wan" and conn_uptime is not None:
            # Rueckwirkende Rekonstruktion: die Box kennt den Trennungszeitpunkt genau.
            outage_start = now - timedelta(seconds=conn_uptime)
            outage_points.append(
                Point("outage")
                .time(outage_start, _WP)
                .field("duration_s", int(conn_uptime))
                .field("targets_lost", "rekonstruiert_aus_messluecke")
            )
            log.info(
                "Ausfall rueckwirkend rekonstruiert: Start %s, Dauer %ds",
                outage_start.isoformat(), int(conn_uptime),
            )

    # 4. Normale Ausfall-Erkennung (Uebergang down<->up waehrend laufendem Betrieb)
    prev_internet_up = state.get("internet_up", True)
    outage_start_ts = state.get("outage_start_ts")

    if not internet_up and prev_internet_up:
        outage_start_ts = now_ts
        state["outage_targets"] = [t for t in external_tags if not target_status[t]]
        log.warning("Ausfall beginnt (%s down)", ", ".join(state["outage_targets"]) or "?")
    elif internet_up and not prev_internet_up and outage_start_ts:
        duration = now_ts - outage_start_ts
        lost = state.get("outage_targets", [])
        outage_points.append(
            Point("outage")
            .time(now, _WP)
            .field("duration_s", int(duration))
            .field("targets_lost", ",".join(lost) if lost else "unknown")
        )
        log.info("Ausfall beendet nach %ds (%s)", duration, ",".join(lost) or "unknown")
        outage_start_ts = None
        state.pop("outage_targets", None)

    all_points = connectivity_points + [wan_point] + gap_points + outage_points

    # 5. Schreiben oder anzeigen
    if args.dry_run:
        print_points(all_points)
    else:
        flush_spool()
        if not try_write(all_points):
            wan_point.field("influx_write_ok", 0)
            spool_append(all_points)

        state["last_run_ts"] = now_ts
        state["internet_up"] = internet_up
        state["outage_start_ts"] = outage_start_ts
        if "uptime_s" in fritz:
            state["last_connection_uptime_s"] = fritz["uptime_s"]
        save_state(state)


if __name__ == "__main__":
    main()
