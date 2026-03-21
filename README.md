# waerme-pi – Dokumentation

Raspberry Pi zur Auslesung des Landis+Gyr UH50 Wärmemengenzählers, Erfassung von Fritz!Box-, Wetter- und Steckdosendaten, System-Monitoring, Speicherung in InfluxDB und Visualisierung in Grafana.

---

## Übersicht

| Komponente | Zweck |
|---|---|
| InfluxDB 2.8.0 | Zeitreihendatenbank für alle Messwerte |
| Grafana | Dashboard und Visualisierung |
| Telegraf | System-Monitoring (CPU, Temperatur, Disk) |
| uh50_influx.py | Wärmezähler auslesen und Daten schreiben |
| fritzbox_collector.py | Fritz!Box Internet- und WLAN-Daten erfassen |
| weather_collector.py | Wetterdaten von Open-Meteo erfassen |
| wlan-power-collector.py | Tuya Smart Plug Stromverbrauch erfassen |
| backup_influx.sh | Wöchentliches Backup auf NAS |

---

## InfluxDB Buckets

| Bucket | Inhalt | Retention |
|---|---|---|
| `waerme` | UH50-Messwerte, Wetterdaten (stündlich) | Forever |
| `fritzbox` | Fritz!Box Internet- und WLAN-Daten | — |
| `system` | System-Metriken via Telegraf | — |
| `wetter-details` | Wetterdaten hochauflösend (5-minütlich) | kurz |
| `tuya-strom` | Smart Plug Stromverbrauch | — |

---

## Scripts

### `/home/admin/uh50_influx.py`
Liest den UH50 Wärmemengenzähler über `/dev/ttyUSB0` aus und schreibt folgende Werte in InfluxDB (Measurement: `uh50`, Bucket: `waerme`):

| Feld | Einheit | Beschreibung |
|---|---|---|
| energie_kwh | kWh | Wärmeenergie gesamt (Zählerstand) |
| volumen_m3 | m³ | Volumen gesamt (Zählerstand) |
| leistung_kw | kW | Aktuelle Wärmeleistung |
| vorlauf_c | °C | Vorlauftemperatur |
| ruecklauf_c | °C | Rücklauftemperatur |
| durchfluss_m3ph | m³/h | Aktueller Durchfluss |
| betriebsstunden_h | h | Betriebsstunden gesamt |
| fehlerzeit_h | h | Fehlerstunden gesamt |

### `/home/admin/fritzbox_collector.py`
Fragt die Fritz!Box per TR-064 API ab und schreibt in InfluxDB (Bucket: `fritzbox`).
Authentifizierung über Benutzer `fritz2060` mit Fritz!Box-Kennwort.

| Measurement | Felder |
|---|---|
| `internet` | downstream_bps, upstream_bps, total_bytes_down, total_bytes_up, connection_uptime_s, is_connected |
| `wifi` | channel, active_hosts (Tags: ssid, standard) |

### `/home/admin/weather_collector.py`
Holt aktuelle Wetterdaten von Open-Meteo (kostenlos, kein API-Key) für Dettenhausen (48.6075, 9.1006).

Unterstützt zwei Modi:
- **Standard** (`--`): schreibt in `INFLUX_BUCKET_WEATHER` (stündlich, lange Retention)
- **Detailed** (`--detailed`): schreibt in `INFLUX_BUCKET_WEATHER_DETAILED` (5-minütlich, kurze Retention)

| Feld | Einheit | Beschreibung |
|---|---|---|
| temperature_c | °C | Lufttemperatur 2m |
| apparent_temperature_c | °C | Gefühlte Temperatur |
| humidity_pct | % | Relative Luftfeuchtigkeit |
| dew_point_c | °C | Taupunkt |
| precipitation_mm | mm | Niederschlag letzte Stunde |
| rain_mm | mm | Regen letzte Stunde |
| snowfall_cm | cm | Schneefall letzte Stunde |
| pressure_hpa | hPa | Luftdruck |
| wind_speed_kmh | km/h | Windgeschwindigkeit |
| wind_gusts_kmh | km/h | Windböen |
| wind_direction_deg | ° | Windrichtung |
| cloud_cover_pct | % | Wolkendecke |
| sunshine_duration_s | s | Sonnenscheindauer letzte Stunde |
| uv_index | — | UV-Index |
| weather_code | — | WMO Wettercode |
| is_day | 0/1 | Tag (1) oder Nacht (0) |

### `/home/admin/wlan-power-collector.py`
Liest Stromverbrauchsdaten von Antela Smart Plug (Kaffeemaschine) über lokales Tuya-Protokoll aus (kein Cloud-Zugriff nötig). Schreibt in InfluxDB (Measurement: `smart_plug`, Bucket: `tuya-strom`).

| Feld | Einheit | Beschreibung |
|---|---|---|
| switch | 0/1 | Schaltzustand |
| power_w | W | Aktuelle Leistung |
| voltage_v | V | Spannung |
| current_ma | mA | Stromstärke |
| energy_kwh | kWh | Gesamtverbrauch seit Inbetriebnahme |

#### Tuya-Gerätedaten regenerieren
Die Dateien `devices.json`, `snapshot.json`, `tinytuya.json` und `tuya-raw.json` werden **nicht** im Repo gespeichert. Sie können jederzeit neu generiert werden:

```bash
pip install tinytuya
python -m tinytuya wizard
```

Der Wizard benötigt:
- **API Key** und **API Secret** aus https://iot.tuya.com → Projekt `home-data` → Overview
- **Region:** `eu`
- Smart Life App mit dem Projekt verknüpft (QR-Code unter Devices → Link App Account → Tuya App Account Authorization)

Danach liegen alle Device IDs, Local Keys und IP-Adressen wieder lokal vor.

### `/home/admin/backup_influx.sh`
Erstellt ein natives InfluxDB-Backup aller Buckets, packt es als `.tar.gz` und lädt es auf das NAS (fritz.nas) hoch. Lokale Backups älter als 4 Wochen werden automatisch gelöscht.
FTP-Zugangsdaten liegen in `/home/admin/.ftp_credentials` (chmod 600).

---

## Konfiguration

### `/home/admin/.env`
Zentrale Konfigurationsdatei für alle Python-Scripts:

```
# Fritz!Box
FRITZ_ADDRESS = 192.168.178.1
FRITZ_USER    = fritz2060
FRITZ_PASSWORD = ...

# InfluxDB
INFLUX_URL   = http://localhost:8086
INFLUX_TOKEN = ...
INFLUX_ORG   = home

# Buckets
INFLUX_BUCKET_FRITZBOX          = fritzbox
INFLUX_BUCKET_WAERME            = waerme
INFLUX_BUCKET_WEATHER           = waerme
INFLUX_BUCKET_WEATHER_DETAILED  = wetter-details
INFLUX_BUCKET_TUYA              = tuya-strom

# Wetter
WEATHER_LAT      = 48.6075
WEATHER_LON      = 9.1006
WEATHER_LOCATION = Dettenhausen

# Tuya Smart Plug - Kaffeemaschine
TUYA_STECKDOSE3_ID   = ...
TUYA_STECKDOSE3_IP   = 192.168.178.170
TUYA_STECKDOSE3_KEY  = ...
TUYA_STECKDOSE3_NAME = Kaffeemaschine
TUYA_STECKDOSE3_VER  = 3.4
```

### `/etc/telegraf/telegraf.conf`
Telegraf-Konfiguration für System-Monitoring. Schreibt in Bucket `system`.
Erfasst: CPU-Auslastung, CPU-Temperatur (`/sys/class/thermal/thermal_zone0/temp`), Disk-Auslastung (`/`).

---

## Wichtige Dateien und Verzeichnisse

| Pfad | Beschreibung |
|---|---|
| `/home/admin/uh50_influx.py` | UH50 Zählerauslesung |
| `/home/admin/fritzbox_collector.py` | Fritz!Box Datenerfassung |
| `/home/admin/weather_collector.py` | Wetterdaten Open-Meteo |
| `/home/admin/wlan-power-collector.py` | Tuya Smart Plug Stromverbrauch |
| `/home/admin/backup_influx.sh` | Backup-Script |
| `/home/admin/.env` | Zentrale Konfiguration (alle Scripts) |
| `/home/admin/.ftp_credentials` | FTP-Zugangsdaten für NAS (chmod 600) |
| `/home/admin/influx_backups/` | Lokale Backup-Ablage |
| `/home/admin/uh50.log` | Log UH50 |
| `/home/admin/fritzbox_collector.log` | Log Fritz!Box |
| `/home/admin/weather_collector.log` | Log Wetter |
| `/home/admin/wlan_power_collector.log` | Log Smart Plug |
| `/home/admin/backup.log` | Log Backups |
| `/etc/telegraf/telegraf.conf` | Telegraf Konfiguration |
| `/etc/ssh/sshd_config` | SSH-Konfiguration (PasswordAuthentication no) |
| `/etc/apt/sources.list.d/influxdata.list` | InfluxDB APT Repository |

---

## Cronjobs

Anzeigen mit `crontab -l`:

```
# Stündlich: UH50 Zähler auslesen
0 * * * * /usr/bin/python3 /home/admin/uh50_influx.py >> /home/admin/uh50.log 2>&1

# Alle 5 Minuten: Fritz!Box Daten erfassen
*/5 * * * * /usr/bin/python3 /home/admin/fritzbox_collector.py >> /home/admin/fritzbox_collector.log 2>&1

# Stündlich: Wetterdaten erfassen (lange Retention)
0 * * * * /usr/bin/python3 /home/admin/weather_collector.py >> /home/admin/weather_collector.log 2>&1

# Alle 5 Minuten: Wetterdaten hochauflösend (kurze Retention)
*/5 * * * * /usr/bin/python3 /home/admin/weather_collector.py --detailed >> /home/admin/weather_collector.log 2>&1

# Alle 5 Minuten: Smart Plug Kaffeemaschine
*/5 * * * * /usr/bin/python3 /home/admin/wlan-power-collector.py >> /home/admin/wlan_power_collector.log 2>&1

# Wöchentlich Sonntag 02:00 Uhr: Backup auf NAS
0 2 * * 0 /home/admin/backup_influx.sh >> /home/admin/backup.log 2>&1
```

---

## Dienste

```bash
# Status prüfen
sudo systemctl status influxdb
sudo systemctl status grafana-server
sudo systemctl status telegraf

# Neu starten
sudo systemctl restart influxdb
sudo systemctl restart grafana-server
sudo systemctl restart telegraf
```

---

## Web-Interfaces

| Dienst | URL |
|---|---|
| InfluxDB | http://waerme-pi:8086 |
| Grafana | http://waerme-pi:3000 |

---

## Grafana Dashboards

| Dashboard | UID | Beschreibung |
|---|---|---|
| Wärme (UH50) | `waerme-uh50` | UH50-Messwerte, Temperaturen, Durchfluss |
| Wärmeverbrauch | `waerme-verbrauch` | Täglicher/wöchentlicher/monatlicher Verbrauch |
| Fritz!Box | `fritzbox-monitor` | Internet-Throughput, WLAN, Verbindungsstatus |
| Fritz!Box Datenvolumen | `fritzbox-traffic` | Übertragene Daten pro Stunde/Tag/Woche/Monat |
| Wetter Dettenhausen | `wetter-dettenhausen` | Temperatur, Wind, Niederschlag, Luftdruck |
| waerme-pi System | `waerme-pi-system` | CPU, Temperatur, Disk via Telegraf |
| Kaffeemaschine | `tuya-kaffeemaschine` | Leistung, Spannung, Stromstärke, Verbrauch |

---

## Backup wiederherstellen

> **Wichtig:** Bei Migration auf neues System InfluxDB **v2.8.0** installieren!

### Schritt 1: Backup vom NAS holen
```bash
curl --netrc-file /home/admin/.ftp_credentials \
     "ftp://fritz.nas/backups/waerme-pi/influx_backup_DATUM.tar.gz" \
     -o /home/admin/restore.tar.gz
```

### Schritt 2: Entpacken
```bash
tar -xzf /home/admin/restore.tar.gz -C /home/admin/
```

### Schritt 3: InfluxDB stoppen
```bash
sudo systemctl stop influxdb
```

### Schritt 4: Wiederherstellen
```bash
influx restore /home/admin/influx_backup_DATUM --full --token "DEIN_TOKEN"
```

### Schritt 5: InfluxDB starten
```bash
sudo systemctl start influxdb
```

### Schritt 6: Nach dem Restore
- Neuen Token in `/home/admin/.env` eintragen
- `telegraf.conf` updaten und Telegraf neu starten
- Grafana Data Source prüfen (URL + Token)
- Tuya-Gerätedaten neu generieren (siehe oben)
- Cronjobs testen

> **Hinweis:** Bei einem frisch aufgesetzten System zuerst InfluxDB installieren und den Setup-Wizard durchlaufen, erst dann `influx restore` ausführen.

---

## Verfügbare Backups auf NAS anzeigen

```bash
curl --netrc-file /home/admin/.ftp_credentials "ftp://fritz.nas/backups/waerme-pi/"
```

---

## Hardware

- Raspberry Pi (aarch64, Debian Trixie)
- Landis+Gyr UH50 Wärmemengenzähler
- USB-zu-Seriell Adapter auf `/dev/ttyUSB0`
- Kommunikation über IEC 62056-21 Protokoll (300 Baud Wake-up, 2400 Baud Daten)
- 4x Antela Smart Plug EU (Modell F1s202-EU-DF), davon 1x aktiv erfasst (Kaffeemaschine)

---

## Übersicht

| Komponente | Zweck |
|---|---|
| InfluxDB 2.8.0 | Zeitreihendatenbank für alle Messwerte |
| Grafana | Dashboard und Visualisierung |
| Telegraf | System-Monitoring (CPU, Temperatur, Disk) |
| uh50_influx.py | Wärmezähler auslesen und Daten schreiben |
| fritzbox_collector.py | Fritz!Box Internet- und WLAN-Daten erfassen |
| weather_collector.py | Wetterdaten von Open-Meteo erfassen |
| backup_influx.sh | Wöchentliches Backup auf NAS |

---

## InfluxDB Buckets

| Bucket | Inhalt | Retention |
|---|---|---|
| `waerme` | UH50-Messwerte, Wetterdaten | Forever |
| `fritzbox` | Fritz!Box Internet- und WLAN-Daten | — |
| `system` | System-Metriken via Telegraf | — |

---

## Scripts

### `/home/admin/uh50_influx.py`
Liest den UH50 Wärmemengenzähler über `/dev/ttyUSB0` aus und schreibt folgende Werte in InfluxDB (Measurement: `uh50`, Bucket: `waerme`):

| Feld | Einheit | Beschreibung |
|---|---|---|
| energie_kwh | kWh | Wärmeenergie gesamt (Zählerstand) |
| volumen_m3 | m³ | Volumen gesamt (Zählerstand) |
| leistung_kw | kW | Aktuelle Wärmeleistung |
| vorlauf_c | °C | Vorlauftemperatur |
| ruecklauf_c | °C | Rücklauftemperatur |
| durchfluss_m3ph | m³/h | Aktueller Durchfluss |
| betriebsstunden_h | h | Betriebsstunden gesamt |
| fehlerzeit_h | h | Fehlerstunden gesamt |

### `/home/admin/fritzbox_collector.py`
Fragt die Fritz!Box per TR-064 API ab und schreibt in InfluxDB (Bucket: `fritzbox`).
Authentifizierung über Benutzer `fritz2060` mit Fritz!Box-Kennwort.

| Measurement | Felder |
|---|---|
| `internet` | downstream_bps, upstream_bps, total_bytes_down, total_bytes_up, connection_uptime_s, is_connected |
| `wifi` | channel, active_hosts (Tags: ssid, standard) |

### `/home/admin/weather_collector.py`
Holt aktuelle Wetterdaten von Open-Meteo (kostenlos, kein API-Key) für Dettenhausen (48.6075, 9.1006) und schreibt in InfluxDB (Measurement: `weather`, Bucket: `waerme`).

| Feld | Einheit | Beschreibung |
|---|---|---|
| temperature_c | °C | Lufttemperatur 2m |
| apparent_temperature_c | °C | Gefühlte Temperatur |
| humidity_pct | % | Relative Luftfeuchtigkeit |
| dew_point_c | °C | Taupunkt |
| precipitation_mm | mm | Niederschlag letzte Stunde |
| rain_mm | mm | Regen letzte Stunde |
| snowfall_cm | cm | Schneefall letzte Stunde |
| pressure_hpa | hPa | Luftdruck |
| wind_speed_kmh | km/h | Windgeschwindigkeit |
| wind_gusts_kmh | km/h | Windböen |
| wind_direction_deg | ° | Windrichtung |
| cloud_cover_pct | % | Wolkendecke |
| sunshine_duration_s | s | Sonnenscheindauer letzte Stunde |
| uv_index | — | UV-Index |
| weather_code | — | WMO Wettercode |
| is_day | 0/1 | Tag (1) oder Nacht (0) |

### `/home/admin/backup_influx.sh`
Erstellt ein natives InfluxDB-Backup aller Buckets, packt es als `.tar.gz` und lädt es auf das NAS (fritz.nas) hoch. Lokale Backups älter als 4 Wochen werden automatisch gelöscht.
FTP-Zugangsdaten liegen in `/home/admin/.ftp_credentials` (chmod 600).

---

## Konfiguration

### `/home/admin/.env`
Zentrale Konfigurationsdatei für alle Python-Scripts:

```
# Fritz!Box
FRITZ_ADDRESS = 192.168.178.1
FRITZ_USER    = fritz2060
FRITZ_PASSWORD = ...

# InfluxDB
INFLUX_URL   = http://localhost:8086
INFLUX_TOKEN = ...
INFLUX_ORG   = home

# Buckets
INFLUX_BUCKET_FRITZBOX = fritzbox
INFLUX_BUCKET_WAERME   = waerme
INFLUX_BUCKET_WEATHER  = waerme

# Wetter
WEATHER_LAT      = 48.6075
WEATHER_LON      = 9.1006
WEATHER_LOCATION = Dettenhausen
```

### `/etc/telegraf/telegraf.conf`
Telegraf-Konfiguration für System-Monitoring. Schreibt in Bucket `system`.
Erfasst: CPU-Auslastung, CPU-Temperatur (`/sys/class/thermal/thermal_zone0/temp`), Disk-Auslastung (`/`).

---

## Wichtige Dateien und Verzeichnisse

| Pfad | Beschreibung |
|---|---|
| `/home/admin/uh50_influx.py` | UH50 Zählerauslesung |
| `/home/admin/fritzbox_collector.py` | Fritz!Box Datenerfassung |
| `/home/admin/weather_collector.py` | Wetterdaten Open-Meteo |
| `/home/admin/backup_influx.sh` | Backup-Script |
| `/home/admin/.env` | Zentrale Konfiguration (alle Scripts) |
| `/home/admin/.ftp_credentials` | FTP-Zugangsdaten für NAS (chmod 600) |
| `/home/admin/influx_backups/` | Lokale Backup-Ablage |
| `/home/admin/uh50.log` | Log UH50 |
| `/home/admin/fritzbox_collector.log` | Log Fritz!Box |
| `/home/admin/weather_collector.log` | Log Wetter |
| `/home/admin/backup.log` | Log Backups |
| `/etc/telegraf/telegraf.conf` | Telegraf Konfiguration |
| `/etc/ssh/sshd_config` | SSH-Konfiguration (PasswordAuthentication no) |
| `/etc/apt/sources.list.d/influxdata.list` | InfluxDB APT Repository |

---

## Cronjobs

Anzeigen mit `crontab -l`:

```
# Stündlich: UH50 Zähler auslesen
0 * * * * /usr/bin/python3 /home/admin/uh50_influx.py >> /home/admin/uh50.log 2>&1

# Alle 5 Minuten: Fritz!Box Daten erfassen
*/5 * * * * /usr/bin/python3 /home/admin/fritzbox_collector.py >> /home/admin/fritzbox_collector.log 2>&1

# Stündlich: Wetterdaten erfassen
0 * * * * /usr/bin/python3 /home/admin/weather_collector.py >> /home/admin/weather_collector.log 2>&1

# Wöchentlich Sonntag 02:00 Uhr: Backup auf NAS
0 2 * * 0 /home/admin/backup_influx.sh >> /home/admin/backup.log 2>&1
```

---

## Dienste

```bash
# Status prüfen
sudo systemctl status influxdb
sudo systemctl status grafana-server
sudo systemctl status telegraf

# Neu starten
sudo systemctl restart influxdb
sudo systemctl restart grafana-server
sudo systemctl restart telegraf
```

---

## Web-Interfaces

| Dienst | URL |
|---|---|
| InfluxDB | http://waerme-pi:8086 |
| Grafana | http://waerme-pi:3000 |

---

## Grafana Dashboards

| Dashboard | UID | Beschreibung |
|---|---|---|
| Wärme (UH50) | `waerme-uh50` | UH50-Messwerte, Temperaturen, Durchfluss |
| Wärmeverbrauch | `waerme-verbrauch` | Täglicher/wöchentlicher/monatlicher Verbrauch |
| Fritz!Box | `fritzbox-monitor` | Internet-Throughput, WLAN, Verbindungsstatus |
| Fritz!Box Datenvolumen | `fritzbox-traffic` | Übertragene Daten pro Stunde/Tag/Woche/Monat |
| Wetter Dettenhausen | `wetter-dettenhausen` | Temperatur, Wind, Niederschlag, Luftdruck |
| waerme-pi System | `waerme-pi-system` | CPU, Temperatur, Disk via Telegraf |

---

## Backup wiederherstellen

> **Wichtig:** Bei Migration auf neues System InfluxDB **v2.8.0** installieren!

### Schritt 1: Backup vom NAS holen
```bash
curl --netrc-file /home/admin/.ftp_credentials \
     "ftp://fritz.nas/backups/waerme-pi/influx_backup_DATUM.tar.gz" \
     -o /home/admin/restore.tar.gz
```

### Schritt 2: Entpacken
```bash
tar -xzf /home/admin/restore.tar.gz -C /home/admin/
```

### Schritt 3: InfluxDB stoppen
```bash
sudo systemctl stop influxdb
```

### Schritt 4: Wiederherstellen
```bash
influx restore /home/admin/influx_backup_DATUM --full --token "DEIN_TOKEN"
```

### Schritt 5: InfluxDB starten
```bash
sudo systemctl start influxdb
```

### Schritt 6: Nach dem Restore
- Neuen Token in `/home/admin/.env` eintragen
- `telegraf.conf` updaten und Telegraf neu starten
- Grafana Data Source prüfen (URL + Token)
- Cronjobs testen

> **Hinweis:** Bei einem frisch aufgesetzten System zuerst InfluxDB installieren und den Setup-Wizard durchlaufen, erst dann `influx restore` ausführen.

---

## Verfügbare Backups auf NAS anzeigen

```bash
curl --netrc-file /home/admin/.ftp_credentials "ftp://fritz.nas/backups/waerme-pi/"
```

---

## Hardware

- Raspberry Pi (aarch64, Debian Trixie)
- Landis+Gyr UH50 Wärmemengenzähler
- USB-zu-Seriell Adapter auf `/dev/ttyUSB0`
- Kommunikation über IEC 62056-21 Protokoll (300 Baud Wake-up, 2400 Baud Daten)