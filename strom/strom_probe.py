#!/usr/bin/env python3
"""
Strom-Probe: minimales Test-Tool zum Live-Beobachten des Stromzaehlers
=======================================================================
Zeigt fortlaufend ALLE OBIS-Codes, die der Zaehler ueber die optische
Schnittstelle sendet -- ideal um beim Umstellen von "Inf on"/"PIN off"
am Zaehler direkt zu sehen, ob und wann neue Werte (z.B. 16.7.0 Momentane
Wirkleistung) dazukommen.

Keine Abhaengigkeit von .env oder InfluxDB, nur pyserial:
  pip install pyserial --break-system-packages

Aufruf:
  python3 strom_probe.py                    # Endlosschleife, Standardport
  python3 strom_probe.py --port /dev/ttyUSB1
  python3 strom_probe.py --once             # nur ein Datagramm lesen und beenden
  python3 strom_probe.py --raw              # zusaetzlich Rohbytes (Hex) zeigen
"""

import argparse
import time
from datetime import datetime

import serial

SML_START = bytes([0x1B, 0x1B, 0x1B, 0x1B, 0x01, 0x01, 0x01, 0x01])
SML_END   = bytes([0x1B, 0x1B, 0x1B, 0x1B, 0x1A])

OBIS_MAP = {
    bytes([0x01, 0x00, 0x01, 0x08, 0x00, 0xFF]): ("Wirkenergie Bezug gesamt",        "Wh"),
    bytes([0x01, 0x00, 0x02, 0x08, 0x00, 0xFF]): ("Wirkenergie Einspeisung gesamt",  "Wh"),
    bytes([0x01, 0x00, 0x01, 0x08, 0x01, 0xFF]): ("Wirkenergie Bezug T1",            "Wh"),
    bytes([0x01, 0x00, 0x01, 0x08, 0x02, 0xFF]): ("Wirkenergie Bezug T2",            "Wh"),
    bytes([0x01, 0x00, 0x10, 0x07, 0x00, 0xFF]): ("Momentane Wirkleistung (gesamt)", "W"),
    bytes([0x01, 0x00, 0x01, 0x07, 0x00, 0xFF]): ("Momentane Wirkleistung Bezug",    "W"),
    bytes([0x01, 0x00, 0x02, 0x07, 0x00, 0xFF]): ("Momentane Wirkleistung Einspeisung", "W"),
    bytes([0x01, 0x00, 0x0F, 0x07, 0x00, 0xFF]): ("Momentane Wirkleistung (Betrag)", "W"),
    bytes([0x01, 0x00, 0x0A, 0x07, 0x00, 0xFF]): ("Momentane Scheinleistung Einspeisung", "VA"),
    bytes([0x01, 0x00, 0x60, 0x01, 0x00, 0xFF]): ("Zaehlernummer", ""),
    bytes([0x01, 0x00, 0x60, 0x32, 0x01, 0xFF]): ("Geraeteeinzelidentifikation", ""),
    bytes([0x81, 0x81, 0xC7, 0x82, 0x03, 0xFF]): ("Hersteller-ID", ""),
}

UNIT_MAP = {
    27: "W", 28: "VA", 29: "var", 30: "Wh", 31: "VAh", 32: "varh",
    33: "Hz", 35: "V", 36: "A",
}


class SmlParseError(Exception):
    pass


class SmlParser:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read_byte(self) -> int:
        if self.pos >= len(self.data):
            raise SmlParseError("Unerwartetes Datenende")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise SmlParseError("Unerwartetes Datenende")
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def peek(self) -> int:
        if self.pos >= len(self.data):
            raise SmlParseError("Unerwartetes Datenende")
        return self.data[self.pos]

    def read_tl(self):
        b = self.read_byte()
        type_nibble = (b >> 4) & 0x07
        length_bits = b & 0x0F
        more = bool(b & 0x80)
        while more:
            b2 = self.read_byte()
            more = bool(b2 & 0x80)
            length_bits = (length_bits << 4) | (b2 & 0x0F)
        if type_nibble == 7:
            return type_nibble, length_bits
        return type_nibble, max(0, length_bits - 1)

    def parse_value(self):
        if self.remaining() == 0:
            raise SmlParseError("Keine Daten mehr")
        first = self.peek()
        if first == 0x01:
            self.read_byte()
            return 'null', None
        if first == 0x00:
            self.read_byte()
            return 'end', None
        type_nibble, data_len = self.read_tl()
        if type_nibble == 7:
            return 'list', [self.parse_value() for _ in range(data_len)]
        raw = self.read_bytes(data_len)
        if type_nibble == 0:
            return 'bytes', raw
        elif type_nibble == 5:
            return ('int', None) if data_len == 0 else ('int', int.from_bytes(raw, 'big', signed=True))
        elif type_nibble == 6:
            return ('uint', None) if data_len == 0 else ('uint', int.from_bytes(raw, 'big', signed=False))
        elif type_nibble == 4:
            return 'bool', bool(raw[0]) if raw else False
        return 'unknown', raw

    def parse_file(self) -> list:
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
            continue
        resp_type, resp_val = body_val[1]
        if resp_type != 'list' or not resp_val or len(resp_val) < 5:
            continue
        _, val_list = resp_val[4]
        if not isinstance(val_list, list):
            continue
        for entry_type, entry_val in val_list:
            if entry_type != 'list' or not entry_val or len(entry_val) < 6:
                continue
            _, obis = entry_val[0]
            _, unit = entry_val[3]
            _, scaler = entry_val[4]
            _, value = entry_val[5]
            if not isinstance(obis, bytes) or len(obis) != 6 or value is None:
                continue
            actual = value * (10 ** scaler) if isinstance(scaler, int) and isinstance(value, (int, float)) else value
            results[obis] = (actual, unit)
    return results


def read_sml_datagram(ser: serial.Serial, timeout: float):
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
            continue
        end_total = end_idx + len(SML_END) + 3
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


def print_values(values: dict) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {len(values)} OBIS-Codes gefunden:")
    if not values:
        print("  (keine Werte dekodiert)")
        return
    for obis, (value, unit_code) in sorted(values.items()):
        name, _ = OBIS_MAP.get(obis, (format_obis(obis) + "  (unbekannt)", ""))
        unit_str = UNIT_MAP.get(unit_code, f"Unit#{unit_code}" if unit_code else "")
        if unit_str == "Wh" and isinstance(value, (int, float)):
            print(f"  {name:<42} {value/1000:>10.3f} kWh")
        elif unit_str in ("W", "VA", "var") and isinstance(value, (int, float)):
            print(f"  {name:<42} {value:>10.2f} {unit_str}")
        elif isinstance(value, bytes):
            print(f"  {name:<42} {value.hex(' ').upper()}")
        else:
            print(f"  {name:<42} {value} {unit_str}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live-Probe fuer Stromzaehler SML-Ausgabe")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="Serieller Port (Standard: /dev/ttyUSB0)")
    ap.add_argument("--baud", default=9600, type=int, help="Baudrate (Standard: 9600)")
    ap.add_argument("--timeout", default=15, type=float, help="Timeout pro Datagramm in Sekunden")
    ap.add_argument("--once", action="store_true", help="Nur ein Datagramm lesen, dann beenden")
    ap.add_argument("--raw", action="store_true", help="Zusaetzlich Rohbytes (Hex, gekuerzt) anzeigen")
    args = ap.parse_args()

    print(f"Oeffne {args.port} @ {args.baud} Baud ... (Strg+C zum Beenden)\n")
    ser = serial.Serial(args.port, args.baud, timeout=1)
    ser.setDTR(True)
    ser.setRTS(True)

    try:
        while True:
            content = read_sml_datagram(ser, timeout=args.timeout)
            if content is None:
                print(f"[{datetime.now():%H:%M:%S}] Timeout - kein vollstaendiges Datagramm empfangen")
            else:
                if args.raw:
                    print(f"  RAW ({len(content)} Bytes): {content.hex(' ').upper()[:150]}...")
                print_values(extract_values(content))
            print()
            if args.once:
                break
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
