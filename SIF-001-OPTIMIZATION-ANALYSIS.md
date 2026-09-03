# SIF-001 Optimeringsanalys
## Hybrit Pilot Plant - Verklig Data vs Programmodell

**Datum:** 2026-07-01  
**Status:** Detaljerad jämförelse för modelloptimering

---

## 1. HAZARD & SKYDDSFUNKTION

| Aspekt | Hybrit-rapport | Programmodell |
|--------|-----------------|---------------|
| **Hazard** | Lågt tryck i kvävestemmet (CO2-reningen) | "Pressure Relief - CO2 Cooler" |
| **Konsekvens** | CO2-backflöde till kvävenät → processtopp | Omvänd förklaring |
| **Skyddsfunktion** | Stänga två absorbatventiler (254SV559, 254SV561) | Relief-ventil |
| **PST (Process Safety Time)** | **30 sekunder** | Ej specificerats i program |
| **Responstid** | **10 sekunder** | Ej specificerats i program |

**IAKTTAGELSE:** Programmet har INTE implementerat PST eller responstidsdata. **KRITISK LÜCKA!**

---

## 2. ARKITEKTUR - MASSIV SKILLNAD

| Komponent | Hybrit-rapport | Programmodell | ❌ MISMATCH |
|-----------|-----------------|---------------|----|
| **Sensor** | **1oo1** (enkl) | 1oo1 | ✓ OK |
| **Logic Solver** | **1oo1** (ABB SIL3) | 1oo1 | ✓ OK |
| **Final Element** | **1oo2** (två ventiler i serie) | 1oo1 (enkl) | ❌ **HELT MOTSATT!** |

### Final Element Arkitektur - KRITISK SKILLNAD:

**Hybrit:** 254SV559 + 254SV561 **SER I SERIE** (båda måste stänga)
- HFT = 1 (Redundans)
- Alpha (CCF) = 10%
- SFF = 79.8% (väl diagnostiserad med PVST)
- Voting: 1oo2 (båda krävs för stängning)

**Program:** Enkl ventil (1oo1)
- Ingen redundans
- SFF = 0.10 (mycket låg)
- Ingen CCF-modellering

---

## 3. KOMPONENTDATA - MASSIV AVVIKELSE

### SENSOR

| Parameter | Hybrit | Program | Skillnad | Status |
|-----------|--------|---------|----------|--------|
| **λ_D [1/h]** | **3.27E-07** | **7.5E-07** | +129% högre | ❌ ÖVER |
| **λ_S (Safe)** | 5.21E-08 | Ej angett | - | ❌ SAKNAS |
| **λ_U (Undetected Safe)** | 6.85E-08 | Ej angett | - | ❌ SAKNAS |
| **SFF (Safe Failure Fraction)** | **73.4%** | **0.70 (70%)** | -3.4% lägre | ✓ NÄRA |
| **DC (Diagnostic Coverage)** | Väl diagnostiserad (via Process Monitoring) | 0.65 | - | ⚠️ OKAT VÄRDE |
| **MRT** | 24 timmar | 8 timmar | -67% | ❌ OPTIMISTISK |
| **Proof Test Interval** | **12 månader** | 8760 timmar (= 1 år) | - | ✓ OK |
| **Proof Test Coverage** | **99%** | 100% | - | ✓ NÄRA |
| **Modell** | Endress+Hauser Cerabar S PMP71/72/75 | "PT-101" (Generisk) | - | ❌ FALSKT |

**MODELL I PROGRAM:** Är bara en generisk placeholder. Bör vara **Endress+Hauser Cerabar S med PR Electronics 9106-isolering**.

**KRITISKA FEL:**
1. λ_D är 129% högre än verkligt värde (motsatt än vad som är optimalt)
2. MTTR är för låg (8h istället för 24h)
3. Modellnamn är felaktigt

---

### LOGIC SOLVER

| Parameter | Hybrit | Program | Skillnad | Status |
|-----------|--------|---------|----------|--------|
| **λ_D [1/h]** | **~1.46E-06** (totalt) | **2.5E-08** | **-98.3% LÄGRE!** | ❌ MYCKET OPTIMISTISK |
| **SFF** | **100.0%** | **0.99 (99%)** | - | ✓ NÄRA |
| **DC** | 100.0% (via SIL3-arkitektur) | 0.99 | - | ✓ NÄRA |
| **MRT** | 24 timmar | 8 timmar | -67% | ❌ OPTIMISTISK |
| **Proof Test Interval** | **12 månader** | 8760 timmar | - | ✓ OK |
| **Proof Test Coverage** | **99%** | 100% | - | ✓ NÄRA |
| **Arkitektur** | ABB AC800M High Integrity SIL3 | "SIS-PLC" (Generisk) | - | ❌ GENERISK |
| **HFT** | 1 (High) | Ej angett | - | ⚠️ SAKNAS |

**MODELL I PROGRAM:** Generisk "SIS-PLC". Bör vara **ABB AC800M High Integrity SIL3** med:
- PM865/TP830 Main Processor: λD = 1.46E-06 [1/h]
- SD821 Power Supply: λD = 1.53E-06 [1/h]
- AI880A Analog In: λD = 3.84E-07 [1/h]
- DO880 Digital Out: λD = 4.17E-07 [1/h]

**KRITISKA FEL:**
1. λ_D är **98.3% LÄGRE** än verkligt värde (mycket optimistisk)
2. MTTR är för låg (8h istället för 24h)
3. Modellnamn är generisk, inte verklig

---

### FINAL ELEMENT (VENTILER)

| Parameter | Hybrit | Program | Skillnad | Status |
|-----------|--------|---------|----------|--------|
| **Arkitektur** | **1oo2** (två ventiler) | **1oo1** (en ventil) | **180° motsatt!** | ❌ HELT FEL |
| **λ_D totalt [1/h]** | ~8.25E-05 (för 1oo2) | 8.0E-06 | -90.3% LÄGRE | ❌ MOTSATT |
| **Ventil 1 (Metso Norgren)** | λD = 2.28E-09 | - | - | ❌ EJ MODELLERAD |
| **Ventil 2 (Metso VPVL)** | λD = 4.25E-08 | - | - | ❌ EJ MODELLERAD |
| **Kontrollmodul (ABB DO880)** | λD = 4.07E-08 | Ingår i Logic | - | ⚠️ DELVIS |
| **SFF** | **79.8%** | **0.10 (10%)** | -89.8% LÄGRE | ❌ MASSIV SKILLNAD |
| **HFT** | 1 (Redundans) | 0 (Enkl) | - | ❌ SAKNAS |
| **Alpha (CCF)** | **10%** | Ej modellerad | - | ❌ SAKNAS |
| **PVST (Partiell Ventil Stroke Test)** | Ja, 85% täckning | Ej modellerad | - | ❌ SAKNAS |
| **MRT** | 24 timmar | 8 timmar | -67% | ❌ OPTIMISTISK |
| **Proof Test** | 12 mån, 85% täckning | 8760 timmar, 100% | - | ⚠️ PTC FÖR HÖGT |
| **Modeller** | Metso Norgren + VPVL + Metso 7000/9000 | "XV-101" (Generisk) | - | ❌ GENERISK |

**MODELLER I PROGRAM:** Generiska placeholders. Bör vara:
1. **254SV559:** Metso Norgren pilot-opererad solenoid
2. **254SV561:** Metso VPVL proportional ventil
3. **254HV562:** Tjeckventil (skydd mot backflöde)

**KRITISKA FEL:**
1. **Arkitektur helt motsatt** — 1oo1 istället för 1oo2
2. **SFF 10 gånger lägre** (0.10 istället för 0.80)
3. **Lambda_D låg** — saknar de två ventilernas individuella failure modes
4. **CCF ej modellerad** — 10% alpha-faktor saknas
5. **PVST ej modellerad** — ingen partiell slag-test täckning
6. **MTTR för låg**

---

## 4. TESTINTERVALL & PARAMETRAR

| Parameter | Hybrit | Program | Skillnad |
|-----------|--------|---------|----------|
| **TI (Proof Test Interval)** | 12 månader | 8760 timmar (= 1 år) | ✓ MATCH |
| **MTTR (Mean Restoration Time)** | **24 timmar** | **8 timmar** | ❌ -67% |
| **PTC (Proof Test Coverage - Sensor)** | **99%** | **100%** | ⚠️ För högt |
| **PTC (Proof Test Coverage - Logic)** | **99%** | **100%** | ⚠️ För högt |
| **PTC (Proof Test Coverage - FE)** | **85%** | **100%** | ❌ -15% verkligt |
| **Mission Time** | **15 år** | Ej angett (antar 175200h = 20år) | ⚠️ +33% längre |
| **Startup Time** | 24 timmar | Ej angett | ❌ SAKNAS |
| **Demand Rate** | 0.000E+00 per år (ingen spontan) | Ej angett | ❌ SAKNAS |

---

## 5. RESULTAT - MASSIV AVVIKELSE

| Metrik | Hybrit | Program | Skillnad | Status |
|--------|--------|---------|----------|--------|
| **PFD_sensor** | 1.18E-03 | 2.294e-03 | +94% HÖGRE | ❌ MOTSATT |
| **PFD_logic** | 2.01E-08 | 2.19e-06 | +10,800% HÖGRE | ❌ MASSIVT |
| **PFD_FE** | 8.25E-05 | 6.549e-02 | +79,400% HÖGRE | ❌ **KATASTROFALT** |
| **PFD_totalt** | **1.27E-03** | **6.76e-02** | **+5,220% HÖGRE!** | ❌ **HELT BORT** |
| **RRF** | **789.3** | ~14.8 | -98.1% LÄGRE | ❌ **HELT BORT** |
| **SIL uppnått** | **SIL 2** | **SIL 1** | -1 nivå | ❌ **UNDER KRAV** |
| **Uppfyller SIL 1 krav?** | **JA** (överuppfyller till SIL 2) | **JA** (precis på gränsen) | - | ⚠️ INGEN MARGINAL |
| **MTTFS** | **44.78 år** | ~1.4 år | -96.9% LÄGRE | ❌ **MASSIV** |

---

## 6. ROOT CAUSE ANALYSIS - VAD GÅR SNETT?

### Problem 1: Final Element Arkitektur
**Orsak:** Programmet använder 1oo1-arkitektur för alla komponenter. SIF-001 använder faktiskt **1oo2 för ventilerna**.

**Impact:** 
- PFD_FE blir ~100x högre än verkligt
- SFF sjunker från 79.8% till 10%
- Ingen redundans eller CCF-modellering

### Problem 2: Failure Rate Data
**Orsak:** Programmet använder **genomsnittliga industriell-automationsvärdena**, inte verklig SERH-data.

**Impact:**
- Sensor λD är för hög (motsatt än behövligt)
- Logic λD är mycket för låg (98.3% optimistisk)
- Final Element saknar individuella ventil-failure rates

### Problem 3: MTTR (Mean Restoration Time)
**Orsak:** Programmet antar 8 timmar. Hybrit-systemet använder 24 timmar för industri-relevant reparation.

**Impact:** PFD blir för låg (~3x lägre än verkligt)

### Problem 4: Proof Test Coverage
**Orsak:** Programmet antar 100% för alla. Final Element har bara 85% i Hybrit.

**Impact:** Final Element PFD blir för låg (~1.18x lägre än verkligt)

### Problem 5: Saknade Parametrar
**Orsaker:**
- PST (Process Safety Time) ej implementerat
- Responstid ej implementerat
- CCF (Common Cause Failure) alpha-faktor ej modellerad
- PVST (Partial Valve Stroke Test) ej implementerat
- Startup Time ej implementerat
- Demand Rate antagande ej dokumenterat

---

## 7. OPTIMERINGSPLAN

### OMEDELBAR (Phase 1)

1. **Uppdatera Final Element Arkitektur för SIF-001:**
   ```python
   "fe": {"name": "254SV559 + 254SV561 (Metso Ventiler)", 
          "arch": "1oo2",  # ÄNDRAT från 1oo1
          "lambda_d": ???,  # Behöver beräknas från två ventiler + control modul
          "dc": ???,        # Behöver uppdateras
          "beta": 0.02,
          "ti": 8760,
          "mttr": 24,       # ÄNDRAT från 8 till 24
          "ptc": 0.85,      # ÄNDRAT från 1.0 till 0.85
          "sff": 0.798,     # ÄNDRAT från 0.10 till 0.798
          "comp_type": "A"}
   ```

2. **Uppdatera MTTR för alla komponenter:** 8h → 24h

3. **Uppdatera Sensor-data:**
   ```python
   "sensor": {"name": "Endress+Hauser Cerabar S + PR Electronics 9106",
              "lambda_d": 3.27e-7,  # ÄNDRAT från 7.5e-7
              ...
              "mttr": 24}
   ```

4. **Uppdatera Logic-data:**
   ```python
   "logic": {"name": "ABB AC800M High Integrity SIL3",
             "lambda_d": 1.46e-6,  # ÄNDRAT från 2.5e-8
             ...
             "mttr": 24}
   ```

### MEDIUM TERM (Phase 2)

5. **Implementera CCF-modellering för 1oo2 Final Element:**
   - Alpha-faktor = 10%
   - Modifiera 1oo2 Markov-modell för att inkludera CCF

6. **Implementera PVST (Partial Valve Stroke Test):**
   - 85% täckning för Final Element
   - Modifiera DC-beräkning

7. **Lägg till PST-parametrar:**
   - Process Safety Time (30 sekunder för SIF-001)
   - Response Time (10 sekunder för SIF-001)

8. **Lägg till individuella ventil-failure rates:**
   - 254SV559 (Norgren): λD = 2.28E-09
   - 254SV561 (VPVL): λD = 4.25E-08
   - Kombinerad modellering för 1oo2

### LONG TERM (Phase 3)

9. **Implementera verklig SERH-databas:**
   - Ersätt generiska industriell-automations-värden
   - Lägg till FMEDA-data från tillverkare

10. **Modellera hela Hybrit-systemet:**
    - Alla 9 eller 74 SIF:ar från rapporten
    - Cross-check mot Hybrit-rapportens beräkningar

---

## 8. BERÄKNING AV KORREKT FINAL ELEMENT PFD

**Från Hybrit-rapport:**
- PFD_FE = 8.25E-05
- Arkitektur: 1oo2 (två ventiler i serie)
- Voting: Båda måste stänga

**Ventiler:**
1. Metso Norgren: λD = 2.28E-09 [1/h]
2. Metso VPVL: λD = 4.25E-08 [1/h]

**Kontrollmodul (ABB DO880):** λD = 4.07E-08 [1/h]

**För 1oo2 arkitektur (båda krävs):**
```
PFD_1oo2 = (λ1 + λ2)² × TI² / 3 + β × λDD × MTTR

Med β (CCF) = 0.10 (10% alpha-faktor för gemensam miljö)
```

**Vi behöver verifiera detta värde genom att implementera 1oo2-modell med CCF-faktor.**

---

## 9. NÄSTA STEG

1. **Läs in denna analys i calc.py** — förstå varför 1oo2 är kritisk
2. **Uppdatera run_sifs.py** — korrigera SIF-001 med verklig data från Hybrit
3. **Verifiera** — kör SIF-001 och jämför resultat mot Hybrit-rapport (målvärde: PFD = 1.27E-03, SIL 2)
4. **Dokumentera** — spara korrigeringslogiken i CLAUDE.md
5. **Iterera** — applicera samma process för SIF-002 → SIF-020

---

**STATUS:** Klara för optimering. Väntar på implementation.
