# Kit de Mapare Termică și de Mediu (AMR-Pharma)

## Arhitectura Sistemului și Conformitatea DAFI
Acest sistem este proiectat pentru a rula la nivel de Edge (pe AMR), asigurând autonomia completă a robotului în medii farmaceutice izolate.

**1. Digitalization and Automation in Pharmaceutical Industry (DAFI):**
- **Data Integrity (ALCOA+):** Orice citire a senzorilor este imutabilă, fiind salvată instantaneu pe stocarea locală (Edge SQLite) sub formă de time-series înainte de a fi trimisă în Cloud.
- **Toleranță la Defecte (Offline-First):** Brokerul MQTT este tratat ca un serviciu volatil. Sistemul rulează un buffer local. Dacă rețeaua pică, datele sunt marcate "pending_sync" și misiunea continuă.
- **Trasabilitate:** Fiecare tranziție a mașinii de stări este logată. Fiecare punct de achiziție include metadate (nivel baterie, coordonate teoretice vs. reale, timestamp UTC).

**2. Asincronicitate și Decuplare:**
Sistemul folosește `asyncio` extensiv. Navigația AMR, citirea senzorilor (I2C, Camera) și publicarea rețelei funcționează independent. Mașina de stări orchestrează aceste procese fără a bloca CPU-ul.



pharma_amr_mapping/
├── api/
│   ├── __init__.py
│   ├── routes.py          # Endpoint-uri FastAPI (start/stop/status)
│   └── server.py          # Configurarea uvicorn/FastAPI
├── models/
│   ├── __init__.py
│   └── domain.py          # Pydantic models (Point, TelemetryData, Config)
├── services/
│   ├── __init__.py
│   ├── comms/
│   │   ├── amr_client.py  # Comunicare cu sistemul de navigație al robotului
│   │   └── mqtt_client.py # Client MQTT (Paho/Gmqtt) cu buffering offline
│   ├── hardware/
│   │   ├── base.py        # Clase abstracte (Interfețe Senzori)
│   │   └── mock_impl.py   # Implementări simulate (Mock)
│   └── storage/
│       └── edge_db.py     # SQLite/JSON handler cu queue pentru sync Cloud
├── state_machine/
│   ├── __init__.py
│   └── amr_fsm.py         # Implementarea asincronă a mașinii de stări
├── main.py                # Punctul de intrare (Bootstrap, DI container)
├── requirements.txt
└── README.md



Deployment
pyinstaller --name "AMR_Edge_Controller" --onedir --add-data "templates;templates" --collect-all "api" --collect-all "services" --collect-all "state_machine" --hidden-import "uvicorn.logging" --hidden-import "uvicorn.loops" --hidden-import "uvicorn.loops.auto" --hidden-import "uvicorn.protocols" --hidden-import "uvicorn.protocols.http" --hidden-import "uvicorn.protocols.http.auto" --hidden-import "uvicorn.protocols.websockets" --hidden-import "uvicorn.protocols.websockets.auto" --hidden-import "uvicorn.lifespan" --hidden-import "uvicorn.lifespan.on" --hidden-import "sqlite3" --hidden-import "paho.mqtt.client" main.py