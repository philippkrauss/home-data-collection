#!/usr/bin/python
from __future__ import print_function
import serial, time

ser = serial.Serial("/dev/ttyUSB0", baudrate=300, bytesize=7, parity="E", stopbits=1, timeout=2, xonxoff=0, rtscts=0)

#send init message
ser.write("\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
ser.write("\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")

#send request message
ser.write("/?!\x0D\x0A")
ser.flush();
time.sleep(.5)

#send read identification message
print(ser.readline())

#change baudrate
ser.baudrate=2400

try:
    #read data message
    while True:
        response = ser.readline()
        print(response, end="")
        if "!" in response:
            break
finally:
    ser.close()