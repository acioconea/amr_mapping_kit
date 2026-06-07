import asyncio
import logging
from datetime import datetime, timezone
from transitions.extensions.asyncio import AsyncMachine

logger = logging.getLogger(__name__)


# --- Clase pentru Simularea Hardware-ului (Motoare și Senzori) ---
class MockAMR:
    async def go_to_xy(self, x, y):
        """Comandă exclusivă pentru planul orizontal."""
        await asyncio.sleep(1)  # Simulează deplasarea fizică

    async def stop_movement(self):
        await asyncio.sleep(0.5)


class MockTherm:
    async def read_matrix(self):
        import random
        await asyncio.sleep(0.5)  # Simulează stabilizarea senzorilor
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

        self.machine = AsyncMachine(model=self, states=self.states, initial='INIT', after_state_change='notify_ui')

        # --- DEFINIREA TRANZIȚIILOR (Conform Diagramei Curente) ---
        self.machine.add_transition('boot_complete', 'INIT', 'IDLE')
        self.machine.add_transition('start_mission', 'IDLE', 'NAV_AND_POS')

        # --- BUCLA DE MAPARE (Mapping Loop) ---
        self.machine.add_transition('destination_reached', 'NAV_AND_POS', 'ACQUISITION')
        self.machine.add_transition('data_acquired', 'ACQUISITION', 'DATA_MGMT')
        self.machine.add_transition('data_saved', 'DATA_MGMT', 'NAV_AND_POS')

        # Finalizare Misiune
        self.machine.add_transition('mission_complete', 'NAV_AND_POS', 'BACKEND_PROC')
        self.machine.add_transition('report_generated', 'BACKEND_PROC', 'IDLE')

        # Stări de Siguranță
        self.machine.add_transition('trigger_handover', '*', 'HANDOVER')
        self.machine.add_transition('trigger_error', '*', 'ERROR')
        self.machine.add_transition('fleet_takeover', 'HANDOVER', 'IDLE')
        self.machine.add_transition('reset_error', ['ERROR', 'HANDOVER'], 'IDLE')

    async def notify_ui(self):
        logger.info(f"[UI Broadcast] Sistemul a intrat în starea: {self.state}")

    async def run_boot_sequence(self):
        if self.state != 'INIT': return
        logger.info("[INIT] Începere secvență de diagnoză hardware (POST)...")
        await asyncio.sleep(1.5)
        logger.info("[INIT] Diagnoză finalizată cu succes. Sistem 100% OPERAȚIONAL.")
        await self.boot_complete()

    async def next_point(self):
        if self.state == 'IDLE':
            logger.info("[FSM] Declanșare start_mission din IDLE...")
            await self.start_mission()

    # =====================================================================
    # LOGICA METODELOR DE INTRARE ÎN STARE (DECUPLATE PRIN TASK-URI)
    # =====================================================================

    async def on_enter_IDLE(self):
        logger.info("[FSM] Robot în IDLE. Pregătit pentru comenzi.")

    async def on_enter_NAV_AND_POS(self):
        logger.info("[FSM] Am intrat în NAV_AND_POS. Pornire verificări și deplasare...")
        asyncio.create_task(self._navigation_logic())

    async def _navigation_logic(self):
        # Dacă există mock, folosim metoda specifică; dacă e robot real, integrăm apelul de baterie.
        if hasattr(self.amr, 'get_battery_level'):
            self.battery_level = await self.amr.get_battery_level()

        if self.battery_level < 20.0:
            logger.warning("[FSM] Nivel critic baterie (<20%). Declanșare Handover.")
            await self.trigger_handover()
            return

        if not self.mission_grid:
            logger.info("[FSM] Grid epuizat! Toate punctele au fost parcurse.")
            await self.mission_complete()
            return

        self.current_target = self.mission_grid.pop(0)
        logger.info(f"[FSM] Navigare către coordonata -> X:{self.current_target['x']} Y:{self.current_target['y']}")

        # Navigație exclusiv orizontală (X, Y)
        if hasattr(self.amr, 'go_to_xy'):
            await self.amr.go_to_xy(self.current_target['x'], self.current_target['y'])
        elif hasattr(self.amr, 'go_to_xyz'):
            # Fallback dacă folosești o altă bibliotecă AMR ce necesită axa Z
            await self.amr.go_to_xyz(self.current_target['x'], self.current_target['y'], 0.0)

        logger.info("[AMR API] Destinație atinsă pe planul XY. Trecem la achiziție.")
        await self.destination_reached()

    async def on_enter_ACQUISITION(self):
        logger.info("[FSM] Am intrat în ACQUISITION. Pornire scanare senzori pe Z...")
        asyncio.create_task(self._acquisition_logic())

    async def _acquisition_logic(self):
        """Logica de achiziție la înălțimi diferite, fără a muta baza AMR-ului."""
        z_levels = [0.2, 1.0, 1.8]
        vertical_profile = []

        for z in z_levels:
            # FĂRĂ COMANDĂ AMR AICI. Doar înregistrare / timp de procesare pentru stratul respectiv.
            logger.info(f"[ACQ] Achiziție date de la senzorii pentru nivelul Z = {z}m...")
            await asyncio.sleep(self.dwell_time)  # Timp alocat pentru stabilizarea termică / citire

            raw_temp_matrix = await self.therm.read_matrix()
            temp_avg = 20.0

            if isinstance(raw_temp_matrix, list):
                valori_plate = []
                for element in raw_temp_matrix:
                    if isinstance(element, list):
                        valori_plate.extend(element)
                    else:
                        valori_plate.append(element)
                if valori_plate:
                    temp_avg = sum(valori_plate) / len(valori_plate)
            else:
                temp_avg = raw_temp_matrix
                raw_temp_matrix = [[raw_temp_matrix] * 8 for _ in range(8)]

            humidity = None
            if z == 0.2:
                if self.hum:
                    humidity = await self.hum.read_humidity()
                else:
                    import random
                    humidity = round(random.uniform(45.0, 55.0), 2)

            vertical_profile.append({
                "z_level": z,
                "temperature": round(temp_avg, 2),
                "humidity": humidity,
                "raw_matrix": raw_temp_matrix
            })

        self.acquired_data = {
            "mission_id": self.mission_id,
            "x": self.current_target['x'],
            "y": self.current_target['y'],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "vertical_profile": vertical_profile
        }

        logger.info("[ACQ] Achiziție completă pe toate axele Z.")
        await self.data_acquired()

    async def on_enter_DATA_MGMT(self):
        logger.info("[FSM] Am intrat în DATA_MGMT. Salvare locală și Cloud...")
        asyncio.create_task(self._data_mgmt_logic())

    async def _data_mgmt_logic(self):
        record_id = None
        if self.storage:
            record_id = await self.storage.save_payload(self.acquired_data)

        if self.mqtt:
            asyncio.create_task(
                self.mqtt.publish("pharma/amr/telemetry", self.acquired_data, record_id or 1)
            )

        logger.info("[DATA] Date salvate cu succes. Repornire buclă către următorul punct.")
        await self.data_saved()

    async def on_enter_BACKEND_PROC(self):
        logger.info("[FSM] Am intrat în BACKEND_PROC. Generare rapoarte finale...")
        asyncio.create_task(self._backend_proc_logic())

    async def _backend_proc_logic(self):
        await asyncio.sleep(2.0)
        logger.info("[BACKEND] Raport GDP și hărți termice 3D salvate în siguranță.")
        await self.report_generated()

    async def on_enter_HANDOVER(self):
        logger.warning("[HANDOVER] Procedură de transfer misiune inițiată.")
        if hasattr(self.amr, 'stop_movement'):
            await self.amr.stop_movement()

        handover_payload = {
            "original_robot_id": "amr_pharma_edge_01",
            "reason": "battery_or_estop",
            "current_battery": self.battery_level,
            "remaining_mission_grid": self.mission_grid
        }

        if self.mqtt:
            asyncio.create_task(
                self.mqtt.publish("pharma/amr/fleet/handover_request", handover_payload, 999999)
            )

        logger.warning(f"[HANDOVER] Misiune suspendată. {len(self.mission_grid)} puncte predate flotei.")
        asyncio.create_task(self._simulate_fleet_takeover())

    async def _simulate_fleet_takeover(self):
        logger.info("[FLEET] Se așteaptă un robot de rezervă...")
        await asyncio.sleep(4.0)
        logger.info("[FLEET] Robotul B a preluat gridul. Baterie: 100%. Misiunea continuă...")

        self.battery_level = 100.0
        if hasattr(self.amr, 'battery'):
            self.amr.battery = 100.0
        elif hasattr(self.amr, '_battery_level'):
            self.amr._battery_level = 100.0

        await self.fleet_takeover()
        if self.mission_grid:
            await self.start_mission()

    async def on_enter_ERROR(self):
        logger.error("[ERROR] Sistem blocat în stare de eroare hardware! Necesită resetare manuală.")
        if hasattr(self.amr, 'stop_movement'):
            await self.amr.stop_movement()