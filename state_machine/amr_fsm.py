import asyncio
import logging
from transitions.extensions.asyncio import AsyncMachine
from services.hardware.base import TemperatureSensor, HumiditySensor, VisionSystem, AMRController
from services.storage.edge_db import EdgeStorage
from services.comms.mqtt_client import MQTTClient

logger = logging.getLogger(__name__)


class MappingMissionFSM:
    states = [
        'INIT', 'IDLE', 'NAV_AND_POS', 'ACQUISITION',
        'DATA_MGMT', 'RESOURCE_GUARD', 'BACKEND_PROC', 'ERROR'
    ]

    def __init__(self, amr: AMRController, therm: TemperatureSensor,
                 hum: HumiditySensor, vis: VisionSystem,
                 storage: EdgeStorage, mqtt: MQTTClient, grid: list):
        self.amr = amr
        self.therm = therm
        self.hum = hum
        self.vis = vis
        self.storage = storage
        self.mqtt = mqtt

        self.mission_grid = grid  # Lista de coordonate (x, y)
        self.current_target = None
        self.acquired_data = {}

        self.machine = AsyncMachine(model=self, states=MappingMissionFSM.states, initial='INIT')

        # Trecerea din INIT în IDLE
        self.machine.add_transition('start_mission', 'INIT', 'IDLE')

        # --- LOGICA CICLICĂ ---
        # next_point este declanșat la start (din IDLE) sau în buclă (din RESOURCE_GUARD)
        self.machine.add_transition('next_point', ['IDLE', 'RESOURCE_GUARD'], 'NAV_AND_POS')
        self.machine.add_transition('reached_target', 'NAV_AND_POS', 'ACQUISITION')
        self.machine.add_transition('data_collected', 'ACQUISITION', 'DATA_MGMT')
        self.machine.add_transition('data_saved', 'DATA_MGMT', 'RESOURCE_GUARD')

        # --- FINALIZAREA CICLULUI ---
        # Trecem în BACKEND_PROC doar când se termină punctele din RESOURCE_GUARD
        self.machine.add_transition('process_backend', 'RESOURCE_GUARD', 'BACKEND_PROC')

        # După ce BACKEND_PROC termină raportul, se întoarce în IDLE, așteptând o nouă misiune
        self.machine.add_transition('mission_complete', 'BACKEND_PROC', 'IDLE')

        # Reguli de siguranță (Rămân neschimbate)
        self.machine.add_transition('low_battery', '*', 'ERROR')
        self.machine.add_transition('reset_error', 'ERROR', 'IDLE')

    async def on_enter_NAV_AND_POS(self):
        """Trimite coordonatele către AMR și așteaptă confirmarea."""
        if not self.mission_grid:
            logger.info("Misiune finalizată.")
            await self.to_IDLE()
            return

        self.current_target = self.mission_grid.pop(0)
        x, y = self.current_target['x'], self.current_target['y']

        logger.info(f"Navigare către X:{x}, Y:{y}")
        reached = await self.amr.go_to_xyz(x, y, 0.0)  # Robotul se deplasează la nivelul solului

        if reached:
            await self.reached_target()
        else:
            await self.to_ERROR()

    async def on_enter_ACQUISITION(self):
        """
        Logica de Mapare Verticală (3D Thermal Profiling):
        - Robotul staționează la X, Y.
        - Senzorul urcă pe axa Z la înălțimile prestabilite.
        - Se înregistrează o singură temperatură pentru fiecare (X, Y, Z).
        """
        logger.info(f"Începere scanare profil vertical la X:{self.current_target['x']}, Y:{self.current_target['y']}")

        z_levels = [0.2, 1.0, 1.8]
        measurements = []

        try:
            for index, z in enumerate(z_levels):
                logger.info(f"Ridicăm senzorul la Z = {z}m...")

                # Robotul rămâne pe loc, mișcăm doar axa Z
                await self.amr.go_to_xyz(self.current_target['x'], self.current_target['y'], z)

                await asyncio.sleep(self.dwell_time)

                # Citim senzorii
                tasks = [self.therm.read_matrix()]
                if index == 0:
                    tasks.append(self.hum.read_humidity())

                results = await asyncio.gather(*tasks)

                # Extragem o SINGURĂ temperatură reprezentativă pentru această înălțime.
                # (De ex: media temperaturilor din centrul camerei termice)
                thermal_matrix = results[0]
                center_temp = (thermal_matrix[3][3] + thermal_matrix[3][4] +
                               thermal_matrix[4][3] + thermal_matrix[4][4]) / 4.0

                # Salvăm punctul spațial (X, Y, Z) -> Temperatură
                measurement = {
                    "z_level": z,
                    "temperature": round(center_temp, 2),
                    "humidity": results[1] if index == 0 else None
                }
                measurements.append(measurement)

                logger.debug(f"Punct (Z={z}m) scanat. Temp: {measurement['temperature']}°C")

            # Asamblăm payload-ul final pentru acest (X, Y)
            self.acquired_data = {
                "x": self.current_target['x'],
                "y": self.current_target['y'],
                "vertical_profile": measurements
            }

            await self.data_collected()

        except Exception as e:
            logger.error(f"Eroare în timpul scanării verticale: {e}")
            await self.to_ERROR()

    async def on_enter_DATA_MGMT(self):
        """Redundanță: Salvare Edge -> Publish MQTT"""
        logger.info("Salvare date...")

        # 1. Salvare sigură pe SQLite (Edge)
        record_id = await self.storage.save_payload(self.acquired_data)

        # 2. Fire and forget către MQTT. Dacă pică, storage-ul reține flag-ul de nesincronizat.
        asyncio.create_task(self.mqtt.publish("pharma/amr/telemetry", self.acquired_data, record_id))

        await self.data_saved()

    async def on_enter_RESOURCE_GUARD(self):
        """Validăm siguranța și decidem dacă reluăm ciclul sau finalizăm."""
        logger.info("Se validează resursele bateriei...")

        battery = await self.amr.get_battery_level()
        if battery < 20.0:
            logger.warning(f"Baterie critică ({battery:.1f}%). Oprire de siguranță!")
            await self.low_battery()
            return

        logger.info(f"Resurse OK ({battery:.1f}%). Evaluare parcurs misiune...")

        # --- RUTARE CICLICĂ ---
        if len(self.mission_grid) > 0:
            logger.info(f"Puncte rămase: {len(self.mission_grid)}. Se reia ciclul spre următorul punct.")
            await self.next_point()
        else:
            logger.info("Grid de misiune epuizat (Toate punctele mapate). Trecem la Backend Processing.")
            await self.process_backend()

    async def on_enter_BACKEND_PROC(self):
        """Procesări finale: validări, rapoarte agregate de final de misiune."""
        logger.info("Se execută procedurile de Backend (Generare raport final)...")

        # Aici poți adăuga logică complexă de validare
        await asyncio.sleep(1.5)  # Simulează un proces de calcul/sincronizare

        logger.info("Procesare Backend finalizată cu succes.")

        # Închidem misiunea cu succes și revenim la așteptare
        await self.mission_complete()