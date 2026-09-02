# LOPA – implementationsplan

Status: beslutad designgrund före implementation
Avgränsning: LOPA implementeras först. SRS ligger utanför denna etapp.

## Implementationsstatus (2026-09-02)

### Genomfört i första vertikala etappen

- Egen LOPA-sida i huvudnavigeringen med lista, LOPA/SIF-identitet,
  revisionsval, lås/upplåsning och beräkningsöversikt.
- Databasmodellen för LOPA, revisioner, HAZOP-scenarier, konsekvenser,
  sensorgrupper, givardelsmedlemmar, barriärer och ändringslogg.
- Skapa tom LOPA och skapa/anslut via **Koppla till LOPA…** på en HAZOP-
  safeguard. Objekt och utlösningsvillkor är strukturerade.
- Import av HAZOP-konsekvensnivåer, numerisk frekvens när den finns och
  övriga safeguards som oberoende LOPA-barriärer.
- Samma HAZOP-riskmatris med dynamiska kategorier samt LOPA-inställningar för
  TEL, safeguard-typer, RRF-till-SIL-band och förutsättningsantagande.
- Deterministisk beräkning med styrande kategori, explicit avsaknad av
  numeriskt underlag och revisionssnapshot av riskmatrisen.

### Nästa etapper

- Redigera/importera/avmarkera scenarier, kategorier, barriärer och
  eskaleringsfaktorer direkt i LOPA-vyn.
- Aktiv synkstatus, radvisa jämförelser och Gå till HAZOP/P&ID.
- LOPA-rapport och Excel-export med låst revisionssnapshot.
- SRS-koppling först efter att LOPA-arbetsflödet är stabilt.

## Syfte

LOPA ska vara en egen modul för skyddsbarriäranalys och SIF-dimensionering.
HAZOP är den primära källan: aktiv LOPA ska spegla relevant HAZOP-underlag,
medan varje låst revision behåller ett exakt historiskt underlag.

En LOPA motsvarar en SIF och kan innehålla flera HAZOP-orsaksscenarier.

## Fastslagna principer

| Område | Beslut |
| --- | --- |
| Informationskälla | HAZOP är huvudkälla. Aktiv LOPA följer HAZOP där det är lämpligt. |
| Riskmatris | LOPA använder samma aktiva HAZOP-riskmatris. Ingen separat LOPA-matris skapas. |
| TEL | TEL kan anges per konsekvenskategori och konsekvensnivå i samma riskmatris. |
| Kategorier | Alla aktiva HAZOP-kategorier deltar, även exempelvis Anläggning och Rykte. |
| Konsekvenser | Konsekvenser speglas från HAZOP. LOPA-only-konsekvenser tillåts inte. |
| Manuella barriärer | Tillåts, men markeras Finns inte i HAZOP och skrivs inte tillbaka. |
| HAZOP-RRF / LOPA-krav | Hålls isär. LOPA skriver inte tillbaka beräknat RRF eller SIL till HAZOP i första versionen. |
| SRS | Väntar tills LOPA är stabil och verifierad. |

## HAZOP-koppling och objektidentitet

När användaren högerklickar på en HAZOP-safeguard och väljer Koppla till
LOPA blir safeguardingens objekt givardel i en ny eller befintlig SIF.

Scenarionyckeln är alltid:

    equipment_catalog.id + utlösningsvillkor

Exempel:

- LT-101 + HH och LT-101 + LL är olika LOPA-scenarier.
- Samma LT-101 + HH kan importera samtliga matchande HAZOP-förekomster.
- En safeguard med flera kopplade taggar ger ett givardelsobjekt per tagg.

Beskrivningstext används aldrig som primär identitet. För äldre data utan
strukturerat objekt eller utlösningsvillkor visas Ej angivet tills användaren
kompletterar uppgiften.

Utlösningsvillkor ska bli ett eget fält på kopplingen mellan safeguard och
objekt. Standardval är H, HH, L, LL, Till, Från och Egen text.

## Importregler

Vid HAZOP-koppling ska LOPA:

1. Skapa en ny LOPA/SIF eller ansluta till vald befintlig LOPA.
2. Söka fram alla HAZOP-förekomster med samma objekt och utlösningsvillkor.
3. Importera varje förekomst som ett separat, checkbart orsaksscenario.
4. Importera konsekvensnivåer från Risk före barriärer.
5. Importera övriga safeguards som oberoende barriärer med beskrivning, RRF,
   safeguard-typ och kategoritillämpning från HAZOP.
6. Behandla den valda SIF-/givardelsfunktionen som givardel, aldrig som en
   oberoende barriär i samma LOPA-beräkning.

Varje importerad post ska spara HAZOP-id:n plus en snapshotskopia av
källinnehållet. Det möjliggör synkstatus, revisionshistorik och Gå till HAZOP.

## HAZOP-spegling och lokala avvikelser

### Aktiv, olåst revision

Importerade rader har normalt läget Följer HAZOP. Följande speglas:

- objekt, taggnummer och utlösningsvillkor
- beskrivning
- frekvens
- safeguard-typ och RRF
- konsekvensnivåer och kategoritillämpning

Vid redigering av ett HAZOP-kopplat fält ska programmet fråga utifrån vad som
redigeras. Exempel:

- Frekvens: ändra bara LOPA eller även den aktuella HAZOP-orsaken.
- Taggnummer: ändra lokalt, ändra objektets tagg överallt eller avbryt.
- RRF, typ, beskrivning och tillämpning: erbjud uppdatering av aktuell HAZOP-
  förekomst och, när det är lämpligt, samtliga verifierade matchningar.

Alla betyder endast poster med samma stabila objekt-id och samma
utlösningsvillkor. Det betyder aldrig osäker textsökning eller alla safeguards
i projektet. Varje dialog visar antal och en kort lista över berörda rader.

### Losskoppling och saknad källa

Lokal avvikelse kräver valet Koppla loss från HAZOP. Raden och LOPA:ns
sammanfattning ska då varna med exempelvis Följer inte HAZOP. Motivering är
möjlig, men inte obligatorisk.

När en HAZOP-källa ändras visas en ändringsvarning. Användaren väljer aktivt
om den öppna LOPA-revisionen ska uppdateras eller behålla sin snapshot.

När HAZOP-källa eller objekt tas bort behålls LOPA-underlaget och varnas med
exempelvis Källan finns inte längre i HAZOP. LOPA kan länkas om eller kopplas
loss, men raderas aldrig som följd av HAZOP-radering.

## Riskmatris, TEL, eskalering och SIL

LOPA läser den aktiva HAZOP-riskmatrisens kategorier, nivåer, koder,
beskrivningar, axlar och färger. Under HAZOP Preparation → Riskmatris
tillkommer underfliken LOPA med:

- TEL per kategori och konsekvensnivå
- flexibla eskaleringsfaktorer per kategori
- gemensamma safeguard-typer för HAZOP och LOPA
- redigerbar RRF-till-SIL-tabell

TEL härleds aldrig från färg eller riskklass. Varje kombination har eget
värde, exempelvis Person 4, Miljö 4 och Rykte 4.

Varje aktiv kategori får en egen eskaleringsrad. Antalet faktorer och deras
rubriker kan anpassas per kategori. Varje faktor har namn, motivering och
procentvärde. Person kan börja med Antändning, Närvaro och Skadas.

Dimensionerande kategori väljs automatiskt som den med högst krav på
riskreduktion. Användaren kan ändra den manuellt med motivering.

Standardtabell för RRF till SIL:

| Krävd RRF | SIL |
| ---: | --- |
| 0–10, inklusive 10 | SIL 0 / A |
| över 10–100, inklusive 100 | SIL 1 |
| över 100–1 000, inklusive 1 000 | SIL 2 |
| över 1 000–10 000, inklusive 10 000 | SIL 3 |
| över 10 000–100 000 | SIL 4 |

SIL-tabell, TEL och övriga LOPA-inställningar är redigerbara per projekt och
snapshotsparas per LOPA-revision. Riskmatrisändringar ger varning; även en
öppen revision uppdateras endast efter aktivt val. Låsta revisioner ändras
aldrig automatiskt.

## Frekvens och förutsättning

HAZOP har två frekvensfall:

1. Numerisk frekvens från standardorsak, felfrekvensdatabas eller manuellt
   numeriskt värde. Den kopieras som LOPA:s källvärde.
2. Enbart vald HAZOP-frekvensnivå. Nivån visas som underlag, men ett numeriskt
   LOPA-värde måste anges innan beräkningen är komplett.

LOPA får inte hitta på ett representativt tal för enbart en frekvensklass.

Varje orsaksscenario kan ärva en procentuell förutsättning från LOPA-mallen
eller ha ett eget värde. Standard är 100 %. Beräkningen är:

    effektiv frekvens = kvarvarande frekvens × förutsättning / 100

10 % betyder 1 av 10, alltså multiplikation med 0,1. Fältet
Kontrollfrekvens från handoffen ingår inte i första LOPA-versionen.

LOPA ska visa Ofullständig — kan inte beräknas med konkret fellista när aktivt
underlag saknas, exempelvis numerisk frekvens, HAZOP-källa eller vald
konsekvenskategori.

## Exkludering, safeguards och MooN

Tre exkluderingsnivåer ska finnas:

| Nivå | Effekt |
| --- | --- |
| Orsaksscenario | Hela frekvensvägen exkluderas. |
| Konsekvenskategori | Endast den kategorins beräkning exkluderas. |
| Safeguard | Barriärens RRF exkluderas från berörda kategorier. |

Urkryssade HAZOP-poster visas nedtonade, men finns kvar för transparens.
Motivering kan anges frivilligt på alla tre nivåer.

Safeguard-typer ersätts med en redigerbar projektlista som delas av HAZOP och
LOPA. LOPA bygger sina barriärkolumner direkt från denna lista. HAZOP:s
kategoritillämpning följer med som startläge och kan justeras per revision.

Manuella LOPA-barriärer är tillåtna och markeras Finns inte i HAZOP.

Den safeguard som blir SIF blir givardel, inte oberoende barriär. Första
kopplingen skapar normalt 1oo1. Ett andra objekt i gruppen föreslår 1oo2,
men användaren bekräftar alltid ändringen. Borttagna gruppmedlemmar raderas
inte tyst ur revisionshistoriken. Manöverdel redigeras direkt i LOPA i första
versionen.

## Revisioner, nummer och arkiv

- Revision startar på 00; nästa föreslås som 01, 02 och så vidare.
- Revisionsbeteckningen kan ändras manuellt men ska vara unik inom LOPA:n.
- Ny revision kopierar aktiv revision.
- Jämförelse kan göras mellan vilka två revisioner som helst.
- Statusar är Utkast, Låst, Godkänd och Arkiverad.
- Upplåsning är tillåten och loggar vem, när och varför.

Låsta revisioner innehåller snapshots av riskmatris, TEL, SIL-tabell,
källunderlag, beräkningar och lokala avvikelser.

LOPA har stabilt internt id och separat synligt LOPA-nummer. Programmet
föreslår nästa nummer, men användaren kan alltid ange exempelvis 017 manuellt.
Aktiva nummer måste vara unika. Automatisk numrering återanvänder inte
arkiverade nummer; manuellt återbruk efter varning är tillåtet.

Ta bort LOPA arkiverar posten i stället för att permanent radera den.

## Gränssnitt, deltagare och navigation

LOPA får en egen knapp i vänstra huvudnavigeringen. Vyn ska ha LOPA-lista till
vänster och vald analys/revision till höger. Listan visar nummer, SIF-namn,
revision, status, krav-SIL och synkvarningar.

Det ska gå att skapa tom LOPA direkt i LOPA-vyn. En sådan markeras som
fristående från HAZOP men kan ändå låsas eller godkännas. HAZOP-källor kan
kopplas på senare.

- Utförd av och Godkänd av återanvänder projektets deltagarlista.
- Fri text tillåts för externa personer utan ändring av deltagarregistret.
- Varje HAZOP-kopplad LOPA-rad har Gå till HAZOP.
- Klick på givardelsobjekt kan öppna P&ID och markera objektet.
- HAZOP-safeguards visar LOPA-länk med antal kopplade LOPA:er och kan öppna
  rätt LOPA eller en lista vid flera träffar.

## Excel-export

Excel-export ingår i LOPA-arbetet:

- Exportdialogen väljer LOPA:er och revisioner; aktiv revision är förvald.
- Arbetsboken får en sammanfattningsflik.
- Varje exporterad LOPA-revision får en egen formaterad analysflik.
- Analysfliken innehåller HAZOP-källor, synkstatus, scenario, givardel,
  konsekvenser, barriärer, kategori/TEL, eskalering, beräkning, krav-SIL och
  revisionsuppgifter.

## Föreslagen teknisk utformning

Implementera normaliserade SQLite-tabeller, inte enbart JSON eller fria
texter. Behövs minst:

- lopa_records och lopa_revisions
- lopa_source_scenarios och lopa_consequence_links
- lopa_sensor_groups och lopa_sensor_members
- lopa_barriers med tillämpning per konsekvenskategori
- lopa_escalation_factors, lopa_comments och lopa_change_log
- snapshots för riskmatris, TEL, SIL-tabell och HAZOP-källor

Safeguard–objekt–utlösningsvillkor behöver en egen normaliserad koppling i
HAZOP, eftersom en safeguard kan ha flera objekt och samma objekt kan ha olika
utlösningsvillkor i olika scenarier.

Nytt UI bör ligga i exempelvis lopa_panel.py och följa projektets befintliga
lager- och re-export-mönster. All databasåtkomst går via Database.
Sammansatta ändringar använder Database.history_group() så att Ctrl+Z/Ctrl+Y
omfattar LOPA. Design ska läggas i design.py.

Den befintliga interna _LopaWidget i scenario_panel.py är en Enablers-widget
och får inte återanvändas som den riktiga LOPA-modulen.

## Etappindelning

1. Datamodell, migrationer, LOPA-inställningar i riskmatrisen och testbar
   beräkningsmotor.
2. Objekt-/utlösningsvillkor på HAZOP-safeguards och säker matchning.
3. LOPA-huvudvy, listvy, revisioner och HAZOP-kopplingsdialog.
4. Scenariotabell, konsekvenser, givardel/MooN, safeguards och beräkningar.
5. Synkstatus, varningar, jämförelse, HAZOP/P&ID-navigation och undo/redo.
6. Excel-export samt visuell och automatiserad verifiering.
7. SRS planeras först efter godkänd LOPA-etapp.

## Verifiering före klar LOPA-etapp

- Migrering på nya och befintliga projekt.
- Enhetsprov för TEL, procentfaktor, eskalering och alla RRF/SIL-gränser.
- Test av samma objekt med HH respektive LL.
- Test av import av flera HAZOP-förekomster samt urkryssning per nivå.
- Test av HAZOP-synk, losskoppling, borttagen källa och låsta revisioner.
- Test av Ctrl+Z/Ctrl+Y för koppling, import, ändring och arkivering.
- Test av HAZOP- och P&ID-navigation från LOPA samt LOPA-länk från HAZOP.
- Excel-export med flera LOPA:er och revisioner.
- Riktad visuell GUI-kontroll i riktig Qt-miljö, utöver headless-tester.
