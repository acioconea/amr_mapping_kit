import json
import logging
import aiosqlite
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class EdgeStorage:
    """
    Gestionarul bazei de date locale (SQLite asincron) pentru AMR.
    Asigură persistența datelor la nivel Edge (Offline-First) și trasabilitatea (ALCOA+).
    """

    def __init__(self, db_path: str = "data/amr_telemetry.sqlite"):
        self.db_path = db_path

        # --- Modificarea adăugată ---
        # Extragem folderul părinte și ne asigurăm că este creat pe disc
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        # ----------------------------

    async def _init_db(self):
        """Inițializează schema bazei de date (EDGE)."""
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mission_id TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                payload JSON NOT NULL,
                is_synced INTEGER DEFAULT 0,
                synced_at TEXT
            );
            ''')
            # Index pentru căutare rapidă a celor nesincronizate
            await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_unsynced ON telemetry(is_synced) WHERE is_synced = 0;
            ''')
            await db.commit()

    async def save_payload(self, payload: Dict[str, Any]) -> int:
        """
        Salvează o citire completă a punctului (PointAcquisition) în SQLite.
        Returnează ID-ul rândului (record_id) pentru a fi folosit de MQTT.
        """
        mission_id = payload.get("mission_id", "unknown_mission")
        timestamp = datetime.now(timezone.utc).isoformat()

        # Serializăm dicționarul în JSON. Folosim default=str pentru a evita erori la datetime.
        payload_json = json.dumps(payload, default=str)

        query = """
        INSERT INTO telemetry (mission_id, timestamp_utc, payload, is_synced)
        VALUES (?, ?, ?, 0)
        """

        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(query, (mission_id, timestamp, payload_json))
                await db.commit()
                record_id = cursor.lastrowid

            logger.debug(f"[EdgeDB] Date salvate local. ID: {record_id}")
            return record_id

        except Exception as e:
            logger.error(f"[EdgeDB] Eroare la scrierea pe disc: {e}")
            raise

    async def mark_synced(self, record_id: int):
        """Marchează un pachet ca fiind trimis cu succes prin MQTT, păstrându-l pentru istoric."""
        from datetime import datetime, timezone
        import aiosqlite

        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE telemetry SET is_synced = 1, synced_at = ? WHERE id = ?",
                (now, record_id)
            )
            await db.commit()

    async def get_unsynced_count(self) -> int:
        """Numără doar pachetele care încă nu au plecat."""
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM telemetry WHERE is_synced = 0") as cursor:
                res = await cursor.fetchone()
                return res[0] if res else 0

    async def get_unsynced_records(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Extrage un batch de înregistrări nesincronizate.
        Poate fi folosit de un 'Sync Worker' (Cron task) care rulează pe fundal
        și încearcă să trimită pachetele pierdute atunci când robotul reintră în acoperire Wi-Fi.
        """
        query = "SELECT id, payload FROM telemetry WHERE is_synced = 0 LIMIT ?"
        records = []

        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query, (limit,)) as cursor:
                    async for row in cursor:
                        record_id, payload_json = row[0], row[1]

                        # Deserializare și reconstruire payload
                        payload_data = json.loads(payload_json)
                        payload_data["point_id"] = record_id  # Injectăm ID-ul pentru referință

                        records.append(payload_data)
            return records

        except Exception as e:
            logger.error(f"[EdgeDB] Eroare la citirea înregistrărilor nesincronizate: {e}")
            return []

    async def get_all_missions(self) -> List[Dict[str, Any]]:
        """Extrage lista tuturor misiunilor unice salvate în baza de date locală."""
        query = """
        SELECT mission_id, MIN(timestamp_utc) as started_at, COUNT(*) as points_count 
        FROM telemetry 
        GROUP BY mission_id 
        ORDER BY started_at DESC
        """
        missions = []
        try:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query) as cursor:
                    async for row in cursor:
                        missions.append({
                            "mission_id": row[0],
                            "started_at": row[1],
                            "points_count": row[2]
                        })
            return missions
        except Exception as e:
            logger.error(f"[EdgeDB] Eroare la citirea listei de misiuni: {e}")
            return []

    async def get_all_missions(self) -> List[Dict[str, Any]]:
        """Extrage lista tuturor misiunilor unice salvate în baza de date locală."""
        query = """
        SELECT mission_id, MIN(timestamp_utc) as started_at, COUNT(*) as points_count 
        FROM telemetry 
        GROUP BY mission_id 
        ORDER BY started_at DESC
        """
        missions = []
        try:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query) as cursor:
                    async for row in cursor:
                        missions.append({
                            "mission_id": row[0],
                            "started_at": row[1],
                            "points_count": row[2]
                        })
            return missions
        except Exception as e:
            logger.error(f"[EdgeDB] Eroare la citirea listei de misiuni: {e}")
            return []

    async def get_mission_telemetry(self, mission_id: str) -> List[Dict[str, Any]]:
        """Extrage toate înregistrările de telemetrie ale unei anumite misiuni."""
        query = "SELECT id, payload FROM telemetry WHERE mission_id = ? ORDER BY timestamp_utc ASC"
        records = []
        try:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query, (mission_id,)) as cursor:
                    async for row in cursor:
                        payload_data = json.loads(row[1])
                        payload_data["point_id"] = row[0]
                        records.append(payload_data)
            return records
        except Exception as e:
            logger.error(f"[EdgeDB] Eroare la citirea telemetriei pentru misiunea {mission_id}: {e}")
            return []


    async def close(self):
        """
        Datorită utilizării context managerilor (async with), conexiunile sunt închise automat.
        Această metodă este păstrată pentru compatibilitate arhitecturală cu lifespan-ul din server.py.
        """
        pass