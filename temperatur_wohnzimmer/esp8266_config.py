# ============================================================
#  Konfiguration – ESP8266 Zimmertemperatur → InfluxDB
#  Diese Datei auf dem ESP8266 als "config.py" speichern.
# ============================================================

# --- WiFi ---
WIFI_SSID     = "..."   # eigene Router-SSID eintragen
WIFI_PASSWORD = ""

# --- InfluxDB ---
INFLUX_URL   = "http://waerme-pi:8086" 
INFLUX_TOKEN  = ""
INFLUX_ORG    = "homelab"
INFLUX_BUCKET = "innentemperatur"          

# --- Sensor ---
DS18B20_PIN   = 4        # GPIO4 = D2 am WeMos D1 Mini
LOCATION      = "..."   # Standort des Pi
ROOM          = "Bad"          # Raumname (Wohnzimmer, Badezimmer, ...)
MEASUREMENT   = "raumtemperatur"   # Measurement-Name (bleibt generisch für spätere Feuchtigkeitssensoren etc.)

# --- Intervall ---
INTERVAL_SEC  = 60       # Wie oft messen? (in Sekunden)
