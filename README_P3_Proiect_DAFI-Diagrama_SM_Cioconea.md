# 📘 README – P3: Proiect DAFI - Diagram State Machines

**Disciplina:** Dezvoltarea Arhitecturilor de Fabricare Inteligente  
**Instituție:** POLITEHNICA București – FIIR  
**Student:** Cioconea Adina Mariana 
**Data:** 30 Aprilie 2026
---

## Scopul Etapei P3

Această etapă corespunde punctului **3. Dezvoltare proiect software** - slide 12 **DAFI - Specificatii proiect.pdf**.

**Trebuie să livrați un SCHELET COMPLET și FUNCȚIONAL al întregului Sistem Ciber-Fizic. 


##  Livrabile Obligatorii

### 1. Tabelul Nevoie Reală → Soluție CPS → Modul Software (max ½ pagină)
Completați in acest readme tabelul următor cu **minimum 2-3 rânduri** care leagă nevoia identificată în Etapa 1-2 cu modulele software pe care le construiți (metrici măsurabile obligatoriu):

| **Nevoie reală concretă** | **Cum o rezolvă CPS-ul vostru**                                                                                                                                                                        | **Modul software responsabil**                   |
|---------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------|
| Maparea termică sezonieră pe straturi adaptată volumului depozitului (până la 10m înălțime). | Achiziție autonomă de date termice/umiditate pe un grid 3D definit, cu eșantionare precisă la 10 minute și o densitate de minimum 28 de puncte/20 m³ pentru a capta stratificarea aerului pe axa Z.    | Mission Control + Data Logging Module (Hardware) |
| Identificarea Hot Spots și Cold Spots în condiții de stres sezonier (vârf de vară/iarnă). | Procesarea în Backend a seturilor de date pentru generarea hărților termice 3D și identificarea zonelor unde fluctuațiile depășesc ±2.0°C, calculând automat MKT prin ecuația Arrhenius.               | Data Analytics (Backend) + UI / Dashboard        |
|Dovada calibrării echipamentelor și validarea poziției senzorilor ficși (EMS) în raport cu datele mapării.| Verificarea digitală a certificatelor de calibrare în starea de Init și corelarea spațială a coordonatelor AMR cu pozițiile senzorilor ficși pentru a genera rapoarte de audit conform 21 CFR Part 11. | Data Analytics (Backend) + UI / Dashboard        |


**Instrucțiuni:**
- Fiți concreti (nu vagi): "detectare fisuri sudură" ✓, "îmbunătățire proces" ✗
- Specificați metrici măsurabile: "< 2 secunde", "> 95% acuratețe", "reducere 20%"
- Legați fiecare nevoie de modulele software pe care le dezvoltați

---


### 2. Diagrama State Machine a Întregului Sistem (OBLIGATORIE)

**Cerințe:**
- **Minimum 4-6 stări clare** cu tranziții între ele
- **Formate acceptate:** PNG/SVG, pptx, draw.io 
- **Legendă obligatorie:** 1-2 paragrafe în acest README: "De ce ați ales acest State Machine pentru nevoia voastră?"


#### Efectuare mapare pentru validare depozit farmaceutic( temperatura + umiditate + pozitie):
```
# AMR Environment Mapping State Machine

**Sistem Automatizat Cartografiere Termică și Monitorizare AMR:**

[ BOOT / START ]
               │
               ▼
           ┌───────┐
           │ INIT  │ (Autodiagnoză la pornire)
           └───────┘
               │
               │ boot_complete
               ▼
   ┌──────►┌───────┐      mission_complete (Grid gol)     ┌──────────────┐
   │       │ IDLE  ├─────────────────────────────────────►│ BACKEND_PROC │
   │       └───────┘◄─────────────────────────────────────┤ (Gen. Raport)│
   │           │                 report_generated         └──────────────┘
   │           │ start_mission 
   │           ▼               
   │     ┌─────────────┐       
   │     │ NAV_AND_POS │ (Transmite comanda de poziție)
   │     └─────────────┘       
   │           │               
   │           │ command_sent  
   │           ▼               
   │       ┌───────┐           
   │       │ IDLE  │ (Așteaptă deplasarea fizică a AMR)
   │       └───────┘           
   │           │               
   │           │ destination_reached 
   │           ▼               
   │     ┌─────────────┐       
   │     │ ACQUISITION │ (Poziționare și scanare pe Z-uri)
   │     └─────────────┘       
   │           │               
   │           │ data_acquired 
   │           ▼               
   │     ┌─────────────┐       
   │     │  DATA_MGMT  │ (Salvare locală EdgeDB & Sync MQTT)
   │     └─────────────┘       
   │           │               
   └───────────┴──────────────────────────────────────────────┘

════════════════════════════════════════════════════════════════════
STĂRI DE SIGURANȚĂ ȘI EXCEPȚII:

 [ ORICE STARE ] ──────── trigger_handover ────────► [ HANDOVER ] ───► Reluare în IDLE
                        (Baterie AMR < 20%)                         (fleet_takeover)

 [ ORICE STARE ] ────────── trigger_error ─────────► [ ERROR ] ──────► Reset în IDLE
                        (Oprire de urgență)                         (reset_error)
```


**Legendă obligatorie (scrieți în README):**
```markdown
### Justificarea State Machine-ului ales:

Am ales arhitectura de monitorizare continuă cu procesare la Edge și sincronizare asincronă pentru că proiectul nostru 
necesită autonomie decizională la nivelul robotului (pentru a continua misiunea chiar și fără semnal Wi-Fi) și o 
gestionare critică a resurselor de energie pentru acoperirea completă a grid-ului de monitorizare.

Stările principale sunt:

1. [SYSTEM_READY / INIT]: Se realizează un check complet de tip "pre-flight": validarea conexiunii la rețea, 
verificarea montării mediului de stocare Edge, autocalibrarea senzorilor de mediu și a camerei termice.

2. [IDLE / STANDBY]: Stare critică adăugată pentru sincronizarea cu platforma mobilă. Kit-ul rămâne în așteptarea 
trigger-ului de la AMR. Sistemul nu consumă resurse de achiziție până nu primește confirmarea că robotul este în poziție.

3. [NAV_AND_POSITIONING]: Robotul transmite coordonatele XY țintă. Kit-ul recepționează datele și efectuează 
calibrarea/orientarea camerei termice și a senzorilor pe axa verticală pentru a viza exact punctul de măsură.

4. [DATA_ACQUISITION]: Captarea simultană a datelor brute (imagine termică, umiditate, temperatură ambientală) 
urmată de corelarea acestora cu metadatele de localizare și timestamp pentru precizie spațială.

5. [DATA_MGMT]: Datele sunt securizate local (redundanță) și transmise prin MQTT către broker. 
Dacă Wi-Fi-ul este instabil, datele rămân în coada de așteptare locală până la restabilirea link-ului.

6. [HANDOVER]: Monitorizarea activă a bateriei. Dacă scade sub 20%, kit-ul salvează stadiul curent al 
grid-ului și inițiază procedura de handover, solicitând un alt AMR echipat cu un kit similar pentru a prelua sarcina de la ultima coordonată.

8. [BACKEND_PROCESSING]: Etapă asincronă în cloud unde datele agregate din întreaga sesiune (sau de la mai mulți roboți) 
sunt procesate pentru generarea hărții termice finale, rapoartelor de conformitate și identificarea hotspot-urilor.

9. [ERROR]: Stare de siguranță activată la orice defecțiune (blocaj gimbal, eroare senzor I2C). Oprește misiunea kit-ului,
 salvează log-urile de eroare și notifică sistemul central pentru mentenanță.

Tranzițiile critice sunt:
* [INIT] → [IDLE] (boot_complete): Trecerea automată după succesul autotestării senzorilor hardware.
* [IDLE] → [NAV_AND_POS] (start_mission): Se verifică bateria și se extrage următoarea coordonată X, Y din planul de misiune (mission_grid).
* [NAV_AND_POS] → [IDLE] (command_sent): Kit-ul scrie pe magistrală/API comanda pentru motoarele AMR și eliberează execuția, revenind în standby pentru a decupla deplasarea de procesul de achiziție.
* [IDLE] → [ACQUISITION] (destination_reached): Declanșată de primirea semnalului asincron de "Position Reached" de la AMR. Abia acum kit-ul pornește secvența hardware de scanare.
* [ACQUISITION] → [DATA_MGMT] (data_acquired): Senzorul finalizează citirile pe axa verticală, face media matricilor termice și generează pachetul final de date.
* [DATA_MGMT] → [IDLE] (data_saved): După salvarea în EdgeDB și transmiterea pachetului MQTT, kit-ul revine în așteptare pentru a prelua următorul punct din grid.
* [IDLE] → [BACKEND_PROC] (mission_complete): Când coada de coordonate este goală (toate punctele au fost mapate), se declanșează procesarea finală a rapoartelor agregate.
* [BACKEND_PROC] → [IDLE] (report_generated): Raportul de conformitate este salvat, iar AMR-ul este gata pentru o nouă misiune.
* [* Orice Stare] → [HANDOVER] (trigger_handover): Bateria AMR scade sub pragul critic. Misiunea curentă se suspendă pentru a proteja integritatea datelor.
* [HANDOVER] → [IDLE] (fleet_takeover): Un robot cu baterie plină preia grid-ul rămas de la robotul descărcat și continuă fluxul de măsurare.
* [* Orice Stare] → [ERROR] (trigger_error): Trecere de urgență în caz de anomalie hardware/software sau apăsarea butonului E-STOP.
* [ERROR] → [IDLE] (reset_error): Deblocare manuală de către operator din interfața de control după remedierea problemei.

Starea ERROR este esențială pentru că într-un mediu industrial dinamic, senzorii se pot decalibra din cauza vibrațiilor
sau robotul poate întâmpina obstacole neprevăzute; sistemul trebuie să garanteze integritatea datelor colectate până la
acel punct și să asigure continuitatea măsurătorilor prin restul flotei.
Bucla de feedback funcționează astfel: rezultatul inferenței de la nivelul Edge (ex: detecția unei zone cu temperatură 
extremă în timp real) poate forța robotul să crească densitatea punctelor de măsurare în acea zonă specifică înainte de
a finaliza grid-ul standard.
```

---

## Checklist Final – Bifați Totul Înainte de Predare

### Documentație și Structură
- [X] Tabelul Nevoie → Soluție → Modul complet (minimum 2 rânduri cu exemple concrete completate in acest fisier readme)
- [X] Diagrama State Machine creată și salvată și postată alături de acest readme pe moodle la P3. State Machine pentru proiectul DAFI
- [X] Legendă State Machine scrisă în acest readme (minimum 1-2 paragrafe cu justificare) 

