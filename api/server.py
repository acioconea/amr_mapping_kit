import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn

# --- Importuri din modulele locale ---
from api.routes import router, get_fsm, ws_manager
from state_machine.amr_fsm import MappingMissionFSM

# Presupunem că ai creat aceste implementări de Mock în serviciile tale pentru dezvoltare
from services.hardware.mock_impl import (
    MockTemperatureSensor,
    MockHumiditySensor,
    MockVisionSystem,
    MockAMRController
)
from services.storage.edge_db import EdgeStorage
from services.comms.mqtt_client import MQTTClient

# Configurare Logging conform standardelor (stdout pentru containere Docker)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Variabilă globală pentru instanța de State Machine
fsm_instance: MappingMissionFSM = None


async def telemetry_broadcaster():
    """Worker care rulează pe fundal și face PUSH de date către WebSockets."""
    while True:
        try:
            if ws_manager.active_connections and fsm_instance:
                # Construim payload-ul exact ca la endpoint-urile REST
                payload = {
                    "status": {
                        "mission_id": getattr(fsm_instance, 'mission_id', 'N/A'),
                        "current_state": fsm_instance.state,
                        "battery_level": await fsm_instance.amr.get_battery_level(),
                        "pending_sync_records": await fsm_instance.storage.get_unsynced_count(),
                        "current_target": fsm_instance.current_target
                    },
                    "latest_data": fsm_instance.acquired_data if fsm_instance.acquired_data else None
                }
                await ws_manager.broadcast(payload)

        except Exception as e:
            logger.error(f"Eroare în WS Broadcaster: {e}")

        await asyncio.sleep(0.5)  # Facem Push la fiecare 500ms (2Hz) pentru fluență

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestionează ciclul de viață al aplicației FastAPI (Startup / Shutdown).
    Asigură inițializarea hardware-ului, a conexiunilor I/O și a mașinii de stări.
    """
    global fsm_instance
    logger.info("Inițializare Sistem Edge AMR Mapping...")

    # 1. Inițializare Interfețe Hardware (Mock-uri pentru dev/testare)
    amr = MockAMRController()
    therm = MockTemperatureSensor()
    hum = MockHumiditySensor()
    vis = MockVisionSystem()

    # 2. Inițializare Stocare (Edge SQLite) și Comms (MQTT Cloud)
    storage = EdgeStorage(db_path="data/amr_telemetry.sqlite")
    await storage._init_db()  # Creează tabelele dacă nu există

    # Folosim un broker public pentru teste
    mqtt = MQTTClient(broker="broker.hivemq.com", port=1883,storage_ref=storage)
    await mqtt.connect()

    # 3. Încărcare Grid de Misiune (În producție, acesta ar veni din WMS/ERP)
    mission_grid = [
        {"x": 10.5, "y": 5.0},
        {"x": 15.0, "y": 5.0},
        {"x": 15.0, "y": 12.5},
        {"x": 20.0, "y": 12.5}
    ]

    # 4. Instanțierea Mașinii de Stări (Core Business Logic)
    fsm_instance = MappingMissionFSM(
        amr=amr,
        therm=therm,
        hum=hum,
        vis=vis,
        storage=storage,
        mqtt_client=mqtt,
        grid=mission_grid
    )

    broadcaster_task = asyncio.create_task(telemetry_broadcaster())

    logger.info("Sistemul a fost inițializat cu succes. Web Server-ul este activ.")

    asyncio.create_task(fsm_instance.run_boot_sequence())

    # --- Punctul în care aplicația rulează și primește request-uri ---
    yield

    # --- Shutdown Logic (Apelat la SIGINT/SIGTERM) ---
    logger.info("Semnal de oprire primit. Executare Graceful Shutdown...")

    # Dacă misiunea rulează, o trecem în ERROR/IDLE pentru a opri buclele
    if fsm_instance and fsm_instance.state not in ['INIT', 'IDLE', 'ERROR']:
        await fsm_instance.to_ERROR()

    broadcaster_task.cancel()

    await mqtt.disconnect()
    await storage.close()
    logger.info("Resurse eliberate. Sistem oprit în siguranță.")


# Inițializare instanță FastAPI
app = FastAPI(
    title="DAFI AMR Mapping System",
    description="API Edge pentru orchestrarea misiunilor de mapare termică și de mediu.",
    version="1.0.0",
    lifespan=lifespan
)


# --- Configurare Dependency Injection ---
def override_get_fsm() -> MappingMissionFSM:
    """
    Această funcție înlocuiește `get_fsm` din routes.py.
    Permite endpoint-urilor să acceseze instanța reală (Singleton) a FSM-ului.
    """
    global fsm_instance
    if fsm_instance is None:
        raise RuntimeError("FSM nu a fost inițializat!")
    return fsm_instance



# Adaugă aceste importuri în api/server.py sau routes.py
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Request

# Configurarea directorului pentru pagini HTML
templates = Jinja2Templates(directory="templates")

# Endpoint-uri pentru a randa paginile (ACTUALIZATE)

@app.get("/login")
async def render_login(request: Request):
    # Folosim explicit request=... și name=... pentru a evita confuzia parametrilor
    return templates.TemplateResponse(request=request, name="login.html")

@app.get("/monitor")
async def render_monitor(request: Request):
    return templates.TemplateResponse(request=request, name="monitor.html")

@app.get("/control")
async def render_control(request: Request):
    return templates.TemplateResponse(request=request, name="control.html")
# Mapăm funcția din routes către instanța noastră globală
app.dependency_overrides[get_fsm] = override_get_fsm

# Înregistrăm rutele
app.include_router(router)

# Entry point pentru rulare directă din terminal
if __name__ == "__main__":
    # Rulăm serverul folosind Uvicorn (optimizat pentru asyncio)
    uvicorn.run(
        "api.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,  # Trebuie să fie False în producție
        log_level="info"
    )