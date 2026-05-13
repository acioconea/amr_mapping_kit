import asyncio
from abc import ABC, abstractmethod
from typing import Dict, Any, List


class TemperatureSensor(ABC):
    @abstractmethod
    async def read_matrix(self) -> List[List[float]]:
        pass


class HumiditySensor(ABC):
    @abstractmethod
    async def read_humidity(self) -> float:
        pass


class VisionSystem(ABC):
    @abstractmethod
    async def detect_apriltags(self) -> List[Dict[str, Any]]:
        pass


class AMRController(ABC):
    @abstractmethod
    async def go_to_xyz(self, x: float, y: float, z: float) -> bool:
        pass

    @abstractmethod
    async def get_battery_level(self) -> float:
        pass