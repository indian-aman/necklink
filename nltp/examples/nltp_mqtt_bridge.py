#!/usr/bin/env python3
"""
NLTP -> MQTT First-Mile Edge Ingest Bridge (Zero-Cost Reference Pattern)
Receives ChaCha20-Poly1305 + X25519 + GF(2^8) Cauchy-FEC + LZ4 verified payloads
from lossy 4G CGNAT / RF / ESP32-Gateway nodes and publishes clean JSON to MQTT.

Install: pip install --upgrade pynltp paho-mqtt
"""
import json
import os
import paho.mqtt.client as mqtt
from nltp import NLTPServer

NLTP_PSK = os.environ["NLTP_PSK"]  # Generate once via: nltp gen-key
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mq.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
mq.loop_start()

with NLTPServer(("0.0.0.0", 9000), psk=NLTP_PSK) as ingest:
    print("NLTP First-Mile Ingest listening on UDP :9000 -> MQTT", MQTT_HOST)
    while True:
        item = ingest.recv(timeout=5.0)
        if item is None:
            continue
        if item.kind == "reflex":
            # Out-of-band priority alert bypassed bulk queue (0 HOL blocking)
            mq.publish("fleet/alerts/reflex", item.data, qos=1)
        elif item.sha256_verified:
            # 100% SHA-256 verified & LZ4-decompressed batch -> fan out to MQTT topics
            for line in item.data.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    node_id = rec.get("node", "edge-unknown")
                    mq.publish(f"fleet/telemetry/{node_id}", line, qos=1)
