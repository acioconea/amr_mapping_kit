from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from datetime import datetime, timezone

# --- Modele de Bază (Geometrie și Identificare) ---

class Coordinate(BaseModel):
    """Reprezintă un punct spațial în depozit."""
    x: float = Field(..., description="Coordonata X pe grid (metri)")
    y: float = Field(..., description="Coordonata Y pe grid (metri)")
    z: float = Field(default=0.0, description="Înălțimea Z a senzorului (metri)")

class AprilTagData(BaseModel):
    """Datele extrase de Vision System pentru un AprilTag detectat."""
    tag_id: int = Field(..., description="ID-ul unic al tag-ului")
    distance: float = Field(..., description="Distanța calculată până la tag")
    # Folosim un dicționar simplu pentru orientare (pitch, yaw, roll)
    pose: Dict[str, float] = Field(default_factory=dict, description="Orientarea spațială a tag-ului")

# --- Modele de Date Senzoriale ---

class TagTemperature(BaseModel):
    """Corelează un AprilTag cu temperatura citită pe pixelul său."""
    tag_id: int = Field(..., description="ID-ul AprilTag-ului")
    temperature: float = Field(..., description="Temperatura extrasă din matrice (°C)")

class ScanResult(BaseModel):
    """Rezultatul unei scanări la un anumit nivel Z."""
    z_level: float = Field(..., description="Nivelul Z la care s-a făcut achiziția")
    # AM ELIMINAT thermal_matrix și l-am înlocuit cu lista de temperaturi extrase
    tag_temperatures: List[TagTemperature] = Field(
        default_factory=list,
        description="Temperaturile corelate strict cu poziția tag-urilor"
    )
    tags: List[AprilTagData] = Field(default_factory=list, description="Datele spațiale ale tag-urilor")
    humidity: Optional[float] = Field(None, description="Umiditatea relativă (%)")

class ZMeasurement(BaseModel):
    """O singură măsurătoare termică la o înălțime specifică."""
    z_level: float = Field(..., description="Înălțimea senzorului (metri)")
    temperature: float = Field(..., description="Temperatura măsurată (°C)")
    humidity: Optional[float] = Field(None, description="Umiditatea relativă (%), doar la nivelul solului")

class PointAcquisition(BaseModel):
    """Profilul termic vertical complet pentru o singură coordonată (X, Y)."""
    x: float
    y: float
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    vertical_profile: List[ZMeasurement] = Field(..., description="Măsurătorile pe cele 3 înălțimi Z")

# --- Modele de Comunicare și Stocare ---

class TelemetryPayload(BaseModel):
    """Payload-ul final care este salvat pe SQLite și trimis prin MQTT."""
    mission_id: str = Field(..., description="Identificatorul unic al misiunii curente")
    point_id: Optional[int] = Field(None, description="ID-ul înregistrării din baza de date locală (SQLite)")
    data: PointAcquisition
    is_synced: bool = Field(default=False, description="Flag pentru starea de sincronizare cu Cloud-ul")

class MissionConfig(BaseModel):
    """Configurația de start pentru o misiune."""
    mission_id: str
    grid_points: List[Coordinate]
    z_levels: List[float] = Field(
        default=[0.2, 1.0, 1.8],
        description="Înălțimile standard pentru scanare în fiecare punct"
    )
    safe_battery_threshold: float = Field(default=15.0, description="Pragul la care se declanșează Handover-ul")

