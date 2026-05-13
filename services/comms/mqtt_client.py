import asyncio
import json
import logging
from typing import Any, Dict, Optional

# Folosim clientul clasic, stabil, Paho MQTT
import paho.mqtt.client as mqtt
from starlette.concurrency import run_in_threadpool

from services.storage.edge_db import EdgeStorage

logger = logging.getLogger(__name__)


class MQTTClient:
    """
    Client MQTT stabil pentru telemetrie Edge-to-Cloud.
    Compatibil Cross-Platform (Windows/Linux) eliminând dependența de asyncio sockets.
    Folosește un pattern Queue Worker cu ThreadPool.
    """

    def __init__(self, broker: str, port: int = 1883, storage_ref: Optional[EdgeStorage] = None):
        self.broker = broker
        self.port = port
        self.storage_ref = storage_ref
        self._identifier = "amr_pharma_edge_01"

        self._queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._is_connected = False

        # Inițializăm Paho MQTT Client
        self._client = mqtt.Client(client_id=self._identifier)

        # Callbacks pentru statusul conexiunii
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._is_connected = True
            logger.info(f"[MQTT] Conectat la brokerul {self.broker}:{self.port} (Paho Worker)")
        else:
            logger.warning(f"[MQTT] Eroare conectare. Cod: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._is_connected = False
        logger.info("[MQTT] Deconectat de la broker.")

    async def connect(self):
        """Pornește task-ul asincron de fundal care gestionează publicarea."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._connection_worker())
            logger.info("[MQTT] Background Paho Worker pornit. Așteptare date...")

    async def disconnect(self):
        """Oprește curat worker-ul."""
        if self._worker_task:
            self._worker_task.cancel()
            self._client.disconnect()
            self._client.loop_stop()
            logger.info("[MQTT] Paho Worker oprit în siguranță.")

    async def publish(self, topic: str, payload: Dict[str, Any], record_id: int):
        """Adaugă payload-ul în coada asincronă spre a fi publicat."""
        await self._queue.put({"topic": topic, "payload": payload, "record_id": record_id})

    def _sync_connect_and_publish(self, topic, json_data):
        """
        Funcție sincronă blocantă (rulează într-un ThreadPool separat).
        Se conectează dacă este offline, publică mesajul și menține bucla (loop).
        """
        if not self._is_connected:
            try:
                self._client.connect(self.broker, self.port, keepalive=60)
                self._client.loop_start()  # Menține ping-urile pe fundal
            except Exception as e:
                # Aruncăm eroarea înapoi la asyncio_worker pentru a prinde Timeout-ul
                raise ConnectionError(f"Broker offline: {e}")

        # Publicăm cu QoS=1 (Garantează că mesajul ajunge cel puțin o dată la broker)
        info = self._client.publish(topic, payload=json_data, qos=1)
        info.wait_for_publish()  # Așteptăm confirmarea PUBACK de la broker
        return True

    async def _connection_worker(self):
        """Consumer asincron care deleagă apelurile de rețea unui ThreadPool."""
        reconnect_interval = 5

        while True:
            try:
                # 1. Așteptăm un mesaj în coadă
                msg = await self._queue.get()

                topic = msg["topic"]
                payload = msg["payload"]
                record_id = msg["record_id"]
                json_data = json.dumps(payload, default=str)

                # 2. Încercăm publicarea (rulând codul blocant Paho în alt Thread)
                success = False
                while not success:
                    try:
                        # run_in_threadpool preia execuția astfel încât FSM-ul robotului să nu înghețe
                        await run_in_threadpool(self._sync_connect_and_publish, topic, json_data)

                        logger.info(f"[MQTT] Payload #{record_id} publicat. Marcat ca Sincronizat în EdgeDB.")

                        # 3. Păstrăm istoricul (Audit Trail): Marchem pachetul ca fiind trimis
                        if self.storage_ref:
                            await self.storage_ref.mark_synced(record_id)

                        success = True  # Ieșim din bucla de reîncercare pentru acest mesaj
                        self._queue.task_done()

                    except ConnectionError as e:
                        logger.warning(f"[MQTT] {e}. Reîncercare în {reconnect_interval}s...")
                        await asyncio.sleep(reconnect_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MQTT] Eroare critică worker: {e}")
                await asyncio.sleep(reconnect_interval)