# ============================================================
#  main.py – Liest DS18B20 und schreibt Daten in InfluxDB
#  Diese Datei auf dem ESP8266 als "main.py" speichern.
# ============================================================

import machine
import onewire
import ds18x20
import urequests
import network
import utime
import gc
import config

# --- Hilfsfunktionen ---

def ensure_wifi():
    """Stellt WLAN-Verbindung wieder her, falls unterbrochen."""
    sta = network.WLAN(network.STA_IF)
    if not sta.isconnected():
        print("[WiFi] Verbindung verloren, verbinde neu...")
        sta.active(True)
        sta.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        for _ in range(20):
            if sta.isconnected():
                break
            utime.sleep(1)
        if sta.isconnected():
            print("[WiFi] Wiederverbunden:", sta.ifconfig()[0])
        else:
            print("[WiFi] Reconnect fehlgeschlagen.")
    return sta.isconnected()


def read_temperature(ds_sensor, roms):
    """Liest Temperatur vom DS18B20. Gibt float oder None zurück."""
    try:
        ds_sensor.convert_temp()
        utime.sleep_ms(750)  # DS18B20 braucht mind. 750ms für Messung
        temp = ds_sensor.read_temp(roms[0])
        return temp
    except Exception as e:
        print("[Sensor] Fehler beim Lesen:", e)
        return None


def write_to_influx(temperature):
    """Sendet Messwert per HTTPS an InfluxDB v2 (VPS)."""
    url = (
        config.INFLUX_URL +
        "/api/v2/write?org=" + config.INFLUX_ORG +
        "&bucket=" + config.INFLUX_BUCKET +
        "&precision=s"
    )

    # InfluxDB Line Protocol: measurement,tag=value field=value timestamp
    # Ohne RTC/NTP kein echter Timestamp → InfluxDB setzt ihn selbst (kein Timestamp angeben)
    line = (
        config.MEASUREMENT +
        ",location=" + config.LOCATION +
        ",room=" + config.ROOM +
        " temperature_c=" + str(round(temperature, 2))
    )

    headers = {
        "Authorization": "Token " + config.INFLUX_TOKEN,
        "Content-Type": "text/plain"
    }

    # RAM vor dem TLS-Handshake freigeben – wichtig auf ESP8266 (wenig Heap)
    gc.collect()

    try:
        response = urequests.post(url, data=line, headers=headers)
        if response.status_code == 204:
            print("[InfluxDB] OK – Daten geschrieben.")
        else:
            print("[InfluxDB] Fehler:", response.status_code, response.text)
        response.close()
        # Nach dem Request wieder aufräumen
        gc.collect()
        return True
    except Exception as e:
        print("[InfluxDB] Verbindungsfehler:", e)
        gc.collect()
        return False


# --- Sensor initialisieren ---

def init_sensor():
    """Initialisiert DS18B20. Gibt (ds_sensor, roms) zurück oder wirft Exception."""
    ds_pin = machine.Pin(config.DS18B20_PIN)
    ds_sensor = ds18x20.DS18X20(onewire.OneWire(ds_pin))
    roms = ds_sensor.scan()

    if not roms:
        raise RuntimeError(
            "Kein DS18B20 gefunden! Prüfe die Verkabelung (Pin D2 / GPIO4)."
        )

    print("[Sensor] DS18B20 gefunden:", len(roms), "Sensor(en)")
    print("[Sensor] ROM-Adresse:", [hex(x) for x in roms[0]])
    return ds_sensor, roms


# --- Hauptschleife ---

print("=== ESP8266 Zimmertemperatur-Logger ===")
print("Ort:       ", config.LOCATION)
print("Bucket:    ", config.INFLUX_BUCKET)
print("Intervall: ", config.INTERVAL_SEC, "Sekunden")
print("")

# Sensor einmal initialisieren
ds_sensor, roms = init_sensor()

errors_in_a_row = 0

while True:
    try:
        # 1. WLAN prüfen
        if not ensure_wifi():
            print("[Main] Kein WLAN – warte 30s und versuche es erneut.")
            utime.sleep(30)
            continue

        # 2. Temperatur messen
        temp = read_temperature(ds_sensor, roms)
        if temp is None:
            errors_in_a_row += 1
            print("[Main] Messfehler", errors_in_a_row, "– überspringe diese Runde.")
            if errors_in_a_row >= 5:
                print("[Main] Zu viele Fehler – starte Sensor neu.")
                ds_sensor, roms = init_sensor()
                errors_in_a_row = 0
            utime.sleep(config.INTERVAL_SEC)
            continue

        errors_in_a_row = 0
        print("[Main] Temperatur:", round(temp, 2), "°C  –  Ort:", config.LOCATION, "/", config.ROOM)

        # 3. In InfluxDB schreiben
        write_to_influx(temp)

    except Exception as e:
        print("[Main] Unerwarteter Fehler:", e)

    # 4. Bis zur nächsten Messung warten
    utime.sleep(config.INTERVAL_SEC)
