# mobilfunk-collector

Liest das verbleibende Datenvolumen der Drillisch-Verträge (winSIM, sim24) aus und
schreibt es nach InfluxDB. Grafana-Dashboard liegt bei.

Läuft auf dem **waerme-pi** im Heimnetz — nicht auf dem VPS.
Begründung siehe Abschnitt Sicherheit.

Ausführliche Anleitung: Report `2026-08-01-15-17_mobilfunk-datenvolumen-monitor_v1.html`
in den Reports.

---

## Dateien

| Datei | Zweck |
|---|---|
| `mobilfunk_influx.py` | Scraper: Login → Verbrauchsseite → InfluxDB |
| `upstream_watch.py` | Prüft GitHub-Repos auf Fixes/Issues, meldet per Telegram |
| `mobilfunk-dashboard.json` | Grafana-Dashboard (UID `mobilfunk-datenvolumen`) |
| `env.example` | Muster für die `.env`-Ergänzung |

---

## Schnellstart

```bash
pip3 install requests python-dotenv influxdb-client
# .env um die MOBILFUNK_*-Blöcke ergänzen (siehe env.example)

python3 mobilfunk_influx.py --dry-run     # testen, nichts schreiben
python3 upstream_watch.py --baseline      # aktuellen Upstream-Stand merken
```

Cron:

```
# Alle 2 Stunden zur Minute 17: Datenvolumen abfragen
17 */2 * * * /usr/bin/python3 /home/admin/mobilfunk_influx.py >> /home/admin/mobilfunk.log 2>&1

# Täglich 07:40: GitHub-Upstream auf Fixes prüfen
40 7 * * * /usr/bin/python3 /home/admin/upstream_watch.py >> /home/admin/mobilfunk_upstream.log 2>&1
```

---

## InfluxDB-Schema

Bucket `mobilfunk`, Measurement `datenvolumen`, Retention 365 d.

| | Name | Bemerkung |
|---|---|---|
| Tag | `person` | `person1`, `person2`, `person3`, `person4` |
| Tag | `provider` | `winsim.de`, `sim24.de` |
| Field | `used_gb` | verbraucht im Abrechnungsmonat |
| Field | `total_gb` | Inklusivvolumen |
| Field | `remaining_gb` | `total_gb - used_gb`, nie negativ |
| Field | `used_pct` | 0–100 |
| Field | `scrape_ok` | 1 = ok, 0 = Login oder Parsing fehlgeschlagen |

`scrape_ok` wird auch bei Fehlern geschrieben — dadurch ist eine Lücke in der
Zeitreihe von einem kaputten Parser unterscheidbar.

---

## Wenn es kaputt geht

Es *wird* kaputtgehen: das ist HTML-Parsing gegen eine fremde Seite. Reihenfolge:

1. `python3 upstream_watch.py --status` — hat der Upstream schon einen Fix?
2. Falls ja: neue Marker aus `src/PremiumSim.js` in `MARKERS_USED` / `MARKERS_TOTAL`
   in `mobilfunk_influx.py` übertragen. Die Marker-Liste ist bewusst genauso
   aufgebaut wie `getSubstring()` im Upstream-Script — 1:1 übertragbar.
3. Falls nein: `python3 mobilfunk_influx.py --only person1 --dry-run --dump-html /tmp/`
   und im HTML nachsehen, wie die Klassen jetzt heißen.

Upstream: <https://github.com/BergenSoft/scriptable_premiumsim>

---

## Dashboard-Notiz: Seriennamen

Grafana benennt Flux-Serien standardmäßig nach dem Wertespaltennamen plus Labels —
daher stand in v1 überall `_value person1`. Behoben in v2 des Dashboards:

- die Queries lassen `person` als **Label** stehen (kein `rename` auf `_field` mehr)
- `fieldConfig.defaults.displayName` ist auf `${__field.labels.person}` gesetzt
- `strings.title()` in der Query macht daraus `Person1` statt `person1`,
  ohne dass die Tags in InfluxDB angefasst werden

Die Soll-Linie in Panel 4 bekommt ebenfalls ein `person`-Label, sonst wäre ihr
Name leer. Ihr Style-Override matcht über `byFrameRefID: "B"` und nicht über den
Namen — das bleibt stabil, wenn der Anzeigename sich mal ändert.

---

## Sicherheit

Die `.env` enthält vollwertige Zugangsdaten zu drei Mobilfunkkonten. Damit lassen
sich kostenpflichtige Tarifoptionen buchen. Deshalb:

- Script läuft auf dem Pi im Heimnetz, **nicht** auf dem öffentlich erreichbaren VPS
- `.env` mit `chmod 600`, Eigentümer `admin`
- InfluxDB-Zugriff nur mit dem bestehenden Write-Only-Token
- keine Zugangsdaten im Git-Repo, `.env` steht in `.gitignore`
- keine Schreib-Operationen im Portal — das Script bucht nichts, es liest nur
