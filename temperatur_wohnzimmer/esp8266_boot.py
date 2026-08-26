# ============================================================
#  boot.py – Läuft beim Start des ESP8266 automatisch
#  Stellt die WLAN-Verbindung her.
#  Diese Datei auf dem ESP8266 als "boot.py" speichern.
# ============================================================

import network
import utime
import config

def connect_wifi():
    sta = network.WLAN(network.STA_IF)
    sta.active(True)

    if sta.isconnected():
        print("[WiFi] Bereits verbunden:", sta.ifconfig()[0])
        return sta

    print("[WiFi] Verbinde mit", config.WIFI_SSID, "...")
    sta.connect(config.WIFI_SSID, config.WIFI_PASSWORD)

    timeout = 20  # Sekunden warten
    while not sta.isconnected() and timeout > 0:
        utime.sleep(1)
        timeout -= 1
        print("[WiFi] Warte... (", timeout, "s)")

    if sta.isconnected():
        ip = sta.ifconfig()[0]
        print("[WiFi] Verbunden! IP:", ip)
    else:
        print("[WiFi] FEHLER: Keine Verbindung nach 20 Sekunden.")
        print("[WiFi] Prüfe SSID und Passwort in config.py")

    return sta

# WLAN beim Boot herstellen
wlan = connect_wifi()
