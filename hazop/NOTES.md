# NOTES.md — Beslut och kontext

> Denna fil uppdateras automatiskt av Claude Code efter varje session.
> Den bevarar beslut, avvägningar och uppskjutna funktioner som inte framgår av koden eller git-historiken.

---

## Arkitekturella beslut

### Virtuell sidordning för P&ID-blad
**Beslut:** `pid_sheets`-tabellen mappar `display_order → physical_page`. Navigation i PIDPanel använder display-index och slår upp fysisk sida via `db.get_sheet_physical_page(display_n)`. Markörer (`cause_markers`, `consequence_markers`, `safeguard_markers`) lagrar alltid fysisk sida i `pid_page`-kolumnen och påverkas inte av omsortering.
**Varför:** Användaren vill kunna sortera om bladordningen utan att befintliga orsaksplaceringar tappas.
**Export-notering:** Sammanfogad PDF sparas i befintlig fil (in-place overwrite). Om programmet byggs ut med export måste man hämta sidorna via `get_sheets()` i rätt displayordning.



### Frekvens F=-1..5 ersätter S×L
**Beslut:** Riskvärdet är inte längre S×L (multiplikation) utan ett direkt matrisuppslag på (F, C) där F=frekvens (-1..5) och C=konsekvens (1..5).
**Varför:** S×L ger missvisande tal och är inte standard i norsk/europeisk processsäkerhet. Logaritmisk frekvensskala är mer intuitiv och konsistent med LOPA.
**Skala:** F=5 >1/år, F=4 = 1–10 år, F=3 ≈ 1/100 år, F=2 ≈ 1/1000 år, F=1 ≈ 1/10000 år, F=0 extremt sällan, F=-1 otänkbar.
**API:** `risk_info(frequency, consequence)` returnerar `(label, bg_color, fg_color)` — ingen score.

### FA / Antändning / Övriga faktorer — LOPA-reduktioner
**Beslut:** Varje konsekvens har utöver safeguard-RRF ytterligare tre reduktionskategorier:
1. **FA** (Frekvensavstängning, checkbox + RRF, default RRF=10): t.ex. operatörsingripande
2. **Antändning** (checkbox + RRF, default RRF=10): ignitionssannolikhet
3. **Övriga faktorer** (tabell: fritext + RRF per rad): eskalering, exponering etc.
**Formel:** `Slutkonsekvens_F = max(-1, F_orsak − floor(log10(RRF_safeguards × RRF_FA × RRF_ign × RRF_övriga)))`
**DB:** `consequences.fa_active`, `fa_rrf`, `ignition_active`, `ignition_rrf` + tabell `reduction_factors`.

### Kopiera trädobjekt med länkindikator
**Beslut:** Safeguards, Consequences och Causes kan kopieras via högerklick. Kopierade objekt får `source_id` satt till originalets id.
**Varför:** En PSV-101 kan vara samma fysiska ventil i flera scenarier. 🔗-ikonen i trädet varnar för att RRF-kredit kan inte tas dubbelt (IEC 61511 krav på oberoende).
**DB:** `source_id INTEGER DEFAULT NULL` på `causes`, `consequences`, `safeguards`.

### Likelihood på Cause, inte Consequence
**Beslut:** Sannolikhetsbedömningen (L) flyttades från `consequences`-tabellen till `causes`-tabellen.
**Varför:** En orsak har en inneboende sannolikhet oavsett vilken konsekvens den leder till. Konsekvensen bedöms enbart på allvarlighet (S).
**Migration:** Gamla `consequences.likelihood`-kolumnen finns kvar i DB men används inte längre.

### RRF på Safeguard reducerar likelihood
**Beslut:** RRF (Risk Reduction Factor) på en safeguard reducerar sannolikheten med `floor(log10(rrf))` steg.
**Skala:** RRF 10 = −1 steg, RRF 100 = −2 steg, RRF 1000 = −3 steg.
**Varför:** Följer IEC 61511 / SIL-konventionen där PFD ≈ 1/RRF.

### Riskmatris lagras som JSON i app_config
**Beslut:** Riskmatrisen (färger, etiketter, storlek, axelriktning) sparas som JSON under nyckeln `'risk_matrix'` i `app_config`-tabellen.
**Varför:** Flexibelt — användaren kan konfigurera valfri matrisstorlek (2×2 till 10×10) och färgsättning utan kodändring.

### Tvåfilsstruktur
**Beslut:** Koden är uppdelad i `hazop.py` (huvudfönster + DB + panels) och `pid_viewer.py` (P&ID-canvas + skanning).
**Varför:** P&ID-komponenten är stor och fristående nog för att motivera separation. Underlättar framtida utbyte av viewer-implementationen.

---

## Funktioner implementerade (kronologisk ordning)

| Funktion | Beskrivning |
|---|---|
| Grundläggande HAZOP-träd | Nod → Cause → Consequence → Safeguard-hierarki med SQLite-backend |
| P&ID-viewer | PDF-inläsning via PyMuPDF, zoom/pan, nodmarkering med polygon-ritning |
| Markörer på P&ID | Röda (cause), orange (consequence), gröna (safeguard) cirklar med taggar |
| Kopplingslinjer på P&ID | Röda linjer cause→consequence, gröna streckade consequence→safeguard |
| Högerklick-kontextmeny på P&ID | Meny med Hitta orsak / Konsekvens / Safeguard / Risk Scenario / Rita nodgräns |
| Risk Scenario-guide | 3-stegs wizard: Cause → Consequence → Safeguard med live riskförhandsvisning |
| Safeguards i trädet | SG_T=4, safeguards visas som löv under konsekvenser |
| Redigerbar bottenpanel | Ersatte grafisk ScenarioPanel med redigerbara textfält (EditableScenarioPanel) |
| Inställningar — riskmatris | Konfigurerbar N×M matris med klickbara färgceller |
| Inställningar — kategorier | Konsekvenskategorier (Person, Miljö, Ekonomi, etc.) redigerbara |
| Administrationsflik | Statistik + fullständig datatabell med riskfärger |
| Utrustningsflik | Persistent utrustningsregister med skanning, redigering och nodgenerering |
| Utrustningsskanning | Tre-pass: fulltext-regex + ord-för-ord + OCR (pytesseract/easyocr) |
| OCR-stöd | pytesseract (PSM 11+6 kombinerat) + easyocr som fallback, 4× renderingsskala |
| KNOWN_PREFIXES-katalog | ~90 P&ID-prefix med svenska namn och utrustningstyp (ISA 5.1-inspirerat) |
| Ny riskmatris F/C | Frekvensaxel F=-1..5 (7 nivåer), konsekvensnivå C=1..5, inget S×L — direkt matrisuppslag |
| 6-kolumn scenariotabell | Bottenpanelen ersatt: Nod→Orsak→Konsekvens→Risk före→Barriärer→Risk efter |
| FA/Antändning + Övriga faktorer | FA ☑ (RRF 10 default) och Antändning ☑ (RRF 10 default) per konsekvens. Övriga faktorer (fritext + RRF) i separat dialog. Slutkonsekvens = F efter alla reduktioner. |
| Editerbar worksheet | F och C redigerbara med combo i worksheettabellen, risknivå före/efter barriär |
| Kopiera i trädet | Högerklick → Kopiera/Klistra in för Cause, Consequence, Safeguard. 🔗-ikon för kopierade safeguards |
| NORSOK Z-013 / F-skala preset | Snabbknappar i riskmatrisinställningar fyller i frekvensaxelns etiketter och gränsvärden med ett klick |
| Konfigurerbara axelnamn i scenariotabell | ScenarioTablePanel visar konfigurerade axeletiketter (t.ex. AA, C3) istället för hårdkodade F=2 C=3 |
| Textfärg per riskkategori | Färgpalettens poster och matrisceller kan ha individuell textfärg; medium (gul) använder svart text som standard |
| PDF revisionshistorik + PID-hantering | Administration → Studiehantering med två flikar: Statistik + PID-hantering. PID-hantering har Revisioner (historik) + Blad (drag-to-reorder). Ny revision ersätter PDF, Nya blad sammanfogar via PyMuPDF. Markörer följer fysiska sidor oavsett visningsordning. |
| P&ID navigeringsprestanda | SVG-rendering ersatt med raster 3× scale som alltid användes som fallback. LRU-cache (10 sidor) i PIDGraphicsView eliminerar omrendering av besökta sidor. _PageRenderer (QThread) förrendar current±1 och current±2 sidor i bakgrunden. In-memory sheet_map i PIDPanel ersätter DB-fråga per sidnavigering. |
| Export P&ID med markup | "📤 Exportera PDF"-knapp i PIDPanel toolbar. Skapar ny PDF i visningsordning med nodgränser (färgade polygoner), C/K/S-markörer (fyllda cirklar med bokstav + etikett) och kopplingslinjer ritade direkt på sidan via PyMuPDF Shape API. |
| Avvikelsenivå i hierarkin | Ny nivå DEV_T=5 mellan Nod och Orsak: Nod → Avvikelse → Orsak → Konsekvens → Safeguard. 16 standardavvikelser (Lågt flöde, Högt tryck, etc.) + fri text. DeviationPanel med snabbknappar. Scenariotabell visar "Avvikelse"-kolumn med rad-merging. Migration skapar "Övrigt"-avvikelse automatiskt för befintliga orsaker. |

---

## Uppskjutna funktioner (ej implementerade)

### P&ID-symbolöverlagringar
**Vad:** Rita ut ISA 5.1-kompatibla vektorsymboler (ventilsymboler, pumpcirklar, etc.) ovanpå PDF:en vid identifierade tagg-positioner.
**Uppskattad tid:**
- Förenklat (geometriska former + färgkodning): ~4–6 timmar
- Fullt ISA 5.1-kompatibelt: ~15–20 timmar
**Status:** Sköts upp av användaren — prioritera annat först.
**Teknisk ansats när det görs:** Rita QPainterPath-symboler i `PIDGraphicsView.add_equipment_symbol()`, skala baserat på P&ID-ritningens koordinatsystem.

### Processutrustningsregister (P&ID Legend)
**Vad:** Inbyggt register med standardsymboler för ventiler, pumpar, kompressorer, filter, instrument etc. Kopplas till utrustningsskanningen.
**Status:** Sköts upp tillsammans med symbolöverlagringarna ovan.

---

## Kända begränsningar och tekniska skulder

- **OCR-positioner är approximativa** — x,y-koordinater från OCR stämmer inte perfekt med PDF-koordinater vid hög zoom. Markörer kan hamna något fel.
- **Likelihood-migration** — befintliga poster i `consequences.likelihood` används inte längre men rensas inte automatiskt. Påverkar inte funktionen.
- **Riskmatris-etiketter kopplade till comboboxar** — om användaren ändrar matrisstorlek i inställningar uppdateras inte automatiskt likelihood/severity-comboboxarnas texter i CausePanel/ConsequencePanel. De visar alltid 5 nivåer.
- **Skalning av P&ID-symboler** — när/om symbolöverlagringar implementeras behöver man hantera att varje P&ID har unik skala. Förslag: en gång per PDF låter användaren klicka på två kända punkter med känt avstånd.
- **EquipmentScanDialog nås fortfarande via PIDPanel** — den gamla scan-dialogen i pid_viewer.py lever kvar parallellt med den nya EquipmentPanel. Kan rensas bort om den inte används.

---

## Användarpreferenser

- Applikationen används i **Process Safety**-kontext (ProSa Process Safety Consulting AB).
- Gränssnittet är på **svenska**.
- Användaren föredrar att **fråga om tidsuppskattning** innan stora implementationer påbörjas.
- **Git-arbetsflöde:** Committa och pusha efter varje meningsfull förändring. GitHub-konto: `AntonGustavsson1`, repo: `hazop-tool`.

---

## Hur denna fil ska underhållas

Uppdatera denna fil när:
- Ett nytt arkitekturellt beslut fattas — lägg till under "Arkitekturella beslut"
- En funktion implementeras — lägg till i tabellen under "Funktioner implementerade"
- En funktion skjuts upp — lägg till under "Uppskjutna funktioner" med teknisk ansats
- En begränsning eller teknisk skuld identifieras — lägg till under "Kända begränsningar"
- Användaren uttrycker en preferens — lägg till under "Användarpreferenser"

Committa alltid NOTES.md tillsammans med kodfiler:
```
git add hazop.py pid_viewer.py NOTES.md
git commit -m "feat: ..."
git push
```
