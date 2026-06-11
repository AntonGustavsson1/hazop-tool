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
| Standardorsaker mallbibliotek | Ny DB-tabell standard_deviations + standard_causes. Seedad med 16 avvikelser och typiska orsaker per avvikelse. Redigerbar via Inställningar → "Standardorsaker"-flik (lägg till/ta bort/sortera avvikelser och orsaker). add_node() seedar deviations från standard_deviations-tabellen. |
| Lägg till orsaker på P&ID | Högerklick på avvikelse i trädet → "📍 Lägg till orsaker på P&ID". Byter till P&ID-vy och aktiverar MODE_CAUSE_TEMPLATE=6. Per klick på P&ID visas TemplateCausePickerDialog med standardorsaker för avvikelsen (+ fritext). Orsak skapas i DB, markör placeras, träd/scenarioproanelen uppdateras. |
| Komponentbaserade standardorsaker | `standard_causes`-tabellen har fått kolumnen `comp_type TEXT DEFAULT ''`. ~200 komponentspecifika orsaker seedade via `_COMP_STD_CAUSES` / `_seed_component_causes()` (sentinel `comp_causes_seeded_v1` i app_config). TemplateCausePickerDialog filtrerar orsakslistan dynamiskt när användaren väljer komponenttyp: visas orsaker med matchande comp_type + generiska (comp_type=''). För "Instrument / Sensor" visas ett extra avsnitt "Sekundär verkan" med radioknapp-lista (Pump stoppar, Reglerventil stänger, etc.) + fritext + valfri sekundär komponent-ID. Kombinerad beskrivning: "Signalfel högt → Pump stoppar (P-101)". Inställningar → Standardorsaker visar komponentspecifika orsaker med [Komponenttyp]-prefix i blå text. |
| Korsavvikelsereferens (pre-dialog) | Innan P&ID-läget startas visas `ReuseDeviationCausesDialog`. Orsaker från ANDRA avvikelser i samma nod listas per avvikelse med hierarkiska referensnummer (t.ex. "1.2.3" = nod.avvikelse.orsak). Varje orsak har toggle-knappar "Referera" och "Invers". Varje avvikelserubrik har "Referera avvikelse" och "Invers avvikelse" för generisk referens. Invers-knapp inaktiveras (grå, tooltip "Ingen invers hittades") när ingen substitution finns. Valda orsaker kopieras till DB före P&ID-läget; om ursprungsorsaken har P&ID-markörer kopieras även dessa automatiskt till den nya orsaken. DB: `causes_for_node_excluding_deviation`, `cause_markers_for_cause`. |
| Inversionsord utökade | `_INVERSION_MAP` utökad med: stopp↔start, stängt↔öppet, öppnat→stängt, stängning↔öppning, closed↔open. Regex sorteras efter nyckel-längd fallande så "stoppar" matchar före prefix "stopp". `invert_cause_text()` returnerar oförändrad text om ingen substitution hittas; dialogen visar då inaktiverad Invers-knapp. |
| Sekvensnummer i trädet | Varje trädobjekt visar sitt eget positionsnummer (t.ex. "1. Nod Alpha", "2. Högt flöde", "3. Pump stopp"). Enumerate används på alla nivåer: nod, avvikelse, orsak, konsekvens, safeguard. |
| Avvikelseemoji ändrad | Avvikelser i trädet använder nu ⬡ (hexagon) istället för ⚠ (varningstriangel). |
| Kategoribaserad konsekvensbedömning | 📊-knapp på KON-cellen öppnar matris-popup där användaren sätter konsekvensnivå (K1–K5) per konsekvenskategori. Varje vald kategori genererar EN rad i scenariotabellen (inte en rad per barriär). KON-cellen visar alltid textbeskrivningen; kategoribadgen visar "Per K3" etc. DB: `consequence_severities (id, consequence_id, category_id, severity)`. |
| RRF-knapp per kategorirad | Barriär-cellen för kategoriraderna visar "RRF×n/tot: rrf"-knapp. Klick öppnar `CatSGSelectionPopup` där alla barriärer är ikryssade som default; avmarkering = "gäller ej" för den kategorin. DB: `consequence_severity_exclusions (severity_id, safeguard_id)`. |
| Dubbelriktad RFORE ↔ kategorimatris | Risk-före-barriär-cellen för kategoriraderna lagrar `risk_click_cat`-metadata. Klick öppnar riskmatrisen och uppdaterar `consequence_severities.severity` (inte `consequences.severity`). Speglar också kategorimatrisens val. |
| Redesign av kategoriraderna | En rad per safeguard (inte per kategori×safeguard). Rad i har sgs[i] som SG och cat_rows[i] som kategoribadge. `n_rows = max(n_cats, n_sgs, 1)`. Gul cirkel på RRF-brickan markerar safeguards uteslutna ur minst en kategori. |
| RRF-popup med kategorikoppling | `SgRRFCategoryPopup` ersätter `CatSGSelectionPopup`. Visar typval (BPCS/SIS/Mekanisk/Administrativ/Övrigt), fritt RRF-belopp (SpinBox + preset-knappar 1/10/100/1000/10000) och checkbox per kategori "Gäller ej för [Kategori]". |
| Risk-cellernas etikett förenklat | RFORE/REFT/SLUT visar inte längre riskklassens textlabel (t.ex. "Mellan") utan bara axlarna ("D1  K3", "−2 steg\nD1  K3"). |
| P&ID real-time update | `_on_scenario_item_edited` anropar `reload_overlays()` så P&ID-markörer uppdateras direkt när orsak/konsekvens/safeguard-text redigeras. `_switch_view` anropar `reload_overlays()` vid byte till P&ID-flik. |
| Kedjad orsak från konsekvens (⛓) | ⛓-ikon i höger kant av KON-cellen i scenariotabellen. Klick öppnar `CauseObjectPopup` för att ange tag, typ och orsaksbeskrivning. Ny orsak skapas under samma avvikelse som förälderorsaken och länkas via `causes.linked_consequence_id`. Orsaker med länk visas med ⛓-emoji i trädet. F-värdet visas nummeriskt som en färgad F-badge i ORS-cellen (efter obj-zonen, 50px bredd). DB: `causes.linked_consequence_id INTEGER DEFAULT NULL`, `safeguard_cause_exclusions (safeguard_id, cause_id PK)`. När en kedjad orsak finns: KON-cellens ⛓-zon byter från ljusgrön till mörkgrå; den kedjade orsaken visar konsekvensen den är länkad från (splittad orsaksrad i tabellen); ORS-cellen på den kedjade orsaken får lila bakgrund + litet ⛓-märke i obj-zonens hörn. Chain-länkade orsakers SG-kolumn visas ej (deduplicerat). RRF-popup har sektion "Gäller ej för orsak" med checkbox per orsak (⚙/⛓-prefix). Frekvens-badge visar numeriskt värde "0.05/år" om base_freq finns, annars "F3". |
| 🔴 Redmarkup per nod | Ny markuptyp separat från nodavvikelser. Högerklick på nod i trädet → "🔴 Editera redmarkup". Inkluderar samma ritverktyg som nodmarkup (select, polygon, polyline, smart, kommentar) men utan "Lägg ut nodnamn"-knapp. Alla former är heldragna (opaque_fill=True) med röd standardfärg (#CC0000, opacity=1.0). Extra verktyg: 25 inline SVG P&ID-symboler i 3 kategorier (Ventiler 13st, Kärl 5st, Utrustning 7st) åtkomliga via symbol-knapp + popup. Symboler kan justeras i bredd, höjd och rotation via högerklick → "Ändra storlek/rotation...". DB: ny tabell `node_red_markups` med kolumner för type, points, label, color, opacity, line_width, symbol_w, symbol_h, symbol_rot. UI: `RedMarkupPanel` (vänster ribbon, röd ton), `RedMarkupTablePanel` (nertill, röd ton), `_SymbolSelectorPopup` (flottande flikpopup), `_SymbolDimsDialog`. |
| Resize/rotate handles för symboler | Hörnhandtag (NW/NE/SW/SE, orange) och rotationshandtag (lila, med linje) på valda P&ID-symboler i MODE_MARKUP_SELECT. Resize håller centerpunkten fix; rotation via atan2+90°. Live-preview under drag via streckad orange bbox. Ny signal `markup_symbol_dims_changed(mu_id, w, h, rot)` sparar till DB och re-renderar. Tre DATA-nycklar: _DATA_SYMBOL_W=6, _DATA_SYMBOL_H=7, _DATA_SYMBOL_ROT=8. |
| Study board — alla sidor synliga | P&ID-vyn visar nu alla PDF-sidor sida vid sida (vänster→höger, 30px gap) istället för en i taget. Alla HAZOP-overlays (nodgränser, orsaker, konsekvenser, safeguards, redmarkup) laddas för alla sidor vid start. `pdf_to_scene(x, y, page=None)` och `scene_to_pdf(pt)` är page-offset-medvetna; `_hit_test_page(scene_pt)` detekterar vilken sida en punkt tillhör. "📐 Layout"-knapp aktiverar MODE_BOARD_LAYOUT=14 där sidorna kan dras fritt. Layout sparas som JSON i `pid_config` (nyckel `board_layout`) och återladdas vid start. |
| Generisk connectoranalys (validerad mot ref-bibliotek) | `_parse_connector` söker dialektens ritningsnummermönster FÖRST; TILL/FRÅN/TO/FROM används bara för riktning och bara när nyckelordet står först i texten eller direkt intill referensen ("TO S0000162", "258-0000-001-PS TILL FACKLA"). Mitt-i-texten-nyckelord ("KVÄVE TILL ELFILTER" = utrustning, inte blad) ignoreras → kantkonvention avgör (vänster=in, höger=ut). `_find_in_zones` tilldelar varje connector sin NÄRMASTE kant (inte zontillhörighet) och dedupliceras på (ref_sheet, position) över alla pass — eliminerade 122 falska topp/unknown-dubbletter i LKAB-biblioteket (hörn-connectors hittades i två zoner). Riktningslösa referenser inne i titelområdet (ritningsreferenslistan) filtreras bort. |
| Flöden matchas från BÅDA ändar | `_match_connections` använder nu både ut- och in-connectors: flödet A→B dokumenteras som OUT på A (ref→B) och IN på B (ref→A) — bägge skapar samma koppling. Missar extraktionen ena änden finns kopplingen ändå; hittas båda höjs confidence (+0.08). LKAB-validering: 254 kopplingar, 190 bekräftade från båda ändar, 74 äkta dubbelriktade, 49 ghost (SAFE LOCATION/ATM/SCRUBBER etc.). `resolve_page` använder `_sheet_ref_variants`. Gammalt suffix-hack i `run()` som mappade alla '0000-001'-suffix till första sidan är borttaget. |
| Ruttade kopplingslinjer | `add_sheet_conn_arc` ritar inte längre en enkel bezier rakt över brädet: korta kantstubbar (`max(70, min(260, chord*0.18))` istf 38 % av kordan) ger brantare svängar, och mittsträckan ruttas runt andra blads rektanglar via girig rekursiv detour (Liang-Barsky-segmenttest `_seg_rect_entry`, omväg via närmsta fria sida, djup ≤ 8) med rundade hörn (`_rounded_path`, quad-bezier radie 130). Blir omvägen > 3× kordan + 1200 px faller den tillbaka till direkt kurva (användaren accepterar att vissa fall är omöjliga). Parallella bågar separeras via `wiggle`-offset i detour-koordinaten. Validerat på LKAB-brädet: 385 → 4 sidkorsningar (251/254 linjer helt rena), ruttning av alla 254 tar 0,75 s. |
| Page-LOD på study board | Brädsidor renderas vid `_LOW_SCALE=0.5` (≈4 MB/A1-blad istf 144 MB vid 3×) och skalas upp ×6 så scenavtryck och alla sparade koordinater är oförändrade (render_scale förblir 3.0). Vid zoom > ~0.21 byts de ≤6 sidor närmast viewport-centrum till fullupplösta 3×-pixmaps via `_PageRenderer` i bakgrunden (`_update_page_lod`, debounce-QTimer 150 ms triggad av wheelEvent/scrollContentsBy/goto_page/fitInView/navigate_to_marker); vid utzoomning/scroll degraderas de tillbaka. 3×-cachen (`_page_cache`, LRU 10) återanvänds som hi-res-lager. Utzoomad panorering ritar bara små pixmaps → snabbt oavsett antal blad. |
| Smart layout — lagerbaserad processflödeslayout | `_propose_layout` är nu Sugiyama-stil istället för kraftbaserad: (1) cykelbrytning via greedy feedback-arc-set (Eades) så returledningar kapas — inte huvudflödet (bladnummer som tie-break eftersom numreringen följer processordningen); (2) longest-path-lager → kolumn per blad, flöde vänster→höger som P&ID-konventionen; (3) barycenter-svep (median) för radordning; (4) justeringspass som linjerar kopplade blad horisontellt; (5) serpentinvik för mycket långa kedjor; (6) "utility hubs" (fackla/avgas/effluent — blad med grad ≥ max(7, 10% av antal)) parkeras i egen rad längst ner så deras linjefans inte korsar flödet; (7) isolerade blad i rutnät underst. Vertikala kanter (topp/botten-connectors) får dela kolumn och staplas. Dev-verktyg: `analyze_refs.py` (kopplingsstatistik + ASCII-karta) och `render_layout.py` (PNG-förhandsvisning) mot `P&ID ref/`-biblioteken. |

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
