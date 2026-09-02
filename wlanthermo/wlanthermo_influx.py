#!/usr/bin/env python3
"""
WLANThermo-Collector -> InfluxDB
=================================
Pollt die lokale HTTP-API eines WLANThermo (Nano V3, `/data`) und schreibt
Kanal-, Pitmaster- und Systemwerte nach InfluxDB. Laeuft als Dauerlaeufer
(systemd) auf waerme-pi, weil das Pollintervall unter einer Minute liegt und
Cron das nicht kann.

  pip install influxdb-client python-dotenv --break-system-packages

Aufruf:
  python3 wlanthermo_influx.py               # Dauerbetrieb
  python3 wlanthermo_influx.py --once        # ein einzelner Zyklus
  python3 wlanthermo_influx.py --dry-run     # pollen und anzeigen, nichts schreiben
  python3 wlanthermo_influx.py --status      # State-Datei anzeigen und beenden

Grundgedanke
------------
Nichts am Geraet muss vorbereitet werden. Einstecken, grillen, fertig - alles
Weitere (Kanalnamen, welche Sonde was war, Benennung des Cooks) laesst sich
hinterher in Grafana entscheiden.

Ist der Nano nicht erreichbar, passiert nichts: kein Punkt, kein Fehler, nur
eine Debug-Zeile. Ist er erreichbar, wird geschrieben, was anliegt.

Session
-------
Jeder Punkt bekommt den Tag `cook` mit einer Session-ID. Die Regel ist bewusst
simpel: **war das Geraet laenger als SESSION_GAP_S (Default 30 min) nicht
erreichbar, beginnt beim naechsten Kontakt eine neue Session.** Ein Neustart
des Collectors oder ein kurzer WLAN-Aussetzer zerreisst damit nichts.

Anfang, Ende und Dauer eines Cooks stehen implizit in den Daten (erster und
letzter Punkt je `cook`) - dafuer braucht es kein eigenes Measurement. Einen
Klartextnamen bekommt ein Cook hinterher ueber eine Grafana-Annotation.

Zusaetzlich traegt jeder Messpunkt `elapsed_s` - Sekunden seit Sessionbeginn.
Damit laesst sich in Grafana ein Trend-Panel "Stunden seit Start" bauen, das
mehrere Cooks uebereinanderlegt. Der Wert wird hier berechnet, weil der
Sessionbeginn hier ohnehin bekannt ist; in Flux waere dasselbe ein Join
ueber ein Aggregat je Cook.

Schema
------
| Measurement     | Tags                        | Felder |
|-----------------|-----------------------------|--------|
| `bbq_channel`   | device, cook, channel       | temp_c, name, elapsed_s, min_c, max_c, alarm, connected, fixed, typ |
| `bbq_pitmaster` | device, cook, pm_id         | value_pct, set_c, elapsed_s, channel, pid, typ, active |
| `bbq_system`    | device, cook                | soc_pct, charge, rssi_dbm, online, unit, device_time_offset_s |

Kanaele mit temp == 999 (kein Fuehler gesteckt) werden nicht geschrieben -
sonst haengen in Grafana Serien auf 999 und ruinieren jede Autoskalierung.

Schreibfehler landen als Line Protocol im Spool und werden beim naechsten
erfolgreichen Zyklus nachgeschoben. Das ist hier kein Luxus: der Anschluss
faellt gelegentlich aus, und ein Longjob dauert 12 Stunden.
"""

import argparse
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

ENV_FILE = os.environ.get("ENV_FILE", os.path.expanduser("~/.env"))
load_dotenv(ENV_FILE)

logging.basicConfig(
    level=getattr(logging, os.getenv("WLANTHERMO_LOGLEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Breaking change in influxdb-client >= 1.40: SECONDS -> S
_WP = getattr(WritePrecision, "S", None) or getattr(WritePrecision, "SECONDS")

# --- Config (aus /home/admin/.env, geteilt mit den anderen Collectors) ---
WT_HOST = os.getenv("WLANTHERMO_HOST", "nanov3")
WT_URL = os.getenv("WLANTHERMO_URL") or "http://%s/data" % WT_HOST
WT_TIMEOUT_S = float(os.getenv("WLANTHERMO_TIMEOUT_S", "3"))
WT_DEVICE = os.getenv("WLANTHERMO_DEVICE", "nanov3")

POLL_INTERVAL_S = int(os.getenv("WLANTHERMO_INTERVAL_S", "10"))
# So lange darf der Nano weg sein, ohne dass eine neue Session beginnt.
SESSION_GAP_S = int(os.getenv("WLANTHERMO_SESSION_GAP_S", "1800"))

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG = os.getenv("INFLUX_ORG")
INFLUX_BUCKET_BBQ = os.getenv("INFLUX_BUCKET_BBQ", "bbq")
# Eigenes Write-Only-Token fuer den Bucket "BBQ" (separat von INFLUX_TOKEN,
# das die anderen Collectors auf diesem Pi schon fuer ihre Buckets nutzen -
# gleicher Variablenname wuerde das in der gemeinsamen .env ueberschreiben).
INFLUX_TOKEN_BBQ = os.getenv("INFLUX_TOKEN_BBQ") or os.getenv("INFLUX_TOKEN")

STATE_DIR = os.getenv("WLANTHERMO_STATE_DIR", "/home/admin/wlanthermo_data")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SPOOL_FILE = os.path.join(STATE_DIR, "spool.lp")
SPOOL_MAX_BYTES = int(os.getenv("WLANTHERMO_SPOOL_MAX_BYTES", str(10 * 1024 * 1024)))

# Der Nano meldet 999 fuer "kein Fuehler gesteckt" - auf allen Kanaelen.
OFF_TEMP = 999.0


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def _f(value):
    """Nach float, oder None wenn das nicht geht."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value):
    v = _f(value)
    return None if v is None else int(v)


def _is_off(temp):
    return temp is None or abs(temp - OFF_TEMP) < 0.01


# --------------------------------------------------------------------------
# Geraet abfragen
# --------------------------------------------------------------------------

def fetch() -> dict | None:
    """GET /data. Gibt None zurueck, wenn das Geraet aus oder nicht da ist."""
    try:
        with urllib.request.urlopen(WT_URL, timeout=WT_TIMEOUT_S) as resp:
            raw = resp.read()
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        # Normalfall: der Nano ist aus. Bewusst nur Debug, sonst stehen hier
        # 8640 Fehlerzeilen pro Tag.
        log.debug("WLANThermo nicht erreichbar (%s): %s", WT_URL, e)
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("Antwort von %s ist kein gueltiges JSON: %s", WT_URL, e)
        return None

    if not isinstance(data, dict) or "channel" not in data:
        log.warning("Unerwartete Antwortstruktur von %s: %.120s", WT_URL, raw)
        return None
    return data


def pitmaster_entries(data: dict) -> list:
    """Firmware liefert {"type": [...], "pm": [...]}; aeltere Staende eine Liste."""
    pm = data.get("pitmaster")
    if isinstance(pm, dict):
        return pm.get("pm") or []
    if isinstance(pm, list):
        return pm
    return []


# --------------------------------------------------------------------------
# Punkte bauen
# --------------------------------------------------------------------------

def build_points(data: dict, cook: str, elapsed_s: int, now: datetime) -> list:
    points = []

    for ch in data.get("channel") or []:
        temp = _f(ch.get("temp"))
        if _is_off(temp):
            continue  # kein Fuehler gesteckt
        p = (
            Point("bbq_channel")
            .time(now, _WP)
            .tag("device", WT_DEVICE)
            .tag("cook", cook)
            # Die Kanalnummer ist die Identitaet - sie steht ohne Zutun fest.
            # Der Name ist bewusst ein Feld: er muss nicht vor dem Grillen
            # gesetzt werden, und ein spaeteres Umbenennen am Geraet spaltet
            # so keine Zeitreihe auf.
            .tag("channel", str(ch.get("number", "?")))
            .field("temp_c", temp)
            .field("name", str(ch.get("name", "")).strip())
            .field("elapsed_s", elapsed_s)
        )
        for src, dst in (("min", "min_c"), ("max", "max_c")):
            v = _f(ch.get(src))
            if v is not None:
                p.field(dst, v)
        for src, dst in (("alarm", "alarm"), ("typ", "typ")):
            v = _i(ch.get(src))
            if v is not None:
                p.field(dst, v)
        # connected/fixed sind noch unklar dokumentiert - mitschreiben, damit
        # spaeter entschieden werden kann, ob sie zu etwas taugen.
        for key in ("connected", "fixed"):
            if key in ch:
                p.field(key, 1 if ch.get(key) else 0)
        points.append(p)

    for pm in pitmaster_entries(data):
        typ = str(pm.get("typ", "off")).lower()
        p = (
            Point("bbq_pitmaster")
            .time(now, _WP)
            .tag("device", WT_DEVICE)
            .tag("cook", cook)
            .tag("pm_id", str(pm.get("id", 0)))
            # typ bewusst als Feld, nicht als Tag: der Modus wechselt waehrend
            # eines Cooks (off -> auto), ein Tag wuerde die Kurve aufspalten.
            .field("typ", typ)
            .field("active", 0 if typ in ("off", "") else 1)
            .field("elapsed_s", elapsed_s)
        )
        for src, dst in (("value", "value_pct"), ("set", "set_c")):
            v = _f(pm.get(src))
            if v is not None:
                p.field(dst, v)
        # channel = welchen Fuehler der Pitmaster regelt. Damit sagen die Daten
        # selbst, welche Sonde der Garraumfuehler war.
        for src, dst in (("channel", "channel"), ("pid", "pid")):
            v = _i(pm.get(src))
            if v is not None:
                p.field(dst, v)
        points.append(p)

    system = data.get("system") or {}
    p = (
        Point("bbq_system")
        .time(now, _WP)
        .tag("device", WT_DEVICE)
        .tag("cook", cook)
        .field("unit", str(system.get("unit", "C")))
    )
    for src, dst in (("soc", "soc_pct"), ("rssi", "rssi_dbm"), ("online", "online")):
        v = _i(system.get(src))
        if v is not None:
            p.field(dst, v)
    p.field("charge", 1 if system.get("charge") else 0)
    dev_time = _i(system.get("time"))
    if dev_time:
        # Uhrzeit des Nano gegen die des Pi. Interessant, falls spaeter Luecken
        # oder Versatz in den Kurven zu erklaeren sind.
        p.field("device_time_offset_s", int(dev_time - now.timestamp()))
    points.append(p)

    return points


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
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# InfluxDB + Spool
# --------------------------------------------------------------------------

def _client() -> InfluxDBClient:
    return InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN_BBQ, org=INFLUX_ORG, timeout=10_000)


def try_write(points: list) -> bool:
    if not points:
        return True
    try:
        with _client() as client:
            client.write_api(write_options=SYNCHRONOUS).write(
                bucket=INFLUX_BUCKET_BBQ, record=points
            )
        return True
    except Exception as e:
        log.error("InfluxDB-Write fehlgeschlagen: %s", e)
        return False


def check_spool_cap() -> None:
    if not os.path.exists(SPOOL_FILE) or os.path.getsize(SPOOL_FILE) <= SPOOL_MAX_BYTES:
        return
    with open(SPOOL_FILE) as f:
        lines = f.read().splitlines()
    keep = lines[len(lines) // 2:]
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


def flush_spool() -> bool:
    """Vorhandenen Spool nachschieben. False, wenn InfluxDB nicht erreichbar war."""
    if not os.path.exists(SPOOL_FILE):
        return True
    sending = SPOOL_FILE + ".sending"
    os.replace(SPOOL_FILE, sending)
    with open(sending) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    if not lines:
        os.remove(sending)
        return True

    sent = 0
    try:
        with _client() as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            for i in range(0, len(lines), 500):
                batch = lines[i:i + 500]
                write_api.write(
                    bucket=INFLUX_BUCKET_BBQ, record="\n".join(batch), write_precision=_WP
                )
                sent += len(batch)
    except Exception as e:
        log.error("Spool-Flush unterbrochen nach %d/%d Zeilen: %s", sent, len(lines), e)
        with open(SPOOL_FILE, "a") as f:
            f.write("\n".join(lines[sent:]) + "\n")
        os.remove(sending)
        return False

    os.remove(sending)
    log.info("Spool geleert: %d Zeile(n) nachgetragen.", sent)
    return True


def print_points(points: list) -> None:
    print("\n" + "=" * 70)
    print("  DRY RUN - %d Punkt(e) (nicht geschrieben)" % len(points))
    print("=" * 70)
    for p in points:
        print("  " + p.to_line_protocol())
    print("=" * 70 + "\n")


# --------------------------------------------------------------------------
# Ein Zyklus
# --------------------------------------------------------------------------

def session_id(state: dict, now: datetime, now_ts: int) -> str:
    """Session-ID bestimmen. Neue Session nach einer Luecke > SESSION_GAP_S."""
    last_seen = state.get("last_seen_ts")
    gap = None if last_seen is None else now_ts - int(last_seen)

    if state.get("session_id") and gap is not None and gap <= SESSION_GAP_S:
        return state["session_id"]

    # Bewusst Ortszeit: die Zeitstempel der Messwerte bleiben UTC, aber die
    # Session-ID ist ein Label zum Wiederfinden - "0500" muss der Uhrzeit
    # entsprechen, zu der man am Grill stand, nicht der UTC-Zeit.
    new_id = now.astimezone().strftime("%Y-%m-%d_%H%M")
    if gap is None:
        log.info("Session %s beginnt (erster Kontakt).", new_id)
    else:
        log.info("Session %s beginnt (Geraet war %d min weg).", new_id, gap // 60)
    state["session_id"] = new_id
    state["session_start_ts"] = now_ts
    return new_id


def cycle(state: dict, dry_run: bool) -> None:
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    data = fetch()
    if not data:
        # Nano aus - nichts zu tun. last_seen_ts bleibt stehen, damit die
        # Luecke beim naechsten Kontakt gemessen werden kann.
        if dry_run:
            print("WLANThermo unter %s nicht erreichbar - nichts zu tun." % WT_URL)
        return

    cook = session_id(state, now, now_ts)
    elapsed_s = now_ts - int(state.get("session_start_ts", now_ts))
    points = build_points(data, cook, elapsed_s, now)

    if dry_run:
        print_points(points)
        return

    spool_ok = flush_spool()
    if not spool_ok or not try_write(points):
        spool_append(points)

    state["last_seen_ts"] = now_ts
    save_state(state)


# --------------------------------------------------------------------------
# Hauptlauf
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="WLANThermo -> InfluxDB Collector")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pollen und anzeigen, nichts schreiben, State unangetastet")
    parser.add_argument("--once", action="store_true",
                        help="Nur ein Zyklus statt Dauerbetrieb")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_S,
                        help="Pollintervall in Sekunden (Default %d)" % POLL_INTERVAL_S)
    parser.add_argument("--status", action="store_true",
                        help="State-Datei ausgeben und beenden")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(load_state(), indent=2))
        return

    if not INFLUX_TOKEN_BBQ and not args.dry_run:
        log.error("Kein INFLUX_TOKEN_BBQ in %s - Abbruch.", ENV_FILE)
        raise SystemExit(1)

    state = load_state() if not args.dry_run else {}

    if args.once or args.dry_run:
        cycle(state, args.dry_run)
        return

    log.info(
        "Start: %s alle %ds -> %s/%s (neue Session nach %d min Luecke)",
        WT_URL, args.interval, INFLUX_URL, INFLUX_BUCKET_BBQ, SESSION_GAP_S // 60,
    )
    next_run = time.monotonic()
    while True:
        try:
            cycle(state, dry_run=False)
        except Exception:
            # Ein Fehler in einem Zyklus darf einen 12-Stunden-Cook nicht beenden.
            log.exception("Zyklus fehlgeschlagen, weiter beim naechsten Intervall.")
        next_run += args.interval
        sleep_s = next_run - time.monotonic()
        if sleep_s < 0:
            next_run = time.monotonic()
            sleep_s = 0
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
