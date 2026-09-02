# wlanthermo

Pollt die lokale HTTP-API des **WLANThermo Nano V3** (`http://nanov3/data`) und schreibt
Kanal-, Pitmaster- und Systemwerte nach InfluxDB (Bucket `bbq`).

Laeuft auf **waerme-pi** als systemd-Dienst — nicht als Cronjob, weil das Pollintervall
unter einer Minute liegt.

Cloud und eigene Erfassung laufen bewusst **parallel**: der Nano kann beides gleichzeitig,
und die Cloud bleibt der Fernblick unterwegs.

→ Projektkontext: `Misc/BBQ/wlanthermo-inbetriebnahme.md` (Schritt 5)

---

## Anspruch: nichts vorbereiten muessen

Der Collector verlangt vor dem Grillen **keine Handgriffe**. Nano einstecken, grillen,
fertig. Kanaele muessen nicht benannt sein, es muss nicht feststehen, welche Sonde was
misst, und waehrend des Cooks ist nichts anzufassen.

Alles Weitere laesst sich hinterher entscheiden:

- **Welche Sonde war der Garraum?** Steht in den Daten: `bbq_pitmaster.channel` sagt,
  welchen Kanal der Pitmaster geregelt hat.
- **Welche Sonde war das Fleisch?** Ergibt sich aus der Kurve — und laesst sich in Grafana
  per Panel-Override je Kanalnummer benennen.
- **Wie hiess der Cook?** Grafana-Annotation ueber den Zeitbereich, Tag `bbq`.

Deshalb ist der Kanalname ein **Feld** und kein Tag: er muss nicht vorher gesetzt sein, und
ein spaeteres Umbenennen am Geraet spaltet keine Zeitreihe auf. Identitaet ist die
**Kanalnummer**, und die steht ohne Zutun fest.

---

## Dateien

| Datei | Zweck |
|---|---|
| `wlanthermo_influx.py` | der Collector |
| `env.example` | Muster fuer die `.env`-Ergaenzung |
| `wlanthermo-collector.service` | systemd-Unit |

---

## Schnellstart

```bash
pip3 install influxdb-client python-dotenv --break-system-packages

# /home/admin/.env um die Werte aus env.example ergaenzen
# (eigenes INFLUX_TOKEN_BBQ, WLANTHERMO_HOST)

python3 wlanthermo_influx.py --dry-run   # einmal pollen, Line Protocol anzeigen
python3 wlanthermo_influx.py --once      # ein Zyklus, schreibt wirklich
python3 wlanthermo_influx.py --status    # State-Datei anzeigen

sudo cp wlanthermo-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wlanthermo-collector
journalctl -u wlanthermo-collector -f
```

Der Dienst darf dauerhaft laufen. Ist der Nano aus, macht der Collector nichts —
kein Punkt, kein Log, keine Last.

---

## Sessions

Jeder Punkt bekommt den Tag `cook` mit einer Session-ID (`2026-09-06_0512`). Die Regel:

> **War der Nano laenger als `WLANTHERMO_SESSION_GAP_S` (Default 30 min) nicht erreichbar,
> beginnt beim naechsten Kontakt eine neue Session.**

Ein Neustart des Collectors, ein Reboot des Pi oder ein kurzer WLAN-Aussetzer zerreisst
damit nichts — der State liegt in `state.json` und ueberlebt beides.

Anfang, Ende und Dauer eines Cooks stehen implizit in den Daten (erster und letzter Punkt
je `cook`); dafuer braucht es kein eigenes Measurement.

---

## InfluxDB-Schema

Bucket `bbq`, eigenes Write-Only-Token. Datenmenge vernachlaessigbar: ~10 Punkte alle 10 s,
nur solange der Nano laeuft.

| Measurement | Tags | Felder |
|---|---|---|
| `bbq_channel` | `device`, `cook`, `channel` (1–8) | `temp_c`, `name`, `elapsed_s`, `min_c`, `max_c`, `alarm`, `connected`, `fixed`, `typ` |
| `bbq_pitmaster` | `device`, `cook`, `pm_id` | `value_pct` (0–100), `set_c`, `elapsed_s`, `channel`, `pid`, `typ` (Text), `active` |
| `bbq_system` | `device`, `cook` | `soc_pct`, `charge`, `rssi_dbm`, `online`, `unit`, `device_time_offset_s` |

Entscheidungen im Schema:

- **`temp == 999` wird nicht geschrieben.** Das ist der Wert des Nano fuer „kein Fuehler
  gesteckt", und zwar auf allen acht Kanaelen. Wuerde man ihn mitschreiben, haengen in
  Grafana Serien auf 999 und ruinieren jede Autoskalierung.
- **Kanalname als Feld, Kanalnummer als Tag** — siehe oben.
- **Der Pitmaster-Modus ist ein Feld, kein Tag.** Er wechselt waehrend eines Cooks
  (`off` → `auto`); als Tag wuerde er die Kurve des Stellwerts in zwei Serien aufspalten.
- **`connected` und `fixed` werden mitgeschrieben**, obwohl unklar ist, was sie bedeuten:
  im Leerlauf steht `connected: false`, obwohl ein Fuehler steckt und misst. Die Felder
  kosten nichts und beantworten die Frage nach ein paar Cooks von selbst.
- **`elapsed_s` (Sekunden seit Sessionbeginn) wird mitgeschrieben**, obwohl es aus den
  Zeitstempeln ableitbar waere. Damit ist das Grafana-Trend-Panel „Stunden seit Start" ein
  Einzeiler statt eines Joins ueber ein Aggregat je Cook. Der Sessionbeginn ist hier ohnehin
  bekannt — die Information dort berechnen, wo sie vorliegt.
- `device_time_offset_s` ist die Uhrzeit des Nano minus die des Pi. Erklaert spaeter
  Versatz oder Luecken in den Kurven.
- **Die Session-ID steht in Ortszeit**, die Messwerte in UTC. Die ID ist ein Label zum
  Wiederfinden: „0500" muss der Uhrzeit entsprechen, zu der man am Grill stand.

---

## Spool

Schreibfehler landen als Line Protocol in `spool.lp` und werden beim naechsten
erfolgreichen Zyklus nachgeschoben. Kein Luxus: der Anschluss (Deutsche Glasfaser)
faellt gelegentlich fuer Minuten bis Stunden aus, und ein Longjob dauert 12 Stunden.
Gedeckelt auf 10 MB, danach fliegt die aeltere Haelfte raus.

Test wie beim `internet_uptime`-Collector:

```bash
sudo iptables -A OUTPUT -d <VPS-IP> -j DROP
# ein paar Minuten laufen lassen
sudo iptables -D OUTPUT -d <VPS-IP> -j DROP
```

Danach muessen die fehlenden Punkte rueckwirkend mit korrektem Zeitstempel in InfluxDB
auftauchen und `spool.lp` verschwunden sein.

---

## Stolpersteine

| Punkt | Konsequenz |
|---|---|
| `nanov3` per mDNS/DHCP statt fester IP | Namensaussetzer mitten im Longjob → DHCP-Reservierung setzen, IP in die `.env` |
| Nano nach dem Cook tagelang eingesteckt lassen | eine einzige Endlos-Session; nach dem Grillen ausstecken |
| `INFLUX_TOKEN` statt `INFLUX_TOKEN_BBQ` in der `.env` | ueberschreibt das Token der anderen Collectors auf dem Pi |
| Nano im Pitmaster-Betrieb nur auf Akku | Luefter bekommt keinen Strom (Geraeteseite, nicht Collector) |

---

## Dashboards

Zwei Stueck, bewusst getrennt — live und Historie wollen gegensaetzliche Defaults
(Auto-Refresh auf gleitendem Fenster gegen festen Zeitraum) und gegensaetzliche X-Achsen
(Uhrzeit gegen Zeit seit Start).

| Dashboard | UID | JSON |
|---|---|---|
| **BBQ – Aktueller Cook** | `bbq-live-v1` | `dashboards/BBQ - Aktueller Cook.json` |
| **BBQ – Cooks** | `bbq-cooks-v1` | `dashboards/BBQ - Cooks.json` |

**Aktueller Cook** (Refresh 10 s, Fenster 12 h): Statuszeile, Temperaturverlauf mit Sollwert
und Luefter-Stellwert auf der rechten Achse plus Aussentemperatur aus `wetter-details`,
sowie zwei Panels zum **Anstieg in °C/h** (30-Minuten-Glaettung, `derivative`). Unter
2 °C/h rot = Stall. Restzeit ueberschlaegt man aus „Noch bis Ziel" geteilt durch den
Anstieg — bewusst als zwei Zahlen statt als ETA-Panel, weil eine ETA aus der Momentan-
steigung ausgerechnet im Stall gegen unendlich laeuft.

**Cooks** (kein Refresh, Fenster 90 Tage): Tabelle mit einer Zeile je Cook (Start, Dauer,
max °C, Ø Luefter); Klick auf die Cook-ID springt per Data-Link mit dem richtigen Zeitraum
ins Live-Dashboard. Darunter ein **Trend-Panel**, das mehrere Cooks ueber `elapsed_s`
uebereinanderlegt, und eine Liste der per Annotation benannten Cooks.

**Kanalbenennung:** beide Dashboards zeigen `K1` … `K8`. Wer welche Sonde war, traegt man
nachtraeglich per Panel-Override (`byName` auf die Kanalnummer, Property `displayName`)
nach — der Geraetename steht als Feld `name` ohnehin in den Daten.

**Cook benennen:** im Live-Dashboard mit Strg+Ziehen eine Region-Annotation ueber den
Zeitbereich legen, Text rein, Tag `bbq`. Taucht dann in der Liste im Cooks-Dashboard auf.

Import: Dashboards → Import → JSON hochladen → Datasource `DS_INFLUXDB` auswaehlen.
