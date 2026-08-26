#!/usr/bin/env python3
"""
Drillisch (winSIM / sim24 / sim.de / PremiumSIM) -> InfluxDB Datenvolumen-Collector

Loggt sich in die Drillisch-"Servicewelt" ein und liest den Datenverbrauch des
laufenden Abrechnungsmonats aus. Schreibt pro Vertrag einen Point nach InfluxDB.

Es gibt KEINE offizielle API. Der Login-Flow und die HTML-Marker sind aus
https://github.com/BergenSoft/scriptable_premiumsim (src/PremiumSim.js) portiert.
Wenn Drillisch die Startseite umbaut, bricht das Parsing -> siehe upstream_watch.py
und den Abschnitt "Wartung" in der README.

Aufruf:
    python3 mobilfunk_influx.py              # alle Accounts, schreibt nach InfluxDB
    python3 mobilfunk_influx.py --dry-run    # nur ausgeben, nichts schreiben
    python3 mobilfunk_influx.py --only person1
    python3 mobilfunk_influx.py --dump-html /tmp/  # Rohes HTML sichern (Debugging)
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from influxdb_client import Point, WritePrecision

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mobilfunk")

# Breaking change in influxdb-client >= 1.40: SECONDS -> S
_WP = getattr(WritePrecision, "S", None) or getattr(WritePrecision, "SECONDS")

# --- Config (aus .env) ---
INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET_MOBILFUNK", "mobilfunk")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = Path(os.getenv("MOBILFUNK_STATE_FILE",
                            str(Path.home() / ".mobilfunk_state.json")))

# Max. eine Fehler-Benachrichtigung pro Account innerhalb dieses Fensters
ALERT_COOLDOWN_S = int(os.getenv("MOBILFUNK_ALERT_COOLDOWN_S", str(12 * 3600)))

UPSTREAM_URL = "https://github.com/BergenSoft/scriptable_premiumsim/commits/main/src/PremiumSim.js"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
              "Gecko/20100101 Firefox/128.0")

TIMEOUT = 20


# ---------------------------------------------------------------------------
# HTML-Marker  --  DAS IST DIE EINZIGE STELLE, DIE BEI EINEM DRILLISCH-UMBAU
# ANGEPASST WERDEN MUSS. Aufbau identisch zu getSubstring() im Upstream-Script:
# eine Liste von Ankern, die der Reihe nach im HTML gesucht werden, danach wird
# bis zum Endmarker gelesen.
# ---------------------------------------------------------------------------
MARKERS_USED = (
    ['id="main"', "e-data_usage_graph-info-numbers", "font-weight-bold", ">"],
    "</span>",
)
MARKERS_TOTAL = (
    ['id="main"', "e-data_usage_graph-info-numbers", "l-txt-small", ">von"],
    "</span>",
)

UNIT_FACTORS_GB = {
    "TB": 1024.0,
    "GB": 1.0,
    "MB": 1.0 / 1024,
    "KB": 1.0 / 1024 / 1024,
    "B":  1.0 / 1024 / 1024 / 1024,
}


class ScrapeError(RuntimeError):
    """Login oder Parsing fehlgeschlagen."""


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def load_accounts() -> list[dict]:
    """
    Liest durchnummerierte Account-Bloecke aus der .env:

        MOBILFUNK_1_NAME     = person1
        MOBILFUNK_1_PROVIDER = winsim.de
        MOBILFUNK_1_USER     = 015112345678
        MOBILFUNK_1_PASSWORD = ...
        MOBILFUNK_1_TARIF_GB = 35     # optional, Fallback wenn Parsing scheitert
    """
    accounts = []
    for i in range(1, 21):
        name = os.getenv(f"MOBILFUNK_{i}_NAME")
        if not name:
            continue
        provider = os.getenv(f"MOBILFUNK_{i}_PROVIDER")
        user     = os.getenv(f"MOBILFUNK_{i}_USER")
        password = os.getenv(f"MOBILFUNK_{i}_PASSWORD")
        if not (provider and user and password):
            log.error("Account %s (%s): PROVIDER/USER/PASSWORD unvollstaendig - uebersprungen",
                      i, name)
            continue
        tarif_gb = os.getenv(f"MOBILFUNK_{i}_TARIF_GB")
        accounts.append({
            "name":     name,
            "provider": provider,
            "user":     user,
            "password": password,
            "tarif_gb": float(tarif_gb) if tarif_gb else None,
        })
    return accounts


# ---------------------------------------------------------------------------
# Parsing-Hilfen
# ---------------------------------------------------------------------------

def extract_between(html: str, anchors: list[str], until: str) -> str:
    """Portierung von getSubstring() aus PremiumSim.js."""
    rest = html
    for anchor in anchors:
        idx = rest.find(anchor)
        if idx == -1:
            raise ScrapeError(f"HTML-Anker nicht gefunden: {anchor!r}")
        rest = rest[idx + len(anchor):]
    end = rest.find(until)
    if end == -1:
        raise ScrapeError(f"Endmarker nicht gefunden: {until!r}")
    return rest[:end].strip()


def to_gb(raw: str) -> float:
    """'1,23 GB' -> 1.23   |   '512 MB' -> 0.5"""
    txt = raw.replace("&nbsp;", " ").replace("\xa0", " ").strip()
    m = re.match(r"^([0-9][0-9.]*(?:,[0-9]+)?)\s*(TB|GB|MB|KB|B)\b", txt, re.IGNORECASE)
    if not m:
        raise ScrapeError(f"Kann Datenmenge nicht parsen: {raw!r}")
    number = m.group(1).replace(".", "").replace(",", ".")
    unit = m.group(2).upper()
    return float(number) * UNIT_FACTORS_GB[unit]


def find_csrf_token(html: str) -> str:
    """CSRF-Token aus dem Login-Formular ziehen (Attributreihenfolge egal)."""
    for tag in re.findall(r"<input[^>]*>", html, re.IGNORECASE):
        if "UserLoginType__token" in tag or "UserLoginType[_token]" in tag:
            m = re.search(r'value="([^"]*)"', tag)
            if m:
                return m.group(1)
    raise ScrapeError("CSRF-Token (UserLoginType__token) nicht im Login-Formular gefunden")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def fetch_usage(account: dict, dump_dir: str | None = None) -> dict:
    provider = account["provider"]
    base = f"https://service.{provider}"

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9",
    })
    session.cookies.set("isCookieAllowed", "true")

    # 1) Login-Seite holen: CSRF-Token + _SID-Cookie
    r = session.get(base + "/", timeout=TIMEOUT)
    r.raise_for_status()
    token = find_csrf_token(r.text)
    sid = session.cookies.get("_SID")
    if not sid:
        raise ScrapeError("_SID-Cookie wurde von der Login-Seite nicht gesetzt")

    # 2) Login absenden -> erwartet 302 auf /start
    r = session.post(
        base + "/public/login_check",
        data={
            "_SID": sid,
            "UserLoginType[alias]":     account["user"],
            "UserLoginType[password]":  account["password"],
            "UserLoginType[logindata]": "",
            "UserLoginType[_token]":    token,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": base,
            "Referer": base + "/",
        },
        allow_redirects=False,
        timeout=TIMEOUT,
    )
    location = r.headers.get("Location", "")
    if r.status_code != 302 or not location.endswith("/start"):
        raise ScrapeError(
            f"Login fehlgeschlagen (HTTP {r.status_code}, Location={location!r}). "
            "Zugangsdaten falsch, Konto gesperrt, oder Login-Formular umgebaut."
        )

    # 3) Verbrauchsseite holen
    r = session.get(
        base + "/mytariff/invoice/showGprsDataUsage",
        headers={"Referer": base + "/start"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    html = r.text

    if dump_dir:
        path = Path(dump_dir) / f"gprs_{account['name']}_{int(time.time())}.html"
        path.write_text(html, encoding="utf-8")
        log.info("HTML gesichert: %s", path)

    # 4) Werte parsen
    used_gb = to_gb(extract_between(html, *MARKERS_USED))
    try:
        total_gb = to_gb(extract_between(html, *MARKERS_TOTAL))
    except ScrapeError:
        if account["tarif_gb"] is None:
            raise
        log.warning("%s: Inklusivvolumen nicht parsebar, nutze Fallback %.0f GB aus .env",
                    account["name"], account["tarif_gb"])
        total_gb = account["tarif_gb"]

    if total_gb <= 0:
        raise ScrapeError(f"Unplausibles Inklusivvolumen: {total_gb}")

    return {
        "used_gb":      round(used_gb, 4),
        "total_gb":     round(total_gb, 4),
        "remaining_gb": round(max(total_gb - used_gb, 0.0), 4),
        "used_pct":     round(used_gb / total_gb * 100, 2),
    }


# ---------------------------------------------------------------------------
# InfluxDB
# ---------------------------------------------------------------------------

def build_point(account: dict, data: dict | None) -> Point:
    p = (
        Point("datenvolumen")
        .time(datetime.now(timezone.utc), _WP)
        .tag("person", account["name"])
        .tag("provider", account["provider"])
    )
    if data is None:
        return p.field("scrape_ok", 0)
    return (
        p.field("used_gb",      float(data["used_gb"]))
         .field("total_gb",     float(data["total_gb"]))
         .field("remaining_gb", float(data["remaining_gb"]))
         .field("used_pct",     float(data["used_pct"]))
         .field("scrape_ok",    1)
    )


def write_points(points: list[Point]) -> None:
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        client.write_api(write_options=SYNCHRONOUS).write(bucket=INFLUX_BUCKET, record=points)
    log.info("%d Point(s) nach InfluxDB '%s' geschrieben.", len(points), INFLUX_BUCKET)


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log.warning("State-Datei nicht schreibbar (%s): %s", STATE_FILE, e)


def notify(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.info("Telegram nicht konfiguriert - Meldung nur im Log: %s", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as e:
        log.error("Telegram-Benachrichtigung fehlgeschlagen: %s", e)


def alert_failure(account: dict, error: Exception, state: dict) -> None:
    """Meldet einen Fehler, aber hoechstens einmal pro Cooldown-Fenster."""
    key = f"last_alert_{account['name']}"
    now = time.time()
    if now - state.get(key, 0) < ALERT_COOLDOWN_S:
        log.info("Fehler-Alert fuer %s unterdrueckt (Cooldown laeuft).", account["name"])
        return
    state[key] = now
    notify(
        f"Mobilfunk-Collector: {account['name']} ({account['provider']}) fehlgeschlagen\n\n"
        f"{type(error).__name__}: {error}\n\n"
        f"Bevor du selbst debuggst - Upstream pruefen:\n{UPSTREAM_URL}"
    )


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Drillisch Datenvolumen -> InfluxDB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur abfragen und ausgeben, nichts nach InfluxDB schreiben")
    parser.add_argument("--only", metavar="NAME",
                        help="Nur diesen Account abfragen")
    parser.add_argument("--dump-html", metavar="DIR",
                        help="Rohes HTML der Verbrauchsseite in DIR ablegen (Debugging)")
    parser.add_argument("--no-jitter", action="store_true",
                        help="Zufaellige Wartezeit zwischen den Accounts abschalten")
    args = parser.parse_args()

    accounts = load_accounts()
    if args.only:
        accounts = [a for a in accounts if a["name"] == args.only]
    if not accounts:
        log.error("Keine Accounts konfiguriert (MOBILFUNK_1_NAME ... in .env).")
        raise SystemExit(2)

    state = load_state()
    points: list[Point] = []
    failures = 0

    for idx, account in enumerate(accounts):
        if idx and not args.no_jitter:
            time.sleep(random.uniform(3, 12))  # nicht im Sekundentakt anklopfen
        try:
            data = fetch_usage(account, args.dump_html)
            log.info("%-8s %-12s %6.2f / %.0f GB  (%.1f %%, noch %.2f GB)",
                     account["name"], account["provider"],
                     data["used_gb"], data["total_gb"],
                     data["used_pct"], data["remaining_gb"])
            points.append(build_point(account, data))
        except Exception as e:  # noqa: BLE001 - ein kaputter Account darf die anderen nicht killen
            failures += 1
            log.error("%s (%s): %s", account["name"], account["provider"], e)
            points.append(build_point(account, None))
            alert_failure(account, e, state)

    save_state(state)

    if args.dry_run:
        for p in points:
            print(p.to_line_protocol())
    else:
        write_points(points)

    raise SystemExit(1 if failures == len(accounts) else 0)


if __name__ == "__main__":
    main()
