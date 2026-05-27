# 🛡️ Kit de Mapare Termică și de Mediu (AMR-Pharma)

Acest proiect reprezintă un Sistem Ciber-Fizic (CPS) proiectat pentru a rula la nivel de Edge pe un Robot Mobil Autonom (AMR). Scopul principal este realizarea cartografierii termice și de mediu (3D) în depozitele farmaceutice, asigurând trasabilitatea datelor și conformitatea cu standardele industriale.

---

## 🌟 Funcționalități Principale

* **Profilare Termică 3D:** Achiziție autonomă de date la multiple niveluri de înălțime (ex: 0.2m, 1.0m, 1.8m) pentru a detecta stratificarea aerului.
* **Arhitectură Offline-First (Edge):** Toate datele sunt salvate inițial într-o bază de date locală SQLite (`EdgeDB`). Dacă rețeaua pică, misiunea continuă neîntrerupt, iar datele sunt sincronizate ulterior.
* **Sincronizare Cloud via MQTT:** Pachetele de telemetrie sunt trimise asincron către un broker MQTT și preluate de un serviciu backend dedicat (`CloudDB`).
* **Swarm Handover (Toleranță la Defecte):** Dacă bateria robotului scade sub 20%, FSM-ul salvează stadiul grilei și inițiază un protocol de transfer către un alt robot din flotă, reluând misiunea automat.
* **Dashboard CPS în Timp Real:** Interfață web (Plotly.js + Bootstrap) pentru monitorizarea în timp real a FSM-ului, nivelului bateriei și generarea „covoarelor” termice 3D.
* **Istoric Misiuni:** Posibilitatea de a reîncărca din baza de date și a vizualiza în 3D misiunile trecute.

---

## 🏗️ Arhitectura Sistemului și Conformitatea DAFI

Sistemul este împărțit în două componente majore care rulează independent:

1.  **Edge Node (Pe Robot):**
    * Rulează FSM-ul (Mașina de Stări).
    * Interoghează senzorii (simulați în `services/hardware/mock_impl.py`).
    * Gestionează `EdgeDB` (Baza de date locală) și coada de publicare MQTT.
    * Expune API-ul REST și serverul WebSocket pentru interfața Web.
2.  **Cloud Backend:**
    * Un script ascultător (`cloud_backend.py`) care consumă datele de pe brokerul MQTT.
    * Stochează o copie a datelor în `CloudDB` pentru audit și generare de rapoarte.

**Conformitate DAFI & ALCOA+:**
Orice citire a senzorilor este imutabilă, fiind salvată instantaneu cu un `timestamp_utc` și un `mission_id` unic. Sistemul nu blochează execuția (folosind intens `asyncio`) pentru a garanta timpii de reacție ai robotului.

---

## 📂 Structura Proiectului

```text
pharma_amr_mapping/
├── api/
│   ├── routes.py          # Endpoint-uri REST și WebSockets
│   ├── server.py          # Configurarea FastAPI și Lifecycle (Startup/Shutdown)
│   └── data/              # Folder pentru baza de date Edge (amr_telemetry.sqlite)
├── models/
│   └── domain.py          # Modele de date Pydantic (Validare și Structură)
├── services/
│   ├── comms/             # MQTT Client și interfața AMR (Navigație)
│   ├── hardware/          # Mock-uri pentru Senzori I2C și Cameră (AMR, Temp, Umiditate)
│   └── storage/
│       ├── edge_db.py       # Interacțiunea cu SQLite-ul local (Async)
│       └── cloud_backend.py # Serviciul de Cloud care consumă mesajele MQTT
├── state_machine/
│   └── amr_fsm.py         # Creierul sistemului: Mașina cu stări finite
├── templates/
│   ├── login.html         # Pagină de autentificare operator (Mock)
│   └── monitor.html       # Dashboard-ul principal 3D
├── main.py                # Punctul de intrare pentru nodul Edge
└── requirements.txt       # Dependințe Python

```
## 🚀 Setup (Instalarea Mediului)

Proiectul este scris în Python și folosește `FastAPI`, `SQLite` (prin `aiosqlite`) și `Paho-MQTT`.

**1. Clonarea proiectului și navigarea în director:**

```bash
git clone https://github.com/acioconea/amr_mapping_kit.git
cd amr_mapping_kit
```

**2. Crearea ,activarea unui mediu virtual și instalarea librariilor**

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```
# ⚙️ Rulare (Pornirea Ecosistemului)

Pentru a simula corect fluxul complet (**Robot Edge → Broker MQTT → Cloud Backend**), sistemul trebuie rulat în două terminale separate (ambele cu mediul virtual activat).

---

## Terminalul 1: Nodul Edge (Robotul AMR)

Acesta pornește Mașina de Stări (FSM), interfața Web, API-ul și conexiunea hardware-ului simulat.

```bash
python main.py --env production
```

### Acces Dashboard Operator

Deschideți în browser:

```text
http://127.0.0.1:8000/login
```

- ID Operator: minim 3 caractere
- PIN: `1234`

---

## Terminalul 2: Cloud Backend (Receptorul MQTT)

Acesta ascultă traficul MQTT și descarcă datele trimise de robot într-o bază de date globală.

```bash
python -m services.storage.cloud_backend
```

---

# 🗄️ Observarea Bazelor de Date (Edge vs. Cloud)

Sistemul implementează 2 baze de date SQLite separate, stocate automat în folderul `data/` la prima rulare:

---

## `data/amr_telemetry.sqlite` (EdgeDB)

Este baza de date aflată fizic pe robot.

### Rol
- Acționează ca un buffer (**Offline-First**)

### Ce să observi
- Rândurile noi primesc inițial:
  ```text
  is_synced = 0
  ```

- După ce worker-ul MQTT confirmă trimiterea lor către broker:
  - `is_synced` devine `1`
  - se completează câmpul `synced_at`

- Aici se stochează și lista misiunilor pentru vizualizarea istoricului pe Dashboard.

---

## `data/cloud_database.sqlite` (CloudDB)

Este baza de date centrală a depozitului (simulată prin al doilea terminal).

### Ce să observi
- Conține tabelul global:

  ```text
  amr_global_telemetry
  ```

- Datele apar aici aproape instantaneu după ce nodul Edge le publică.
- Înregistrează identificatorul `mission_id` pentru trasabilitate completă pe flote de roboți.

---

> **Notă:** Pentru a inspecta fișierele `.sqlite` direct, recomandăm instalarea programului gratuit **DB Browser for SQLite**.

---

# 📡 API Endpoints Principale

Sistemul expune o serie de rute RESTful și un tunel WebSocket, definite în `api/routes.py`.

---

## Control FSM (Comenzi)

### Pornire misiune

```http
POST /api/v1/mission/start
```

Preia parametrii grilei (**Lățime, Lungime, Pas**) și pornește achiziția.

---

### Oprire de urgență (E-STOP)

```http
POST /api/v1/mission/stop
```

Oprește motoarele și FSM-ul.

---

### Delegare misiune

```http
POST /api/v1/mission/handover
```

Declanșează manual delegarea misiunii către alt robot.

---

### Resetare stare eroare

```http
POST /api/v1/mission/reset
```

Scoate robotul din starea de eroare.

---

# 📊 Telemetrie și Date

### Status misiune

```http
GET /api/v1/mission/status
```

Returnează:
- starea FSM
- nivelul bateriei
- targetul curent
- pachetele nesincronizate

---

### Istoric misiuni

```http
GET /api/v1/mission/history
```

Returnează lista (rezumatul) tuturor misiunilor finalizate și salvate local.

---

### Descărcare misiune completă

```http
GET /api/v1/mission/history/{mission_id}
```

Descarcă toate punctele:
- coordonate
- temperaturi 3D

ale unei misiuni vechi.

---

# 🔄 Flux în Timp Real

### WebSocket Telemetry

```http
WS /api/v1/mission/ws/telemetry
```

Tunel WebSocket prin care serverul face PUSH (**2Hz**) către Dashboard cu:
- statusul sistemului
- noile scanări verticale

