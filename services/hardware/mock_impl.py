import asyncio
import random
import logging
from typing import List, Dict, Any

# Importăm interfețele abstracte definite anterior
from services.hardware.base import (
    TemperatureSensor,
    HumiditySensor,
    VisionSystem,
    AMRController
)

logger = logging.getLogger(__name__)


class MockTemperatureSensor(TemperatureSensor):
    """
    Simulează un senzor termic (ex: AMG8833 sau MLX90640).
    Returnează o matrice 8x8 cu temperaturi ambientale normale pentru un depozit farmaceutic (15°C - 25°C).
    """

    async def read_matrix(self) -> List[List[float]]:
        logger.debug("[Mock I2C] Inițializare citire senzor termic...")

        # Simulăm timpul de expunere și citire de pe bus-ul I2C (aprox. 300ms)
        await asyncio.sleep(0.3)

        # Generăm o matrice 8x8 cu valori ambientale de ~20°C
        # Introducem o ușoară variație (noise) realistă pentru senzori
        matrix = []
        for _ in range(8):
            row = [round(random.uniform(19.5, 21.0), 2) for _ in range(8)]
            matrix.append(row)

        # Opțional: Inserăm un "hotspot" artificial aleatoriu pentru a simula un echipament care se încălzește
        if random.random() > 0.8:
            rx, ry = random.randint(0, 7), random.randint(0, 7)
            matrix[rx][ry] = round(random.uniform(28.0, 32.0), 2)

        logger.debug("[Mock I2C] Matrice termică generată cu succes.")
        return matrix


class MockHumiditySensor(HumiditySensor):
    """
    Simulează un senzor de umiditate și temperatură (ex: BME280 sau SHT31).
    """

    async def read_humidity(self) -> float:
        logger.debug("[Mock I2C] Citire senzor umiditate...")

        # Senzorii de umiditate sunt de obicei foarte rapizi
        await asyncio.sleep(0.1)

        # Depozitele farmaceutice au umiditate controlată (ex: 45% - 60%)
        humidity = round(random.uniform(48.0, 52.0), 2)
        logger.debug(f"[Mock I2C] Umiditate citită: {humidity}%")

        return humidity


class MockVisionSystem(VisionSystem):
    """
    Simulează procesarea Computer Vision (ex: OpenCV / AprilTag detector rulând pe Edge TPU).
    """

    async def detect_apriltags(self) -> List[Dict[str, Any]]:
        logger.debug("[Mock Camera] Captură cadru și procesare AprilTags...")

        # Procesarea imaginii este "costisitoare", simulăm un delay mai mare (ex: 800ms)
        await asyncio.sleep(0.8)

        tags = []
        # Simulăm găsirea a 1 până la 3 tag-uri în câmpul vizual
        num_tags = random.randint(1, 3)

        for _ in range(num_tags):
            tag_data = {
                "tag_id": random.randint(10, 99),
                "distance": round(random.uniform(0.5, 3.5), 2),
                "pose": {
                    "pitch": round(random.uniform(-5.0, 5.0), 2),
                    "yaw": round(random.uniform(-15.0, 15.0), 2),
                    "roll": round(random.uniform(-2.0, 2.0), 2)
                }
            }
            tags.append(tag_data)

        logger.debug(f"[Mock Camera] Detectate {len(tags)} tag(uri).")
        return tags


class MockAMRController(AMRController):
    """
    Un controller complet simulat pentru robot.
    Ține evidența internă a poziției și consumă bateria proporțional cu distanța parcursă.
    """

    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.battery = 100.0

    async def go_to_xyz(self, x: float, y: float, z: float) -> bool:
        logger.info(f"[Mock AMR] Comandă de navigare primită: (X:{x}, Y:{y}, Z:{z})")

        # Calculăm distanța teoretică (Euclediană) pentru a aproxima timpul de deplasare
        distance = ((x - self.x) ** 2 + (y - self.y) ** 2 + (z - self.z) ** 2) ** 0.5

        # Simulare timp: 1 secundă pentru fiecare metru (pentru dev rapid, e accelerat față de realitate)
        travel_time = distance * 1.0

        logger.debug(f"[Mock AMR] Timp estimat deplasare: {travel_time:.2f}s")
        await asyncio.sleep(travel_time)

        # Actualizare status robot
        self.x, self.y, self.z = x, y, z

        # Consum baterie: 0.5% per metru
        self.battery -= (distance * 0.5)
        self.battery = max(0.0, self.battery)  # Evităm valori negative

        logger.info("[Mock AMR] Punct atins.")
        return True

    async def get_battery_level(self) -> float:
        await asyncio.sleep(0.1)  # Delay simulare interogare BMS
        return round(self.battery, 2)