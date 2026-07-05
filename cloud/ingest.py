"""
import os
ingest.py  —  Connects to AWS IoT Core, subscribes to the floc topic,
and stores every incoming message in the database.

Flow:
  AWS IoT Core  →  on_message()  →  store_payload()  →  SQLite

This is the "receiver". For now it stores each message immediately. Later we
can add the in-memory queue + batching if data rates grow — but at your volume
(a message every minute) direct storage is perfectly fine and simpler.
"""

import json
import ssl
import paho.mqtt.client as mqtt

import db   # our database helper module (db.py)

# --------------------------- SETTINGS ---------------------------
# These are the only things you change when the topic/endpoint/certs change.

IOT_ENDPOINT = os.getenv("IOT_ENDPOINT", "your-endpoint-ats.iot.REGION.amazonaws.com")
IOT_PORT     = 8883                          # standard secure MQTT port

TOPIC        = os.getenv("MQTT_TOPIC", "DL/YOUR_TOPIC")                 # the topic to subscribe to
CLIENT_ID    = "flocdash-ingest-1"           # UNIQUE — must not clash with any other connection

# Certificate file paths (relative to where we run this script)
CA_CERT   = "certs/AmazonRootCA1.pem"
CERT_FILE = "certs/cert.pem.crt"
KEY_FILE  = "certs/private.pem.key"

# One shared database connection for the whole service.
conn = db.get_connection()


# --------------------------- CALLBACKS ---------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    """
    Runs when we successfully connect (or fail to) to AWS IoT Core.
    rc == 0 means success. Anything else is an error code.
    """
    if rc == 0:
        print(f"Connected to AWS IoT Core. Subscribing to '{TOPIC}'...")
        client.subscribe(TOPIC, qos=1)       # QoS 1 = broker re-sends if we miss one
    else:
        print(f"Connection failed with code {rc}")


def on_message(client, userdata, msg):
    """
    Runs every time a message arrives on the subscribed topic.
    msg.payload is raw bytes — we decode it to text, parse the JSON,
    then hand it to store_payload() to save in the database.
    """
    try:
        text = msg.payload.decode("utf-8")
        payload = json.loads(text)
    except Exception as e:
        print(f"Could not parse message: {e}")
        return

    try:
        db.store_payload(conn, payload)
    except Exception as e:
        print(f"Could not store message: {e}")


# --------------------------- MAIN ---------------------------

def main():
    # Create the MQTT client with our unique client id.
    client = mqtt.Client(client_id=CLIENT_ID, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    # Configure TLS (the secure certificate connection AWS IoT requires).
    client.tls_set(
        ca_certs=CA_CERT,
        certfile=CERT_FILE,
        keyfile=KEY_FILE,
        tls_version=ssl.PROTOCOL_TLSv1_2,
    )

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to {IOT_ENDPOINT} ...")
    client.connect(IOT_ENDPOINT, IOT_PORT, keepalive=60)

    # loop_forever() blocks here, handling messages until you stop it (Ctrl+C).
    client.loop_forever()


if __name__ == "__main__":
    main()
