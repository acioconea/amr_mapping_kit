import sqlite3
import json
import os
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

# Setări Cloud
CLOUD_DB_PATH = "data/cloud_database.sqlite"
BROKER_ADDRESS = "broker.hivemq.com"  # Același broker folosit de Edge
MQTT_TOPIC = "pharma/amr/telemetry"


def setup_cloud_db():
    """Creează baza de date pentru Cloud dacă nu există."""
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(CLOUD_DB_PATH)
    # MODIFICAT: Am adăugat coloana mission_id pentru trasabilitate completă
    conn.execute('''
        CREATE TABLE IF NOT EXISTS amr_global_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT,
            received_at_utc TEXT NOT NULL,
            x REAL,
            y REAL,
            vertical_profile JSON NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    print("[CLOUD-DB] Baza de date Cloud a fost inițializată.")


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[CLOUD-MQTT] Conectat la brokerul {BROKER_ADDRESS}. Așteptare date...")
        client.subscribe(MQTT_TOPIC)
    else:
        print(f"[CLOUD-MQTT] Eroare conectare: {rc}")


def on_message(client, userdata, msg):
    """Ce se întâmplă când Cloud-ul primește un pachet MQTT de la robot."""
    payload_str = msg.payload.decode('utf-8')
    print(f"\n[CLOUD-RX] Pachet primit pe '{msg.topic}'")

    try:
        data = json.loads(payload_str)

        # MODIFICAT: Extragem mission_id din pachetul trimis de Edge
        mission_id = data.get("mission_id", "unknown_mission")
        x = data.get("x")
        y = data.get("y")
        profile = json.dumps(data.get("vertical_profile", []))
        received_at = datetime.now(timezone.utc).isoformat()

        # Salvăm în baza de date centrală (Cloud)
        conn = sqlite3.connect(CLOUD_DB_PATH)
        # MODIFICAT: Includem mission_id în query
        conn.execute(
            "INSERT INTO amr_global_telemetry (mission_id, received_at_utc, x, y, vertical_profile) VALUES (?, ?, ?, ?, ?)",
            (mission_id, received_at, x, y, profile)
        )
        conn.commit()
        conn.close()

        print(f"[CLOUD-DB] Date salvate în Cloud pentru Misiunea: {mission_id} | Coordonate (X:{x}, Y:{y})")

    except Exception as e:
        print(f"[CLOUD-ERROR] Eroare la procesarea pachetului: {e}")


if __name__ == "__main__":
    print("=" * 50)
    print(" PORNIRE SERVICIU BACKEND CLOUD (MQTT Consumer) ")
    print("=" * 50)

    setup_cloud_db()

    # Inițializăm clientul Paho MQTT
    client = mqtt.Client(client_id="pharma_cloud_receiver_01")
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(BROKER_ADDRESS, 1883, 60)
        # Menținem serverul deschis la infinit pentru a asculta mesajele de la AMR
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[CLOUD] Server oprit de operator.")