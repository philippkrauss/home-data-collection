# internet_uptime

Misst die Verfuegbarkeit des Internetanschlusses minuetlich und schreibt sie nach InfluxDB. Ziel ist
zunaechst Erkenntnis (wie oft, wie lange, liegt es an DG oder am Heimnetz),
nicht Alarmierung.

Laeuft auf **waerme-pi** im Heimnetz.

---

## Dateien

| Datei | Zweck |
|---|---|
| `uptime_influx.py` | der Collector |
| `env.example` | Muster fuer die `.env`-Ergaenzung |

---

## Schnellstart

```bash
pip3 install fritzconnection influxdb-client python-dotenv --break-system-packages

# /home/admin/.env um die Werte aus env.example ergaenzen
# (eigenes INFLUX_TOKEN_INTERNET, FRITZ_* falls noch nicht vorhanden)

python3 uptime_influx.py --dry-run     # testen, nichts schreiben, State bleibt unangetastet
python3 uptime_influx.py               # normaler Lauf
```

Cron (siehe Konzept Abschnitt 4 - `flock` verhindert Ueberlappung, falls ein
Lauf haengt):

```
* * * * * /usr/bin/flock -n /tmp/uptime.lock /usr/bin/python3 /home/admin/home-data-collection/internet_uptime/uptime_influx.py >> /home/admin/logs/uptime.log 2>&1
```

---

## Spool-Test (wichtigster Verifikationsschritt)

Simuliert einen VPS-Ausfall, um die Nachschub-Logik zu pruefen:

```bash
sudo iptables -A OUTPUT -d <VPS-IP> -j DROP
# 5 Minuten warten / laufen lassen
sudo iptables -D OUTPUT -d <VPS-IP> -j DROP
```

Danach in InfluxDB pruefen: die fehlenden Minuten muessen rueckwirkend mit
korrektem Timestamp auftauchen, `state.json` zeigt `internet_up: true` und
`/home/admin/internet_uptime_data/spool.lp` existiert nicht mehr.

---

## InfluxDB-Schema

Bucket `internet`, Retention infinite (Datenmenge vernachlaessigbar,
~7 KB/Stunde).

| Measurement | Wann | Tags | Felder |
|---|---|---|---|
| `connectivity` | jeder Lauf, ein Punkt je Ziel | `target` (fritzbox, cloudflare_v4, google_v4, cloudflare_v6, dns) | `up` (0i/1i), `rtt_ms` (nur wenn erreichbar) |
| `wan` | jeder Lauf | - | `is_connected`, `uptime_s`, `box_uptime_s`, `internet_up`, `internet_up_v4`, `internet_up_v6`, `influx_write_ok` |
| `outage` | bei Uebergang down->up, oder rueckwirkend rekonstruiert | - | `duration_s`, `targets_lost` |
| `gap` | wenn seit dem letzten Lauf mehr als `GAP_THRESHOLD_S` (Default 180 s) vergangen sind | - | `duration_s`, `cause` (`power` / `wan` / `collector` / `unknown`) |

Warum `fritzbox` als eigenes Ziel: antwortet die Box, aber die externen Ziele
nicht, liegt es **nicht** am Heimnetz. Warum IPv4/IPv6 getrennt: bei DS-Lite
(bestaetigt, siehe Konzept Abschnitt 3) sagt ein kaputter AFTR "IPv4 tot,
IPv6 lebt" - ohne die Trennung sieht das wie ein Totalausfall aus.

---

## Dashboard

Noch nicht gebaut (Konzept Abschnitt 9 und 10). Titel "Internet -
Verfuegbarkeit", UID `internet-uptime-v1`, JSON gehoert nach Fertigstellung
in `home-data-collection/dashboards/`.

---

