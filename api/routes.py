from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import logging
import time
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from services.analysis.gdp_report import generate_gdp_report

# Presupunem că aceste importuri vin din modulele tale
from state_machine.amr_fsm import MappingMissionFSM

logger = logging.getLogger(__name__)

# Inițializăm router-ul
router = APIRouter(prefix="/api/v1/mission", tags=["Mission Control"])


# --- Modele Pydantic pentru Request/Response ---

class MissionStatusResponse(BaseModel):
    mission_id: Optional[str] = "N/A"
    current_state: str
    battery_level: float
    pending_sync_records: int
    current_target: Optional[Dict[str, float]] = None


class ActionResponse(BaseModel):
    status: str
    message: str


class TelemetryDataResponse(BaseModel):
    point_id: Optional[int]
    data: Dict[str, Any]


class ConnectionManager:
    def __init__(self):
        # Stocăm toate conexiunile active (poți avea mai multe tablete conectate simultan)
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Client UI conectat. Total conexiuni: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info("Client UI deconectat.")

    async def broadcast(self, message: dict):
        """Trimite date către toți clienții conectați simultan."""
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass # Ignorăm clienții care s-au deconectat brusc

ws_manager = ConnectionManager()


# --- Dependency Injection ---
# Într-o aplicație reală, această funcție ar returna instanța Singleton a FSM-ului
# configurată în main.py
def get_fsm() -> MappingMissionFSM:
    # Aici ar trebui să returnezi instanța globală a aplicației
    # ex: return request.app.state.fsm
    raise NotImplementedError("Dependency not configured in server.py")


# --- Endpoints ---

# --- Endpoint-ul WebSocket ---
@router.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket, fsm: MappingMissionFSM = Depends(get_fsm)):
    await ws_manager.connect(websocket)
    try:
        # Menținem conexiunea deschisă
        while True:
            # Aici putem primi mesaje de la UI (dacă vrem să înlocuim și butoanele de control)
            # Pentru moment doar ascultăm ca să ținem socket-ul viu
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


class MissionParams(BaseModel):
    length: float = Field(default=20.0, description="Lungimea depozitului (axa X)")
    width: float = Field(default=10.0, description="Lățimea depozitului (axa Y)")
    step_interval: float = Field(default=5.0, description="Din câți în câți metri se face măsurătoarea")
    measure_time: float = Field(default=3.0, description="Timpul de măsurare/staționare per punct (secunde)")


@router.post("/start", response_model=ActionResponse)
async def start_mission(params: MissionParams, fsm: MappingMissionFSM = Depends(get_fsm)):
    """
    Inițializează și pornește misiunea de mapare generând o grilă dinamică.
    """
    if fsm.state == 'INIT':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sistemul efectuează diagnoza de pornire. Vă rugăm așteptați câteva secunde!"
        )

    if fsm.state != 'IDLE':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Nu se poate porni misiunea. Stare curentă: {fsm.state}"
        )
    try:
        # --- GENERARE AUTOMATĂ A GRILEI DE COORDONATE ---
        new_grid = []
        x = 0.0
        while x <= params.length:
            y = 0.0
            while y <= params.width:
                new_grid.append({"x": round(x, 2), "y": round(y, 2)})
                y += params.step_interval
            x += params.step_interval

        if not new_grid:
            raise ValueError("Parametrii introduși nu au generat niciun punct valid!")

        # Încărcăm traseul și setările în Robot (FSM)
        fsm.mission_grid = new_grid
        fsm.dwell_time = params.measure_time  # Timpul de așteptare la măsurătoare
        fsm.mission_id = f"MISS_{int(time.time())}"

        logger.info(f"Misiune inițiată. Grilă generată cu {len(fsm.mission_grid)} puncte.")

        # Trecem în IDLE dacă suntem în INIT
        if fsm.state == 'INIT':
            await fsm.start_mission()

        # Declanșăm asincron bucla de navigație
        asyncio.create_task(fsm.next_point())

        return ActionResponse(status="success", message=f"Misiune pornită cu traseu de {len(fsm.mission_grid)} puncte.")

    except Exception as e:
        logger.error(f"Eroare la pornirea misiunii: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stop", response_model=ActionResponse)
async def stop_mission(fsm: MappingMissionFSM = Depends(get_fsm)):
    """
    Oprește forțat misiunea și trece robotul în starea de EROARE/STOP.
    """
    if fsm.state == 'ERROR':
        return ActionResponse(status="ignored", message="Mission is already stopped/in error state.")

    try:
        logger.warning("Oprire de urgență solicitată prin API.")
        await fsm.to_ERROR()  # Tranziție forțată conform mașinii de stări
        return ActionResponse(status="success", message="Mission forcefully stopped.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status", response_model=MissionStatusResponse)
async def get_system_status(fsm: MappingMissionFSM = Depends(get_fsm)):
    """
    Returnează starea în timp real a sistemului, bateria și metricele de sincronizare (Edge to Cloud).
    """
    try:
        battery = await fsm.amr.get_battery_level()
        unsynced = await fsm.storage.get_unsynced_count()

        return MissionStatusResponse(
            mission_id=fsm.mission_id,
            current_state=fsm.state,
            battery_level=battery,
            pending_sync_records=unsynced,
            current_target=fsm.current_target
        )
    except Exception as e:
        logger.error(f"Eroare la citirea statusului: {e}")
        raise HTTPException(status_code=500, detail="Nu s-a putut citi starea sistemului.")


@router.get("/data/latest", response_model=TelemetryDataResponse)
async def get_latest_data(fsm: MappingMissionFSM = Depends(get_fsm)):
    """
    Returnează ultimul set de date procesat și salvat de la nivelul Edge.
    Util pentru un dashboard local vizualizat pe tabletă de operatorul din depozit.
    """
    if not fsm.acquired_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nicio dată nu a fost colectată încă în această misiune."
        )

    return TelemetryDataResponse(
        point_id=fsm.acquired_data.get("point_id"),  # Dacă e setat de storage
        data=fsm.acquired_data
    )


@router.post("/reset", response_model=ActionResponse)
async def reset_mission(fsm: MappingMissionFSM = Depends(get_fsm)):
    """
    Scoate sistemul din starea de EROARE (Acknowledge) și îl readuce în IDLE.
    """
    # Permitem resetarea DOAR dacă robotul este blocat în eroare
    if fsm.state == 'ERROR':
        try:
            await fsm.reset_error()  # Declanșăm tranziția definită în FSM
            logger.info("Sistem resetat de operator. FSM a revenit în IDLE.")
            return ActionResponse(
                status="success",
                message="Sistem resetat cu succes. AMR este pregătit pentru o nouă misiune."
            )
        except Exception as e:
            logger.error(f"Eroare la resetarea sistemului: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # Dacă operatorul apasă reset când robotul merge bine, ignorăm comanda
    return ActionResponse(
        status="ignored",
        message=f"Sistemul funcționează normal. Nu necesită resetare (Stare: {fsm.state})"
    )


@router.post("/handover", response_model=ActionResponse)
async def manual_handover(fsm: MappingMissionFSM = Depends(get_fsm)):
    """Întrerupe manual misiunea curentă și pasează coordonatele rămase altui robot."""
    try:
        if fsm.state in ['INIT', 'ERROR', 'HANDOVER']:
            raise HTTPException(
                status_code=400,
                detail=f"Nu se poate iniția Handover din starea {fsm.state}"
            )

        await fsm.trigger_handover()
        return ActionResponse(status="success", message="Procedură de Handover inițiată. Misiunea a fost delegată.")
    except Exception as e:
        logger.error(f"Eroare Handover manual: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history", response_model=List[Dict[str, Any]])
async def get_mission_history(fsm: MappingMissionFSM = Depends(get_fsm)):
    """API pentru returnarea listei tuturor misiunilor salvate local."""
    try:
        return await fsm.storage.get_all_missions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history/{mission_id}", response_model=List[Dict[str, Any]])
async def get_mission_detail(mission_id: str, fsm: MappingMissionFSM = Depends(get_fsm)):
    """API pentru returnarea tuturor punctelor colectate dintr-o misiune specifică."""
    try:
        records = await fsm.storage.get_mission_telemetry(mission_id)
        if not records:
            raise HTTPException(status_code=404, detail="Misiunea nu are puncte salvate.")
        return records
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/report/{mission_id}", response_model=Dict[str, Any])
async def get_mission_gdp_report(mission_id: str, fsm: MappingMissionFSM = Depends(get_fsm)):
    """API pentru generarea raportului GDP / Analizei Termice MKT."""
    try:
        # Preia datele din baza de date folosind funcția pe care ai adăugat-o anterior
        records = await fsm.storage.get_mission_telemetry(mission_id)
        if not records:
            raise HTTPException(status_code=404, detail="Misiunea nu are puncte salvate.")

        # Generează raportul
        report = generate_gdp_report(mission_id, records)
        return report
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Eroare la generarea raportului GDP: {e}")
        raise HTTPException(status_code=500, detail="Eroare internă la procesarea datelor.")

