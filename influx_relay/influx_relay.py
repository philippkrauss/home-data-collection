#!/usr/bin/env python3
# ============================================================
#  influx_relay.py – HTTP → HTTPS Relay für InfluxDB
#
#  Läuft auf dem Raspberry Pi.
#  ESP8266 schickt plain HTTP hierhin, dieser Relay leitet
#  die Anfrage als HTTPS an den VPS-InfluxDB weiter.
#
#  Starten:   python3 influx_relay.py
#  Autostart: systemd (siehe influx_relay.service)
# ============================================================

import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
import logging
import sys

# --- Konfiguration -----------------------------------------------------------

# Port auf dem der Relay auf dem Pi lauscht (ESP8266 sendet hierhin)
RELAY_HOST = "0.0.0.0"
RELAY_PORT = 8086

# Ziel: VPS InfluxDB (HTTPS)
# Nur Schema + Host + ggf. Port – kein trailing slash!
# Beispiel: "https://influx.meinserver.de" oder "https://1.2.3.4:8086"
INFLUX_TARGET = "https://dein-vps.example.com"

# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Relay] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


class RelayHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        # 1. Request-Body vom ESP8266 lesen
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # 2. Ziel-URL zusammenbauen (Pfad + Query-String übernehmen)
        target_url = INFLUX_TARGET + self.path

        # 3. Relevante Headers weiterleiten
        forward_headers = {}
        for key in ("Authorization", "Content-Type"):
            if key in self.headers:
                forward_headers[key] = self.headers[key]

        # 4. HTTPS-Request an VPS senden
        req = urllib.request.Request(
            target_url,
            data=body,
            headers=forward_headers,
            method="POST"
        )

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                response_body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            response_body = e.read()
            log.warning("VPS antwortete mit HTTP %d: %s", status, response_body[:200])
        except Exception as e:
            log.error("Verbindung zum VPS fehlgeschlagen: %s", e)
            self.send_response(502)
            self.end_headers()
            return

        # 5. Antwort an ESP8266 zurückschicken
        self.send_response(status)
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

        log.info("%s → HTTP %d", self.path.split("?")[0], status)

    def log_message(self, format, *args):
        # Standard-HTTP-Logging unterdrücken (wir loggen selbst)
        pass


# --- Hauptprogramm -----------------------------------------------------------

if __name__ == "__main__":
    if "dein-vps.example.com" in INFLUX_TARGET:
        log.error("INFLUX_TARGET ist noch nicht konfiguriert!")
        log.error("Bitte INFLUX_TARGET in influx_relay.py anpassen.")
        sys.exit(1)

    server = HTTPServer((RELAY_HOST, RELAY_PORT), RelayHandler)
    log.info("Relay gestartet: 0.0.0.0:%d → %s", RELAY_PORT, INFLUX_TARGET)
    log.info("Warte auf Anfragen vom ESP8266...")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Relay gestoppt.")
        server.server_close()