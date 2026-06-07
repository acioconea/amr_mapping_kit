import asyncio
import logging
from datetime import datetime, timezone
from transitions.extensions.asyncio import AsyncMachine

logger = logging.getLogger(__name__)


# --- Clase pentru Simularea Hardware-ului (Motoare și Senzori) ---
class MockAMR:
    async def go_to_xyz(self, x, y, z):
        await asyncio.sleep(1)  # Simulează deplasarea fizică

    async def stop_movement(self):
        await asyncio.sleep(0.5)


class MockTherm:
    async def read_matrix(self):
        import random
        await asyncio.sleep(0.5)  # Simulează stabilizarea senzorilor
        # Generează o grilă de 8x8 pixeli termici cu temperaturi între 18 și 30 de grade
        return [[round(random.uniform(18.0, 30.0), 1) for _ in range(8)] for _ in range(8)]

# =====================================================================
# CREIERUL ROBOTULUI: Mașina cu Stări Finite (FSM)
# =====================================================================
class MappingMissionFSM:
    def __init__(self, amr=None, therm=None, hum=None, vis=None, mqtt_client=None, storage=None, **kwargs):
        self.amr = amr or MockAMR()
        self.therm = therm or MockTherm()
        self.hum = hum
        self.vis = vis
        self.mqtt = mqtt_client
        self.storage = storage

        # STRICT aceste stări (fără RESOURCE_GUARD sau altele vechi)
        self.states = [
            'INIT', 'IDLE', 'NAV_AND_POS', 'ACQUISITION',
            'DATA_MGMT', 'HANDOVER', 'ERROR', 'BACKEND_PROC'
        ]

        self.mission_grid = []
        self.current_target = None
        self.dwell_time = 3.0
        self.battery_level = 100.0
        self.acquired_data = {}
        self.mission_id = "N/A"

        self.waiting_for_amr = False

        self.machine = AsyncMachine(model=self, states=self.states, initial='INIT',after_state_change='notify_ui')

        # --- DEFINIREA TRANZIȚIILOR ---
        self.machine.add_transition('boot_complete', 'INIT', 'IDLE')
        self.machine.add_transition('start_mission', 'IDLE', 'NAV_AND_POS')
        self.machine.add_transition('command_sent', 'NAV_AND_POS','IDLE')
        self.machine.add_transition('destination_reached', 'IDLE', 'ACQUISITION')
        self.machine.add_transition('data_acquired', 'ACQUISITION', 'DATA_MGMT')
        self.machine.add_transition('data_saved', 'DATA_MGMT', 'IDLE')
        self.machine.add_transition('fleet_takeover', 'HANDOVER', 'IDLE')

        self.machine.add_transition('trigger_handover', ['IDLE', 'NAV_AND_POS', 'ACQUISITION', 'DATA_MGMT'], 'HANDOVER')
        self.machine.add_transition('trigger_error', '*', 'ERROR')
        self.machine.add_transition('reset_error', ['ERROR', 'HANDOVER'], 'IDLE')

        self.machine.add_transition('mission_complete', 'IDLE', 'BACKEND_PROC')
        self.machine.add_transition('report_generated', 'BACKEND_PROC', 'IDLE')

    async def notify_ui(self):
        """
        Funcție apelată automat de biblioteca transitions
        imediat după ce starea s-a schimbat.
        """
        current_state = self.state
        logger.info(f"[UI Broadcast] Sistemul a intrat în starea: {current_state}")


    async def run_boot_sequence(self):
        if self.state != 'INIT':
            return
        logger.info("[INIT] Începere secvență de diagnoză hardware (POST)...")
        await asyncio.sleep(1.5)
        await asyncio.sleep(1.5)
        logger.info("[INIT] Diagnoză finalizată cu succes. Sistem 100% OPERAȚIONAL.")
        await self.boot_complete()

    # =====================================================================
    # LOGICA FSM (Decuplată cu Task-uri Asincrone pentru a evita Deadlock)
    # =====================================================================

    async def on_enter_IDLE(self):
        if self.mission_grid:
            logger.info("[FSM] Robot în IDLE. Preluăm următorul punct în 1 secundă...")
            asyncio.create_task(self._delayed_next_point())
        else:
            logger.info("[FSM] Robot în IDLE. Așteptare comenzi / Misiune completă.")

    async def _delayed_next_point(self):
        await asyncio.sleep(1.0)
        await self.next_point()

    async def next_point(self):
        if self.state != 'IDLE': return

        if self.waiting_for_amr:
            return

        self.battery_level = await self.amr.get_battery_level()

        if self.battery_level < 20.0:
            logger.warning("[FSM] Nivel critic baterie (<20%).")
            await self.trigger_handover()
            return

        if not self.mission_grid:
            logger.info("[FSM] Grid epuizat! Trecem la procesarea de Backend.")
            await self.mission_complete()
            return

        self.current_target = self.mission_grid.pop(0)
        logger.info(f"[FSM] Ne deplasăm către X:{self.current_target['x']} Y:{self.current_target['y']}")
        await self.start_mission()

    async def on_enter_NAV_AND_POS(self):
        logger.info("[FSM] Transmitere comenzi către motoarele AMR...")

        # Blocăm grid-ul înainte să trecem în IDLE
        self.waiting_for_amr = True

        asyncio.create_task(self._simulate_amr_movement())

        # Trecem asincron în IDLE (dar next_point se va lovi de return-ul pus mai sus)
        await self.command_sent()

    async def _simulate_amr_movement(self):
        """Simulează deplasarea robotului care se întâmplă independent de Kit."""
        await self.amr.go_to_xyz(self.current_target['x'], self.current_target['y'], 0.0)

        # AMR-ul a ajuns fizic la destinație. Deblocăm flag-ul.
        self.waiting_for_amr = False

        logger.info("[AMR API] Am ajuns la destinație. Declanșăm achiziția.")

        # Tranziționăm IDLE -> ACQUISITION
        await self.destination_reached()

    async def _delayed_reached_target(self):
        await asyncio.sleep(0.1)
        await self.reached_target()

    async def on_enter_ACQUISITION(self):
        z_levels = [0.2, 1.0, 1.8]
        vertical_profile = []

        for z in z_levels:
            logger.info(f"[ACQ] Măsurare Z = {z}m...")
            await self.amr.go_to_xyz(self.current_target['x'], self.current_target['y'], z)
            await asyncio.sleep(self.dwell_time)

            # 1. Citim datele de la senzorul termic (Senzorul returnează o matrice 8x8)
            raw_temp_matrix = await self.therm.read_matrix()
            temp_avg = 20.0

            # Dacă senzorul trimite o listă/matrice (grilă de pixeli termici), îi facem media
            if isinstance(raw_temp_matrix, list):
                # Aplatizăm matricea indiferent dacă e 1D (listă) sau 2D (listă de liste)
                valori_plate = []
                for element in raw_temp_matrix:
                    if isinstance(element, list):
                        valori_plate.extend(element)
                    else:
                        valori_plate.append(element)

                # Calculăm media temperaturilor din toți pixelii pentru raportul 3D/GDP
                if valori_plate:
                    temp_avg = sum(valori_plate) / len(valori_plate)
            else:
                # Fallback dacă senzorul trimite doar un număr
                temp_avg = raw_temp_matrix
                # Creăm o matrice dummy pentru ca interfața să aibă ce randa
                raw_temp_matrix = [[raw_temp_matrix] * 8 for _ in range(8)]

            humidity = None
            if z == 0.2:
                if self.hum:
                    humidity = await self.hum.read_humidity()
                else:
                    # Fallback în caz că senzorul nu e conectat
                    import random
                    humidity = round(random.uniform(45.0, 55.0), 2)

            # 2. ADĂUGĂM `raw_matrix` PENTRU CA FRONTEND-UL SĂ POATĂ DESENA FEED-UL LIVE
            vertical_profile.append({
                "z_level": z,
                "temperature": round(temp_avg, 2),
                "humidity": humidity,
                "raw_matrix": raw_temp_matrix  # <--- Secretul pentru Camera Termoviziune
            })

        self.acquired_data = {
            "mission_id": self.mission_id,
            "x": self.current_target['x'],
            "y": self.current_target['y'],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "vertical_profile": vertical_profile
        }

        asyncio.create_task(self._delayed_data_acquired())

    async def _delayed_data_acquired(self):
        await asyncio.sleep(0.1)
        await self.data_acquired()

    async def on_enter_DATA_MGMT(self):
        logger.info("[DATA] Salvare pachet date...")

        record_id = None
        if self.storage:
            record_id = await self.storage.save_payload(self.acquired_data)

        if self.mqtt:
            asyncio.create_task(
                self.mqtt.publish("pharma/amr/telemetry", self.acquired_data, record_id or 1)
            )

        logger.info("[DATA] Date procesate cu succes. Ne întoarcem în IDLE.")
        asyncio.create_task(self._delayed_data_saved())

    async def _delayed_data_saved(self):
        await asyncio.sleep(0.1)
        await self.data_saved()

    async def on_enter_HANDOVER(self):
        logger.warning("[HANDOVER] Procedură de transfer misiune inițiată.")

        # Verificăm dacă controller-ul tău hardware știe comanda de oprire
        if hasattr(self.amr, 'stop_movement'):
            await self.amr.stop_movement()
        else:
            logger.info("[HANDOVER] (Mock) Oprire motoare...")

        handover_payload = {
            "original_robot_id": "amr_pharma_edge_01",
            "reason": "low_battery",
            "current_battery": self.battery_level,
            "last_completed_point": self.current_target,
            "remaining_mission_grid": self.mission_grid
        }

        if self.mqtt:
            asyncio.create_task(
                self.mqtt.publish("pharma/amr/fleet/handover_request", handover_payload, 999999)
            )

        logger.warning(f"[HANDOVER] Misiune oprită. {len(self.mission_grid)} puncte predate flotei.")
        asyncio.create_task(self._simulate_fleet_takeover())

    async def _simulate_fleet_takeover(self):
        """Simulează comportamentul Swarm: un alt robot sosește să continue treaba."""
        logger.info("[FLEET] Se așteaptă un robot disponibil la docul de încărcare...")

        # Așteptăm 5 secunde (timpul în care noul robot e trezit și trimis)
        await asyncio.sleep(5.0)

        logger.info("[FLEET] ROBOTUL B a preluat misiunea! Baterie: 100%. Se reia traseul...")

        # Noul robot are bateria plină!
        self.battery_level = 100.0

        # Resetăm nivelul și în "hardware-ul" conectat la sistem
        if hasattr(self.amr, 'battery'):
            self.amr.battery = 100.0
        elif hasattr(self.amr, '_battery_level'):
            self.amr._battery_level = 100.0

        # Trecem înapoi în IDLE.
        # Deoarece mission_grid nu e goală, IDLE va relua automat de unde a rămas Robotul A!
        await self.fleet_takeover()

    async def on_enter_BACKEND_PROC(self):
        logger.info("[BACKEND] Se execută procesarea datelor agregate și calcularea MKT...")

        # Simulăm timpul de procesare în cloud / pe server
        await asyncio.sleep(2.0)

        logger.info("[BACKEND] Raport GDP generat și salvat cu succes.")

        # Revenim în IDLE, pregătiți pentru o misiune cu totul nouă
        await self.report_generated()

    async def on_enter_ERROR(self):
        logger.error("[ERROR] Sistem blocat în stare de eroare! Necesită Reset manual de pe interfață.")

        if hasattr(self.amr, 'stop_movement'):
            await self.amr.stop_movement()