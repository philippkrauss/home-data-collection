from fritzconnection.core.fritzconnection import FritzConnection

fc = FritzConnection(
    address="192.168.178.1",
    password="bezug5874"
)
print(fc.call_action("WLANConfiguration:1", "GetInfo"))
