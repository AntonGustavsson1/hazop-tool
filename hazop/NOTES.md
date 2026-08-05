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
| Konsekvensgraf med beroende kolumner | `_STD_CONSEQUENCE_STEPS` (platta listor per steg) ersatt av `_CONSEQ_NODES` — en riktad graf med 62 noder där varje nod har `text` + `next` (logiska eskaleringssteg). Del2-alternativen beror på valt Del1, Del3 på Del2 osv. Grafen följer event tree-analys (CCPS/DNV): LOC → jetbrand/pölbrand (direktantändning) \| flash fire/VCE (fördröjd) \| toxisk dispersion/miljö (ingen antändning); brandpåverkan → BLEVE/domino; runaway → snabb tryckstegring → kärlbrott. Allt omitigerat (inga PSV/larm — de är barriärer). `_CONSEQ_ENTRY` ger Del1-ingångar per avvikelse (15 st); `_CONSEQ_GENERIC_NEXT` är fallback efter fritext. Dialogens `_cascade_from()` återpopulerar nedströms kolumner vid varje val; `consequence_steps.node_key` (ny kolumn) sparar grafnoden så beroendekedjan återställs vid omöppning. `[objekt]` → ref-tag, eller "objektet" när tag saknas (läsbara prepositioner). Buggfix: `_migrate()` kör kolumnmigreringarna både före och efter `executescript` — färska databaser kraschade annars på `standard_causes.comp_type` vid seedning. |
| Ruttade kopplingslinjer | `add_sheet_conn_arc` ritar inte längre en enkel bezier rakt över brädet: korta kantstubbar (`max(70, min(260, chord*0.18))` istf 38 % av kordan) ger brantare svängar, och mittsträckan ruttas runt andra blads rektanglar via girig rekursiv detour (Liang-Barsky-segmenttest `_seg_rect_entry`, omväg via närmsta fria sida, djup ≤ 8) med rundade hörn (`_rounded_path`, quad-bezier radie 130). Blir omvägen > 3× kordan + 1200 px faller den tillbaka till direkt kurva (användaren accepterar att vissa fall är omöjliga). Parallella bågar separeras via `wiggle`-offset i detour-koordinaten. Validerat på LKAB-brädet: 385 → 4 sidkorsningar (251/254 linjer helt rena), ruttning av alla 254 tar 0,75 s. |
| Page-LOD på study board | Brädsidor renderas vid `_LOW_SCALE=0.5` (≈4 MB/A1-blad istf 144 MB vid 3×) och skalas upp ×6 så scenavtryck och alla sparade koordinater är oförändrade (render_scale förblir 3.0). Vid zoom > ~0.21 byts de ≤6 sidor närmast viewport-centrum till fullupplösta 3×-pixmaps via `_PageRenderer` i bakgrunden (`_update_page_lod`, debounce-QTimer 150 ms triggad av wheelEvent/scrollContentsBy/goto_page/fitInView/navigate_to_marker); vid utzoomning/scroll degraderas de tillbaka. 3×-cachen (`_page_cache`, LRU 10) återanvänds som hi-res-lager. Utzoomad panorering ritar bara små pixmaps → snabbt oavsett antal blad. |
| Smart layout — lagerbaserad processflödeslayout | `_propose_layout` är nu Sugiyama-stil istället för kraftbaserad: (1) cykelbrytning via greedy feedback-arc-set (Eades) så returledningar kapas — inte huvudflödet (bladnummer som tie-break eftersom numreringen följer processordningen); (2) longest-path-lager → kolumn per blad, flöde vänster→höger som P&ID-konventionen; (3) barycenter-svep (median) för radordning; (4) justeringspass som linjerar kopplade blad horisontellt; (5) serpentinvik för mycket långa kedjor; (6) "utility hubs" (fackla/avgas/effluent — blad med grad ≥ max(7, 10% av antal)) parkeras i egen rad längst ner så deras linjefans inte korsar flödet; (7) isolerade blad i rutnät underst. Vertikala kanter (topp/botten-connectors) får dela kolumn och staplas. Dev-verktyg: `analyze_refs.py` (kopplingsstatistik + ASCII-karta) och `render_layout.py` (PNG-förhandsvisning) mot `P&ID ref/`-biblioteken. |
| UI-cleanup — konsekvent kontextmeny-driven interface | Tre faser genomförda: (1) TreePanel: visibility toggles (⚙️ Orsaker \| ⚠️ Konsekvenser \| 🛡️ Safeguards) redan på toppen, tree widget i mitten, minimala kontroller nedtill. (2) Panel-knappar: Tog bort inline "📍 Lägg till på P&ID"-knappar från CausePanel, ConsequencePanel, SafeguardPanel (47 rader borttagna). Användare använder högerklick i trädet istället (primär) eller toolbar i P&ID-visaren (sekundär). (3) Konsistens: Context-menyer kompletta, PID-toolbar redan streamlind (🔍 Navigera, ⚙️ Orsak, ⚠️ Konsekvens). Resultat: från 4 knapprad ned till konsekvent, avsiktligt gränssnitt. |
| Tvingad säkerhetskopia före riskfyllda operationer (stabilitet #5) | `Database._write_backup()`/`_prune_backups()` (hourly 48h + daily 30d rullande, `hazop_backups/`) fanns redan och kördes throttlat (var 120:e sekund) från `commit()` samt ovillkorligt vid `__init__`-slutet, men bara EFTER `_migrate()`. Två nya tvingade (icke-throttlade) anrop tillagda: (1) `Database.__init__` tar nu en säkerhetskopia INNAN `_migrate()` körs — men bara om DB-filen redan existerade och hade data (`self.path.stat().st_size > 0`); en helt ny/tom DB har inget att förlora och hoppas över. (2) `delete_node()` (kaskaderar causes→consequences→safeguards, den mest destruktiva enskilda användaråtgärden) tar en tvingad säkerhetskopia direkt innan raderingen påbörjas, oavsett om throttle-fönstret (120s) nyss löpt ut. Båda anropen är omslutna i try/except som bara loggar en varning — ett backup-fel blockerar aldrig appstart eller radering. Nya tester i `test_regression.py::BackupSystemTests` (4 st): backup-fil innehåller samma data som live-DB, pruning håller sig inom retention-gränsen, `delete_node()` tvingar fram en ny backup trots nyligen throttlad backup, och backup-fel hindrar inte själva raderingen. |
| "Visa avvikelser utan orsaker" i Worksheet | Ny checkbox bredvid "Visa samtliga noder" i `HAZOPWorksheet`. Kopplad direkt till `ScenarioTablePanel.set_show_empty_deviations(bool)`. Tidigare försvann avvikelser tyst ur scenariotabellen om de saknade orsaker helt (såvida inte HELA noden/studien var tom — då visades placeholder-rader via befintlig fallback-logik i `_build_rows()`). `_causes_for_node()` returnerar nu en sentinel-post `(None, dev_d)` per orsakslös avvikelse när flaggan är på; huvudloopen i `_build_rows()` känner igen `cause_d is None` och anropar `_add_placeholder_row()` istället för att hoppa över avvikelsen. Fungerar interfolierat i rätt avvikelseordning, både i enskild nod-vy och "Visa samtliga noder"-vy. Flaggan är en visningspreferens (som "Fyll skärm") — den nollställs INTE av `load_node`/`load_deviation`/`load_cause`/`load_consequence`/`load_all` (kvarstår vid nodbyte), men nollställs av `clear()` (fullständig state-reset). 7 nya tester i `test_regression.py` (`ScenarioTablePanelShowEmptyDeviationsTests` + 1 i `HAZOPWorksheetTests`). |
| Konsekvenskedjedialog omgjord till stegguide (2026-08-02) | `ConsequenceStepPickerDialog` var tidigare 5 kolumner (Del1–Del5) sida vid sida i en ~1040×560 dialog. Omgjord till en kompakt (420×480) steg-för-steg-guide: en breadcrumbrad överst ("Del 1 → Del 2 → …", tidigare valda steg visar sin text, klick på ett REDAN besökt steg hoppar tillbaka dit), sedan EN steg-panel (`self._step_panel`/`_step_layout`, återbyggs helt i `_render_step(step_idx)`) med klickbara kort (en QPushButton per `(node_key, text)`-par, 0–6 st dynamiskt — aldrig hårdkodat till 5), fritextfält + "Nästa"-knapp som fallback, samt ref-tag/objekttyp-rad för aktuellt steg. Klick på ett kort avancerar direkt till nästa steg (`_card_clicked` → `_cascade_from` → `_advance_to_next` → `_render_step`). "◀ Tillbaka" (`_go_back`) återställer `_current_step` och renderar om — inget omräknas, `self._cols[i]['sel']`/`_options[i]`/`_opt_keys[i]` per steg lever kvar hela dialogens livstid även när steget inte är synligt. Terminalnoder (`next=[]`, t.ex. `fatality`) visar "Kedjan slutar här" istället för tomma kort; alla 5 steg klara visar en slutförd-status. `ref_edit`/`obj_combo` per steg skapas EN gång i `__init__` och återanvänds (flyttas in i steg-panelen via layout `addWidget`, som auto-omförälder) — måste INTE `deleteLater()`:as vid omritning. **Bugg som fixades under implementationen:** `_clear_step_layout()` gjorde ursprungligen `deleteLater()` på ALLA widgets den hittade i steg-layouten, inklusive de återanvända `ref_edit`/`obj_combo`-objekten → risk för "wrapped C/C++ object has been deleted"-krasch vid återbesök av ett steg. Fix: `_persistent_widgets()` samlar de widgets som ska överleva; `_clear_layout_recursive` gör `setParent(None)` (behåll) istället för `deleteLater()` (förstör) baserat på medlemskap i den mängden. Ephemera widgets (kort, labels, fritextfält) får BÅDE `setParent(None)` OMEDELBART (så de inte längre syns i `findChildren`/widgetträdet) OCH `deleteLater()` (verklig städning vid nästa event-loop-varv) — att bara lita på `deleteLater()` räckte inte eftersom `_render_step()` kan anropas igen (snabba klick, eller tester utan aktiv `exec()`-loop) innan Qt hunnit bearbeta den uppskjutna borttagningen. Datamodellen (`_CONSEQ_NODES`, `_CONSEQ_ENTRY`, `_CONSEQ_GENERIC_NEXT`, `_successor_pairs`, `_resolve`, `Database.set_consequence_steps`/`get_consequence_steps`) är helt oförändrad — bara presentationen är ny. "Snabbval"-textfältet (`_apply_quickselect`) togs bort (var beroende av att se alla 5 kolumner samtidigt). Ref-tag-pin-flödet (`_request_pick_for_col` → `MainWindow._on_ref_tag_picked` → dialogen visas igen) fungerar oförändrat för vilket steg som än är aktuellt. 10 nya tester i `test_regression.py::ConsequenceStepPickerWizardTests`. |
| Konsekvenskedjedialog: tillbaka till kolumnvy, men förtätad (2026-08-02) | Användaren ville tillbaka till det ursprungliga "Del1–Del5 sida vid sida"-formatet (upplevde steg-guiden som ett steg tillbaka) men "rejält förbättrad — slimmare, tightare, bättre". `ConsequenceStepPickerDialog` skrevs om en tredje gång: alla 5 kolumner visas nu samtidigt igen (ingen `_render_step`/breadcrumb/`_go_back` kvar), men designen är tätare än originalet: kolumnbredd 150px (var 175px min, dialogen ~810px total mot tidigare ~1040px, `setMinimumHeight(440)` mot tidigare 560), tunna 1px-avdelare mellan kolumnerna, "Tag"/"Typ"-etiketter förkortade och lagda i samma rad som fältet (sparar två radhöjder per kolumn jämfört med separata etikettrader ovanför), "Snabbval"-fältet borttaget permanent (var redan borttaget i steg-guide-versionen; bedömdes som lågvärdes power-user-genväg som bara skapade rörigt intryck — kan läggas till igen om användaren vill ha den). **Nya förbättringar utöver ren förtätning:** (1) Varje kolumnrubrik ("Del N") är nu statusfärgad via `_refresh_header_state()` — ljusblå = alternativ finns, mörkblå/fylld = ett val är gjort, grå = inget att göra här (antingen ej nådd än, eller kedjan tog slut). (2) Terminalnoder (`next=[]`) visar nu "Kedjan slutar här" i en liten meddelanderuta istället för en tom listruta — men bara när den FÖREGÅENDE kolumnen faktiskt har ett val (annars visas en neutral tom lista, för att inte felaktigt antyda att kedjan "tagit slut" i kolumner som helt enkelt inte nåtts än). Denna distinktion hanteras av en ny `upstream_has_sel`-parameter i `_populate_column()`/`_cascade_from()` — en bugg som fångades under implementationen: en naiv portering hade visat "Kedjan slutar här" på ALLA oanvända kolumner direkt vid dialogens öppning. `_init_columns()` initierar nu explicit kolumn 2–5 till ett neutralt tomt läge (`upstream_has_sel=False`) innan den sparade kedjan (om någon) återställs, så varje kolumnwidget garanterat får ett definierat synlighetsläge. Datamodellen (`_CONSEQ_NODES`/`_entry_pairs`/`_successor_pairs`/`_generic_pairs`/`_resolve`/`Database.set_consequence_steps`/`get_consequence_steps`) är fortsatt helt oförändrad. Tab-navigering mellan fält och ref-tag-pin-flödet (`_request_pick_for_col` → `MainWindow._on_ref_tag_picked`) bevarade oförändrat. `ConsequenceStepPickerWizardTests` (10 tester, testade steg-guidens interna API som inte längre finns) ersattes med `ConsequenceStepPickerColumnsTests` (11 tester) mot den nya kolumn-API:n — `isVisible()` fungerar inte i headless-tester eftersom dialogen aldrig `show()`:as (reflekterar toppfönstrets faktiska skärmstatus, inte widgetens egen synlighetsflagga), tester använder `isHidden()` istället. Alla 76 tester i `test_regression.py` passerar. |
| Konsekvent orsaksinmatning i alla menyer (2026-08-02) | Alla ställen där man matar in/skapar orsaker gjordes stringenta — de såg tidigare olika ut beroende på vilken meny man använde. **Borttaget (död kod, inga anropsställen):** `ObjectTagPopup`, `EditableScenarioPanel`, `RiskScenarioWizard`. **Delade hjälpfunktioner (modulnivå, ersätter kopierad kod):** `_obj_type_matches()` (bidirektionell substr-matchning, ersatte `StandardCausesPickerPopup._type_matches` staticmethod + inline-logik i `CauseObjectPopup`), `_make_tag_completer(db, parent)` (equipment_catalog-QCompleter, ersatte 2 kopior), `_maybe_save_as_standard_cause()` (extraherad ur `StandardCausesPickerPopup._maybe_save_as_standard`, nu även anropad av `CauseObjectPopup._ok()` — fritext-orsaker kan nu sparas till standardbiblioteket oavsett vilken dialog de skrevs i), `_resolve_std_deviation_id()` + `_create_cause_from_pick()` (skapar en orsak från en pickers `cause_picked`-signal, med F-nivå/frekvens satt korrekt). **Enat objekt-typ-bibliotek:** Den hårdkodade `OBJ_TYPES`-listan (10 kategorier, användes bara i `CauseObjectPopup`s combo) togs bort helt — `CauseObjectPopup._populate_type_combo()` hämtar nu samma DB-drivna `standard_objects`-lista (20 kategorier) som `StandardCausesPickerPopup` redan använde, filtrerad per avvikelse när möjligt. `_draw_equip_icon()` matchade tidigare `comp_type` exakt mot de 8 gamla OBJ_TYPES-strängarna; ny `_icon_category()`-funktion mappar den mer detaljerade standard_objects-vokabulären (t.ex. "Manuell ventil", "Säkerhetsventil/sprängbleck") till samma 8 ritkategorier via nyckelordsmatchning, så ikonerna fortsätter fungera. **Tysta tomrads-skapare ersatta med riktig inmatning direkt:** Trädmenyns "+ Lägg till orsak" (`TreePanel.add_cause`), Enter på en avvikelse i trädet (`TreePanel._add_cause_for_deviation` — delar nu logik via ny `_open_cause_picker_for_deviation()`), samt scenariotabellens Ctrl+Enter/snabbmeny (`ScenarioTablePanel._quick_add_cause`) öppnar nu `StandardCausesPickerPopup` direkt istället för att tyst infoga en tom "Ny orsak"-rad som användaren sedan var tvungen att komma ihåg att döpa om. Orsaken skapas i DB:n först när popupen accepteras (samma mönster som redan användes av ⛓ kedjad-orsak-flödet). **Inline-redigering fick autocomplete:** `_PidDelegate.createEditor()` för ORS-kolumnen (dubbelklick i scenariotabellen) kopplar nu en `QCompleter` mot standardorsaksbeskrivningar (skopad till radens objekttyp/avvikelse när känd, annars alla `standard_causes`-beskrivningar) — tidigare en helt oassisterad `QLineEdit`. **PropertiesRibbon-popuparna slogs ihop:** "📝 Redigera orsaksbeskrivning" (fritext-only `_text_popup`) och "🏷 Redigera objekttyp och tag-ID" (`CauseObjectPopup`) var två separata menyval; nu ett enda "📝 Redigera orsak (beskrivning, objekt, tag)" som öppnar `CauseObjectPopup` — samma kombinerade beskrivning+objekt+tag+standardorsak-flöde som används i scenariotabellen. **Verifiering:** `python -m py_compile hazop.py` OK, alla 76 befintliga tester i `test_regression.py` passerar oförändrat, appen startar utan krasch (`QT_QPA_PLATFORM=offscreen python hazop.py`), samt ett fristående smoke-test konstruerade `StandardCausesPickerPopup`/`CauseObjectPopup`/`_create_cause_from_pick`/`PropertiesRibbon`-actionlistan direkt mot en temp-DB för att träffa de nya kodvägarna (inga befintliga tester rörde vid dem tidigare). Inga nya permanenta tester tillagda i `test_regression.py` denna gång. |
| Autodetektera utrustning på P&ID, kopplad till tagg (2026-08-05) | Ny geometrisk symboligenkänning för vektor-PDF:er (inskannade bilder är fas 2, ej implementerat). Ny modul `symbol_geometry.py` (inget Qt-beroende): flattar `page.get_drawings()` till primitiver, klustrar dem via union-find på FAKTISK kant-till-kant-distans (`_prim_gap`, segment-till-segment) — INTE bbox-overlap, eftersom en stor omslutande form (ritningsram, kärlkontur) annars bbox-overlappar allt den innesluter och slår ihop hela sidan till ett kluster (fångat av `test_no_bbox_bridging_through_large_enclosing_shape`). Varje kluster får ett konfidensvärde 0–1 (tröskelbaserat, avsiktligt en första gissning — se `classify_cluster`). Tagg→symbol-koppling (`resolve_tag_symbol`) prioriterar: ledarlinje (kedja av ≤3 segment, tillåter en vinkel) → vidrör/intill → närmast → inget (rapporteras, göms inte). **Prestandafix (kritiskt, hittat genom att testa mot riktiga `P&ID ref/`-filer, inte bara syntetiska):** klustring tog 7–13s på riktiga sidor pga O(k²) par-jämförelse i extremt täta rutnätsceller (en cell hade 1483 primitiver — skraffering/tabellmönster, inte utrustningssymboler). Fix: `_MAX_CELL_DENSITY=40`, celler över tröskeln hoppar över par-jämförelsen helt (en riktig ventilsymbol har 4–6 primitiver, aldrig hundratals i en 20×20pt-yta) → 0,5–1,5s, som bieffekt även bättre klusterkvalitet (täta ytor slutade felaktigt smeta ihop riktiga symboler med brus). **Bugg fixad på vägen:** `pid_viewer.py`s `_extract_pdf_lines_for_page` (snap-vid-markup-ritning) testade `hasattr(path,'rects')` mot ett dict (alltid `False`) — returnerade alltid `[]`, aldrig fungerat. Görs nu om via samma primitiv-extraktion. **Ny funktionalitet i appen:** `EquipmentPanel` (Utrustningsregistret) har en ny knapp "🎯 Hitta på P&ID" som körs på ikryssade rader (sida+typ väljs via befintligt filter/kryssrutor i tabellen — inget nytt väljarfönster). Öppnar `EquipmentMarkerReviewDialog` (pid_viewer.py, modellerad på `EquipmentScanDialog`): tabellradar visar tagg (redigerbar), sida, typ, konfidens % (färgkodad), metod; låg/ingen-konfidens-rader är avkryssade som default. "💾 Spara markörer" skriver till ny tabell `equipment_markers` (id, equipment_id→equipment_catalog CASCADE, tag, pid_page, x, y, comp_type, shape_outline JSON, confidence, link_method). `PIDGraphicsView.add_equipment_marker` ritar en halvtransparent röd form (spårar `shape_outline`-polygonen, eller en generisk bowtie-ikon om ingen outline finns) — nytt `'equipment'`-lager i den redan generiska `_type_items`/`_type_visible`-mekanismen, nytt "🔧 Utrustning"-togglet i `TreePanel._VIS_BTNS` (grå, skiljer sig från orsak/konsekvens/safeguard röd/orange/grön). **Medveten avgränsning:** ingen ISA-5.1-typklassificering från formen — typ kommer alltid från taggens befintliga prefix (`KNOWN_PREFIXES`); geometrin lokaliserar bara var symbolen är ritad. Omplacering av en felplacerad markör stöds inte än (kryssa ur och placera manuellt via befintligt Orsak/Konsekvens-flöde istället) — flaggat som möjlig uppföljning. 19 nya tester: `test_symbol_geometry.py` (10 st, syntetiska PDF:er byggda i testkoden — riktiga `P&ID ref/`-filer används bara för lokal manuell körning, aldrig som testfixturer, delvis pga att några är märkta "confidential") + `EquipmentMarkersDbTests`/`EquipmentMarkerReviewDialogTests` (9 st) i `test_regression.py`. Alla 100 tester passerar. |
| Fix: Worksheet-krasch efter "Nytt projekt"/"Öppna .hzp" — `sqlite3.ProgrammingError` (2026-08-05) | Anton rapporterade felmeddelande vid klick på Worksheet-fliken. Krasch-loggarna visade `HAZOPWorksheet.refresh()` → `_populate_node_combo()` → `self.db.nodes()` → "Cannot operate on a closed database", oavsett vilken sida man kom ifrån. Grundorsak: `MainWindow._reload_all_panels()` (körs av `_hzp_new`/`_load_hzp` efter att den gamla DB-anslutningen stängts och en ny `Database`-instans öppnats) uppdaterar `.db`-referensen på en hårdkodad panel-lista — men `self.worksheet`, `self.red_markup_panel` och `self.red_markup_table_panel` saknades i den listan. De behöll sin GAMLA (nu stängda) `Database`-referens permanent efter ett "Nytt projekt"/"Öppna projekt"-anrop, tills appen startades om. `HAZOPWorksheet` har dessutom en egen inbäddad `ScenarioTablePanel`-instans (`self._table_panel`, skild från `MainWindow.scenario_panel`) som också behövde sin egen uppdatering. **Fix:** lade till `self.worksheet`, `self.red_markup_panel`, `self.red_markup_table_panel` i panel-listan, plus ett separat `self.worksheet._table_panel.db = db`-anrop (samma mönster som redan användes för Inställningars underpaneler). **Verifierat att buggen var verklig:** återskapade den exakta kraschen genom att tillfälligt ta bort `self.worksheet` ur listan igen och köra det nya testet — kraschade identiskt med den rapporterade tracebacken, sedan grönt igen efter att fixen återställdes. 2 nya tester i `test_regression.py::ReloadAllPanelsDbSwapTests` (verifierar att alla paneler — inklusive den inbäddade `_table_panel` — faktiskt får den nya `db`-referensen, samt en end-to-end-regression som anropar `worksheet.refresh()` efter en DB-swap och kräver att den INTE kastar `sqlite3.ProgrammingError`). Alla 102 tester passerar. |

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

## Prestandaoptimering — ScenarioTablePanel (2026-07-29)

**Refaktoring genomförd:**
1. **Signal-baserat deferred rebuild** — Ersatte alla `QTimer.singleShot(0, self._rebuild)` (24 st) med en central `_schedule_rebuild()` metod
   - `_rebuild_pending` flag förhindrar dubbletter
   - `_on_rebuild_scheduled()` är enda deferred-körs-punkten
   - Eliminerar race conditions från timerkaskader

2. **Extraherad `_resize_rows(vscroll_value, hscroll_value)` metod** — Tidigare en unnamed closure i `_rebuild()`
   - Ansvarar för `resizeRowsToContents()` + höjdbegränsningar + scroll-position-återställning
   - Anropas direkt (inte defer) från `_rebuild()` efter `_apply_spans()` för att undvika infinite loops
   
3. **Förhindrad infinite loop** — Tidigare flöde var:
   ```
   _rebuild() → _apply_spans() → _on_cell_changed(via signal) → _rebuild()
   ```
   Nu: `blockSignals()` under `_rebuild()` → `_on_cell_changed` aldrig triggar under ombyggnad
   
4. **Resultat:**
   - Tabellredigering är nu responsiv (ingen lag på tangentslag)
   - Row-höjder anpassas fortfarande korrekt
   - Spann appliceras utan ombyggnader
   
**Test:** `python -m py_compile hazop.py` OK. Verifierad: 0 direkta `QTimer.singleShot(0, self._rebuild)` | 24 `_schedule_rebuild()` anrop | 1 `QTimer` i `_on_rebuild_scheduled()` (korrekt).

---

## Stabilitetsgenomgång (2026-08-02)

**Bakgrund:** En serie krascher (app stängdes tyst vid klick på orsak-markör, röd markup m.m.) spårades och åtgärdades i flera omgångar med parallella granskningsagenter, följt av en implementationsomgång och två oberoende slutgranskningar.

**Grundorsak till de flesta krascherna:** `Database.get_node/get_cause/get_consequence/get_safeguard/get_deviation/get_node_markup/get_node_red_markup` returnerade rått `sqlite3.Row` (stödjer inte `.get(key, default)`), men flera anropsställen antog dict-beteende. Fixat vid källan (commit `267866f`) — alla dessa metoder returnerar nu `dict(row) if row else None`, vilket eliminerar hela buggklassen strukturellt istället för att patcha varje anropsplats.

**Övriga fixade krascher/problem denna session:**
- `view_stack` användes innan den initierats (setChecked triggade signal för tidigt)
- Toolbar-lambdor refererade `tree_panel` innan den skapades
- 6 `blockSignals()`-block utan try/finally kunde lämna signaler permanent blockerade vid exception
- 13+ array/dict-access utan bounds-check (RRF_VALUES, SG_TYPES, currentIndex()==-1)
- Saknade `dict()`-konverteringar av `sqlite3.Row` på 3 ytterligare ställen (`_update_badge`, `_on_cell_changed_inner`, `_on_selected` SG_T-gren)
- Orphan-data vid radering: `consequence_severities`/`consequence_severity_exclusions` saknar FK helt — nu manuellt rensade i `delete_node/cause/consequence/safeguard`; `causes.linked_consequence_id` nollas när målkonsekvensen raderas
- Trädraderingar triggade inte P&ID-overlay-refresh (stale markers) — nu wired till `reload_overlays()`
- `SettingsPanel._cat_delete()` refererade `self._sev_def_panel` som aldrig skapades — garanterad krasch vid radering av konsekvenskategori
- 4× `QMessageBox(None, ...)` istället för `self` som parent
- `ConnectorAnalyzer`-tråden kunde hänga hela appen permanent (värre än krasch) om PDF-analys failade mitt i — saknade try/except runt merparten av `run()`
- Röd markup-klick: `highlight_markup()`/`_start_inline_label_edit()`/`zoom_to_markup_items()` kollade bara `_markup_items`, aldrig `_red_markup_items` — **fixades, reverterades av misstag (trodde det orsakade en startup-krasch som i själva verket var sqlite3.Row-buggen ovan), återapplicerades sedan i slutgranskningen**
- `_approve_node()` saknade None-guard på `node["name"]` — krasch vid godkännande av redan raderad nod
- `_write_hzp()` (Spara som) läckte en sqlite3-anslutning vid backup-fel, vilket sedan gjorde att `unlink()` kastade `PermissionError` (reproducerat på detta Windows-system) och maskerade det ursprungliga felet
- 3 paneler (`StandardCausesSettingsPanel`, `PIDAnalysisPanel`, `StandardObjectsSettingsPanel`) kopplade om samma signal vid varje anrop utan att koppla ur föregående — O(n²) redundanta DB-skrivningar över en lång session

**Stabilitetsförbättringar (5 st, implementerade parallellt):**
1. **Global `sys.excepthook`** (`hazop.py`) — fångar undantag i Qt-slots (knapptryck, trädval etc.) och visar felruta + loggar via `CrashReporter`, istället för att appen tyst stängs. Skiljer på startup-fel (fortfarande fatalt, tydligt fel) och runtime-fel efter att event loop startat (appen fortsätter köra).
2. **DB-nivå orphan-cleanup** — manuell rensning i delete-metoderna för tabeller utan FK (`consequence_severities`, `consequence_severity_exclusions`, `causes.linked_consequence_id`), eftersom SQLite inte kan lägga till FK-constraints retroaktivt utan tabellombyggnad.
3. **Enhetlig dict-baserad DB-helper** — se grundorsak ovan.
4. **Regressionstestsvit** (`hazop/test_regression.py`, `python test_regression.py -v`) — 20 tester, headless via `QT_QPA_PLATFORM=offscreen`, täcker DB-lagret och GUI-smoke-tester för alla kända kraschmönster. Kör denna efter framtida ändringar i Database-klassen eller borttagningslogik.
5. **Automatisk backup** — `Database._write_backup()` (fanns delvis sedan tidigare: hourly/daily rolling retention) utökad med forcerad backup före schema-migrering och före `delete_node()`.

**Kvarvarande lågprioriterade poster (ej åtgärdade, ofarliga):**
- `delete_deviation()` saknar samma forcerade pre-delete-backup som `delete_node()` fick (inkonsekvens, ej bugg)
- Några ställen dubbel-wrappar redan-dict-värden i `dict(x) if x else None` (ofarligt no-op, kosmetiskt)
- Ingen `closeEvent`/`aboutToQuit`-hantering stoppar bakgrundstrådar (`_prefetch_thread`, `_lod_renderer`, `_analyzer_thread`) vid appavslut — kan i sällsynta fall ge en Qt-varning vid stängning

---

## Tredje `_rebuild()`-kraschen — ny trigger hittad (2026-08-02, uppföljning)

**Bakgrund:** Krascher i `ScenarioTablePanel._rebuild()` återkom en TREDJE gång trots två tidigare fixar (`84c8b7c`: `_LopaWidget`-focus-out-reentrancy-skydd i `_update_lopa_risk()`; `686e289`: dubbel `tree_panel.refresh()+_on_selected()`-mönster). Loggen (`hazop_crash.log`, 2026-08-02 12:08:46) visade `_rebuild: D — setRowCount(0)` och `_rebuild: E — reset meta` och sen inget mer — ingen Python-exception, processen dog tyst mitt i `_build_rows()`, som saknade all intern loggning.

**Verifierat att de två tidigare fixarna fortfarande sitter korrekt:**
- `_rebuild()` rensar fokus (`self._table.focusWidget().clearFocus()`, null-skyddat) före `setRowCount(0)` — oförändrat, korrekt.
- `_update_lopa_risk()` har `if getattr(self, '_rebuilding', False): return` som allra första rad — oförändrat, korrekt.
- `_LopaWidget` är fortfarande den ENDA `setCellWidget()` inuti `ScenarioTablePanel` — inga andra oskyddade cell-widgets hittades.

**Ny grundorsak hittad:** `ScenarioTablePanel._edit_extra()` (kopplad till en `_LopaWidget`s "+ övriga"-knapp — en live cell-widget i tabellen) anropade `self._rebuild()` **direkt och synkront** efter `dlg.exec()` — den ENDA dialoghanteraren i hela klassen som gjorde så. Alla andra 24 `_schedule_rebuild()`-anrop (via `QTimer.singleShot(0, ...)`) är korrekt fördröjda. Problemet: `dlg.exec()` kör en nästlad Qt-eventloop, och en redan köad `_schedule_rebuild()`-timer (från en tidigare cellklick) kan trigga UNDER den nästlade loopen — inte efter. Det innebär att `_rebuild()` kan köras medan `_edit_extra()` (och knappens `clicked`-hanterare som anropade den) fortfarande ligger kvar på C++-anropsstacken, pausad inuti `dlg.exec()`. `_rebuild()`s `setRowCount(0)` förstör då samma `_LopaWidget`/knapp som orsakade anropet — use-after-free utan Python-exception.

**Fix:** `_edit_extra()` anropar nu `self._schedule_rebuild()` istället för `self._rebuild()` direkt, i linje med alla andra dialoghanterare i klassen.

**Även gjort (hårdning + diagnostik, inte bevisad grundorsak):**
- `_ScenarioDelegate.sizeHint()` — hela kroppen omsluten av try/except med säker `QSize`-fallback. Databaskontroll visade inga patologiska data (max 132 tecken, inga kontrolltecken) så detta är sannolikt INTE den faktiska triggern, men kostar inget som skyddsnät.
- `_resize_rows()` — `resizeRowsToContents()`-anropet loggar nu explicit före/efter och loggar+re-raise:ar vid Python-catchable exceptions.
- Granulär loggning tillagd genomgående i `_build_rows()` (checkpoints F0/F1/F2/G0-G3/H0-H3/I0), `_apply_spans()` (J0-J6) och `_resize_rows()` (K0-K3) — om kraschen återkommer en fjärde gång pekar loggen exakt ut vilket delsteg som orsakade den, istället för att bara visa D/E som tidigare.

**`QGraphicsScene::addItem`-varningsfloden (pid_viewer.py) utredd separat:** En bakgrundsagent uteslöt alla 6 konkreta hypoteser om objekt-återanvändning (reentrant `_load_overlays()`, dubblettrader i DB, dubbel connection-line-loop, etc.) — ingen bekräftad. Enligt Qt-semantik är `addItem()` på ett redan tillagt objekt ett no-op (loggar varning, kraschar inte, korrumperar inte i sig). Varningsfloden är sannolikt symptom på samma allmänna "dubbel-invocation"-mönster som de tidigare `_rebuild()`-buggarna, men den exakta triggern hittades inte den här gången — inget fixat i `pid_viewer.py` denna session.

**Ärlighet om säkerhetsgrad:** Native (C++-nivå) krascher går per definition inte att bevisa 100% i efterhand utan en debugger/core dump. `_edit_extra()`-fixen är en konkret, verifierad, tidigare-ej-undersökt reentrancy-väg som matchar kraschsignaturen exakt (tyst död, inget Python-undantag, mitt i tabellombyggnad) — men det kan finnas ytterligare triggers av samma klass. Den nya granulära loggningen är den viktigaste förbättringen: nästa ocurrence (om någon) bör peka ut den exakta raden istället för att kräva ny gissning.

**Test:** Ny testklass `EditExtraDeferredRebuildTests` i `test_regression.py` (2 tester) verifierar att `_edit_extra()` numera anropar `_schedule_rebuild()` istället för `_rebuild()` direkt, och att en redan köad rebuild-timer som triggar under dialogens nästlade eventloop inte orsakar en extra, direkt-stacked rebuild ovanpå den. Alla 46 tester (44 tidigare + 2 nya) passerar: `python test_regression.py -v`.

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
