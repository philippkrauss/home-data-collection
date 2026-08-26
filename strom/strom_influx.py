#!/usr/bin/env python3
"""
Stromzähler (Easymeter / DZG) -> InfluxDB Collector
====================================================
Liest den Stromzähler per IR/SML aus und schreibt die Werte nach InfluxDB.

Standalone-Version: der SML-Parser (vormals strom_lesen.py) ist komplett
eingebettet, kein zweites Modul mehr nötig. Nur pyserial + influxdb-client +
python-dotenv als Abhängigkeiten.

  pip install pyserial influxdb-client python-dotenv --break-system-packages

Aufruf:
  python3 strom_influx.py                # normaler Lauf, schreibt nach InfluxDB
  python3 strom_influx.py --dry-run       # liest, schreibt aber nicht
  python3 strom_influx.py --debug         # zeigt zusätzlich alle gefundenen OBIS-Werte

Bekannte Felder, die geschrieben werden (sofern im SML-Datagramm vorhanden):
  energie_kwh              1.8.0  Wirkenergie Bezug gesamt      (Pflichtfeld)
  einspeisung_kwh          2.8.0  Wirkenergie Einspeisung gesamt
  bezug_t1_kwh             1.8.1  Wirkenergie Bezug Tarif 1
  bezug_t2_kwh             1.8.2  Wirkenergie Bezug Tarif 2
  leistung_w              16.7.0  Momentane Wirkleistung (gesamt/saldiert)
  leistung_bezug_w         1.7.0  Momentane Wirkleistung Bezug
  leistung_einspeisung_w   2.7.0  Momentane Wirkleistung Einspeisung

Welche dieser Felder tatsächlich ankommen, hängt vom Push-Datensatz ab, den
der Zähler über die optische Schnittstelle sendet (Kurz- vs. erweiterter
Datensatz, am Zähler selbst per PIN + Bedientaste umschaltbar). Mit
--debug werden alle im Datagramm gefundenen OBIS-Codes ausgegeben, auch
solche, die (noch) nicht auf ein InfluxDB-Feld gemappt sind.
"""

import argparse
import logging
import os
import time
from datetime import datetime, timezone

import serial
from dotenv import load_dotenv
from influxdb_client import Point, WritePrecision

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Breaking change in influxdb-client >= 1.40: SECONDS -> S
_WP = getattr(WritePrecision, "S", None) or getattr(WritePrecision, "SECONDS")

# --- Config (aus .env) ---
SERIAL_PORT   = os.getenv("STROM_PORT", "/dev/ttyUSB0")
BAUDRATE      = int(os.getenv("STROM_BAUD", "9600"))
SML_TIMEOUT   = int(os.getenv("STROM_TIMEOUT", "15"))

INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN")
INFLUX_ORG    = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET_STROM", "strom")

# ---------------------------------------------------------------------------
# SML-Transportschicht / OBIS-Zuordnung
# ---------------------------------------------------------------------------
SML_START = bytes([0x1B, 0x1B, 0x1B, 0x1B, 0x01, 0x01, 0x01, 0x01])
SML_END   = bytes([0x1B, 0x1B, 0x1B, 0x1B, 0x1A])  # gefolgt von 3 Bytes (Padding+CRC)

# OBIS-Code -> (Klartextname, Roheinheit) - nur fuer --debug-Anzeige
OBIS_MAP = {
    bytes([0x01, 0x00, 0x01, 0x08, 0x00, 0xFF]): ("Wirkenergie Bezug gesamt",       "Wh"),
    bytes([0x01, 0x00, 0x02, 0x08, 0x00, 0xFF]): ("Wirkenergie Einspeisung gesamt", "Wh"),
    bytes([0x01, 0x00, 0x01, 0x08, 0x01, 0xFF]): ("Wirkenergie Bezug T1",           "Wh"),
    bytes([0x01, 0x00, 0x01, 0x08, 0x02, 0xFF]): ("Wirkenergie Bezug T2",           "Wh"),
    bytes([0x01, 0x00, 0x10, 0x07, 0x00, 0xFF]): ("Wirkleistung aktuell",           "W"),
    bytes([0x01, 0x00, 0x01, 0x07, 0x00, 0xFF]): ("Wirkleistung Bezug",             "W"),
    bytes([0x01, 0x00, 0x02, 0x07, 0x00, 0xFF]): ("Wirkleistung Einspeisung",       "W"),
    bytes([0x01, 0x00, 0x60, 0x01, 0x00, 0xFF]): ("Zaehlernummer",                  ""),
    bytes([0x81, 0x81, 0xC7, 0x82, 0x03, 0xFF]): ("Hersteller-ID",                  ""),
}

UNIT_MAP = {
    27: "W", 28: "VA", 29: "var", 30: "Wh", 31: "VAh", 32: "varh",
    33: "Hz", 35: "V", 36: "A",
}

# OBIS-Code -> (InfluxDB-Feldname, Divisor). Divisor rechnet Wh -> kWh um,
# bei W-Werten bleibt er 1. energie_kwh (1.8.0) ist Pflicht, alle anderen
# werden nur geschrieben, wenn der Zähler sie tatsaechlich sendet.
_OBIS_ENERGIE = bytes([0x01, 0x00, 0x01, 0x08, 0x00, 0xFF])  # 1.8.0

FIELD_MAP = {
    _OBIS_ENERGIE:                                    ("energie_kwh", 1000.0),
    bytes([0x01, 0x00, 0x02, 0x08, 0x00, 0xFF]):       ("einspeisung_kwh", 1000.0),
    bytes([0x01, 0x00, 0x01, 0x08, 0x01, 0xFF]):       ("bezug_t1_kwh", 1000.0),
    bytes([0x01, 0x00, 0x01, 0x08, 0x02, 0xFF]):       ("bezug_t2_kwh", 1000.0),
    bytes([0x01, 0x00, 0x10, 0x07, 0x00, 0xFF]):       ("leistung_w", 1.0),
    bytes([0x01, 0x00, 0x01, 0x07, 0x00, 0xFF]):       ("leistung_bezug_w", 1.0),
    bytes([0x01, 0x00, 0x02, 0x07, 0x00, 0xFF]):       ("leistung_einspeisung_w", 1.0),
}


# ---------------------------------------------------------------------------
# Minimaler SML-Parser (kein externes SML-Paket noetig)
# ---------------------------------------------------------------------------
class SmlParseError(Exception):
    pass


class SmlParser:
    """Minimaler SML-Parser fuer den Datenteil eines SML-Files
    (also das, was zwischen Start- und Ende-Escape liegt)."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read_byte(self) -> int:
        if self.pos >= len(self.data):
            raise SmlParseError("Unerwartetes Datenende beim Lesen eines Bytes")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise SmlParseError(
                f"Unerwartetes Datenende: {n} Bytes erwartet, {self.remaining()} verfuegbar"
            )
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def peek(self) -> int:
        if self.pos >= len(self.data):
            raise SmlParseError("Unerwartetes Datenende beim Peek")
        return self.data[self.pos]

    def read_tl(self):
        """Gibt (type_nibble, data_length) zurueck.
        type_nibble: 0=OctetString, 4=Bool, 5=Int, 6=Uint, 7=List
        Fuer List: data_length = Anzahl Elemente
        Fuer andere: data_length = Nutzdatenlaenge (ohne TL-Bytes)
        """
        b = self.read_byte()
        type_nibble = (b >> 4) & 0x07
        length_bits = b & 0x0F
        more = bool(b & 0x80)  # Bit 7 gesetzt = erweitertes TL
        while more:
            b2 = self.read_byte()
            more = bool(b2 & 0x80)
            length_bits = (length_bits << 4) | (b2 & 0x0F)
        if type_nibble == 7:  # List
            return type_nibble, length_bits
        data_len = max(0, length_bits - 1)
        return type_nibble, data_len

    def parse_value(self):
        """Parst einen SML-Wert. Gibt (type_name, value) zurueck."""
        if self.remaining() == 0:
            raise SmlParseError("Keine Daten mehr")
        first = self.peek()
        if first == 0x01:  # Null / Optional nicht vorhanden
            self.read_byte()
            return 'null', None
        if first == 0x00:  # Ende-Markierung
            self.read_byte()
            return 'end', None
        type_nibble, data_len = self.read_tl()
        if type_nibble == 7:  # List
            items = [self.parse_value() for _ in range(data_len)]
            return 'list', items
        raw = self.read_bytes(data_len)
        if type_nibble == 0:
            return 'bytes', raw
        elif type_nibble == 5:
            return ('int', None) if data_len == 0 else ('int', int.from_bytes(raw, 'big', signed=True))
        elif type_nibble == 6:
            return ('uint', None) if data_len == 0 else ('uint', int.from_bytes(raw, 'big', signed=False))
        elif type_nibble == 4:
            return 'bool', bool(raw[0]) if raw else False
        else:
            return 'unknown', raw

    def parse_file(self) -> list:
        """Parst alle SML-Nachrichten im File. Gibt eine Liste von Nachrichten zurueck."""
        messages = []
        while self.remaining() > 1:
            if self.peek() == 0x00:
                break
            try:
                t, v = self.parse_value()
                if t == 'list':
                    messages.append(v)
            except SmlParseError:
                break
        return messages


def extract_values(sml_content: bytes) -> dict:
    """Parst den SML-Nutzdatenteil und gibt {obis_bytes: (wert, unit_code)} zurueck."""
    parser = SmlParser(sml_content)
    results = {}
    try:
        messages = parser.parse_file()
    except Exception:
        return results

    for msg in messages:
        if not isinstance(msg, list) or len(msg) < 4:
            continue
        body_type, body_val = msg[3]
        if body_type != 'list' or not body_val or len(body_val) < 2:
            continue
        msg_type_t, msg_type_v = body_val[0]
        if msg_type_t != 'uint' or msg_type_v != 0x0701:
            continue  # nur SML_GetList.Response (0x0701) interessiert uns
        resp_type, resp_val = body_val[1]
        if resp_type != 'list' or not resp_val or len(resp_val) < 5:
            continue
        _, val_list = resp_val[4]  # valList
        if not isinstance(val_list, list):
            continue
        for entry_type, entry_val in val_list:
            if entry_type != 'list' or not entry_val or len(entry_val) < 6:
                continue
            # ListEntry: [objName, status, valTime, unit, scaler, value, valueSignature]
            _, obis = entry_val[0]
            _, unit = entry_val[3]
            _, scaler = entry_val[4]
            _, value = entry_val[5]
            if not isinstance(obis, bytes) or len(obis) != 6 or value is None:
                continue
            if isinstance(scaler, int) and isinstance(value, (int, float)):
                actual = value * (10 ** scaler)
            else:
                actual = value
            results[obis] = (actual, unit)
    return results


def read_sml_datagram(ser: serial.Serial, timeout: float = SML_TIMEOUT):
    """Liest so lange, bis ein vollstaendiges SML-Datagramm gefunden wurde.
    Gibt den Nutzdatenteil zurueck (zwischen Start- und Ende-Escape)."""
    buf = b''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(512)
        if chunk:
            buf += chunk
        start_idx = buf.find(SML_START)
        if start_idx == -1:
            if len(buf) > 16:
                buf = buf[-(len(SML_START) - 1):]
            continue
        end_idx = buf.find(SML_END, start_idx + len(SML_START))
        if end_idx == -1:
            continue  # noch nicht komplett
        end_total = end_idx + len(SML_END) + 3  # + Padding-Byte + 2 CRC-Bytes
        if len(buf) < end_total:
            continue
        content = buf[start_idx + len(SML_START) : end_idx]
        buf = buf[end_total:]
        return content
    return None


def format_obis(obis: bytes) -> str:
    if len(obis) != 6:
        return obis.hex(':')
    a, b, c, d, e, f = obis
    return f"{a}-{b}:{c}.{d}.{e}*{f}"


def debug_print_values(values: dict) -> None:
    """Zeigt alle im Datagramm gefundenen OBIS-Werte, auch nicht gemappte."""
    if not values:
        print("  (keine Werte dekodiert)")
        return
    for obis, (value, unit_code) in sorted(values.items()):
        name, _ = OBIS_MAP.get(obis, (format_obis(obis), ""))
        unit_str = UNIT_MAP.get(unit_code, f"Unit#{unit_code}" if unit_code else "")
        mapped = " [-> influx]" if obis in FIELD_MAP else ""
        if unit_str == "Wh" and isinstance(value, float):
            print(f"  {name:<40} {value/1000:>10.3f} kWh  ({value:.1f} Wh){mapped}")
        elif unit_str in ("W", "VA", "var") and isinstance(value, float):
            print(f"  {name:<40} {value:>10.1f} {unit_str}{mapped}")
        elif obis == bytes([0x01, 0x00, 0x60, 0x01, 0x00, 0xFF]) and isinstance(value, bytes):
            print(f"  {name:<40} {value.hex(' ').upper()}")
        else:
            print(f"  {name:<40} {value} {unit_str}{mapped}")


# ---------------------------------------------------------------------------
# Zaehler auslesen -> InfluxDB Point
# ---------------------------------------------------------------------------
def read_meter(debug: bool = False) -> dict:
    """Liest ein SML-Datagramm und gibt {feldname: wert} fuer alle bekannten,
    im Datagramm vorhandenen Felder zurueck (energie_kwh ist Pflicht)."""
    log.info("Oeffne seriellen Port %s ...", SERIAL_PORT)
    ser = serial.Serial(SERIAL_PORT, baudrate=BAUDRATE, timeout=1)
    ser.setDTR(True)
    ser.setRTS(True)

    try:
        content = read_sml_datagram(ser, timeout=SML_TIMEOUT)
    finally:
        ser.close()

    if content is None:
        raise TimeoutError(
            f"Kein vollstaendiges SML-Datagramm empfangen nach {SML_TIMEOUT}s (Port: {SERIAL_PORT})"
        )

    values = extract_values(content)

    if debug:
        print("\n--- Alle gefundenen OBIS-Werte ---")
        debug_print_values(values)
        print("----------------------------------\n")

    if _OBIS_ENERGIE not in values:
        raise ValueError(
            f"OBIS Wirkenergie Bezug gesamt (01 00 01 08 00 FF) nicht im Datagramm. "
            f"Gefundene Codes: {[b.hex() for b in values]}"
        )

    fields = {}
    for obis, (field_name, divisor) in FIELD_MAP.items():
        if obis in values:
            raw_value, _unit = values[obis]
            fields[field_name] = float(raw_value) / divisor
            log.info("%s = %.3f", field_name, fields[field_name])

    return fields


def build_point(fields: dict) -> Point:
    """Baut einen InfluxDB Point mit allen vorhandenen Feldern."""
    point = Point("easymeter").time(datetime.now(timezone.utc), _WP)
    for name, value in fields.items():
        point = point.field(name, value)
    return point


def print_point(p: Point) -> None:
    """Pretty-print eines Points fuer den Dry-Run-Modus."""
    line = p.to_line_protocol()
    parts = line.split(" ")
    print("\n" + "=" * 60)
    print("  DRY RUN — easymeter point (nicht nach InfluxDB geschrieben)")
    print("=" * 60)
    print(f"  Measurement : {parts[0]}")
    if len(parts) >= 2:
        for field in parts[1].split(","):
            print(f"  Field       : {field}")
    print("=" * 60 + "\n")


def write_to_influx(point: Point) -> None:
    """Schreibt einen Point nach InfluxDB."""
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        log.info("Nach InfluxDB Bucket '%s' geschrieben.", INFLUX_BUCKET)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stromzaehler -> InfluxDB Collector (standalone)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zaehler auslesen, aber statt InfluxDB nur auf der Konsole ausgeben",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Zusaetzlich alle im SML-Datagramm gefundenen OBIS-Werte anzeigen (auch nicht gemappte)",
    )
    args = parser.parse_args()

    try:
        fields = read_meter(debug=args.debug)
    except Exception as e:
        log.error("Fehler beim Auslesen des Zaehlers: %s", e)
        raise SystemExit(1)

    point = build_point(fields)

    if args.dry_run:
        print_point(point)
    else:
        write_to_influx(point)


if __name__ == "__main__":
    main()
