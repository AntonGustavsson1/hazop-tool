# Axelmappning (riskmatris → ny riskmatris) — Handoff till Codex

Popup/modal med två flikar ("1a Kopplingsfält" och "1b Matris mot matris") som låter en användare mappa varje steg i en gammal riskmatris axel till ett steg i en ny riskmatris axel, med visuell koppling via kurvade pilar. Båda flikar delar samma state (mappning, vald etikett) — de är två visualiseringar av samma underliggande data, inte separata verktyg.

## 1. Datamodell

```ts
type AxisStep = { id: string; t: string; s: string }; // t = kort kod (bokstav/siffra/grekisk bokstav), s = beskrivning

// Gammal matris axlar (exempel, byt ut mot HAZOP-programmets verkliga data)
const OLDX: AxisStep[] = [ // Konsekvens, 7 steg
  { id: 'x:A', t: 'A', s: 'Försumbar' },
  { id: 'x:B', t: 'B', s: 'Obetydlig' },
  { id: 'x:C', t: 'C', s: 'Liten' },
  { id: 'x:D', t: 'D', s: 'Måttlig' },
  { id: 'x:E', t: 'E', s: 'Betydande' },
  { id: 'x:F', t: 'F', s: 'Allvarlig' },
  { id: 'x:G', t: 'G', s: 'Katastrofal' },
];
const OLDY: AxisStep[] = [ // Frekvens/sannolikhet, 6 steg
  { id: 'y:0', t: '0', s: '< 0,001 /år' },
  { id: 'y:1', t: '1', s: '0,001 /år' },
  { id: 'y:2', t: '2', s: '0,01 /år' },
  { id: 'y:3', t: '3', s: '0,1 /år' },
  { id: 'y:4', t: '4', s: '1 /år' },
  { id: 'y:5', t: '5', s: '> 1 /år' },
];

// Ny matris axlar
const NEWC: AxisStep[] = [ // Konsekvens, 4 steg
  { id: 'c1', t: '1', s: 'Liten — första hjälpen' },
  { id: 'c2', t: '2', s: 'Måttlig — vårdbehov' },
  { id: 'c3', t: '3', s: 'Allvarlig — sjukhusvård' },
  { id: 'c4', t: '4', s: 'Katastrofal — dödsfall' },
];
const NEWL: AxisStep[] = [ // Frekvens, 5 steg (grekiska bokstäver, mest sannolikt→minst sannolikt)
  { id: 'l5', t: 'ε', s: 'Nästan säkert — varje vecka' },
  { id: 'l4', t: 'δ', s: 'Sannolikt — varje månad' },
  { id: 'l3', t: 'γ', s: 'Möjligt — vart år' },
  { id: 'l2', t: 'β', s: 'Osannolikt — vart 10:e år' },
  { id: 'l1', t: 'α', s: 'Sällsynt — vart 100:e år' },
];

// Riskcell-färger, för att rendera själva matriserna i 1b
const OLDGRID = ['LLLLLLL', 'LLLBBBB', 'LLBBYYY', 'LBBYYRR', 'BBYYRRR', 'BYYRRRR']; // rad = OLDY-index, kolumn = OLDX-index
const OLDCOL = { L: '#b6c8dc', B: '#4a7fc1', Y: '#e8d44d', R: '#cf4b3e' };

const NEWGRID = [ // rad = NEWL-index, kolumn = NEWC-index
  ['M', 'H', 'C', 'C'],
  ['M', 'M', 'H', 'C'],
  ['W', 'M', 'H', 'H'],
  ['W', 'M', 'M', 'H'],
  ['W', 'W', 'M', 'M'],
];
const NEWCOL = { W: '#93c05e', M: '#e8d44d', H: '#e08a3c', C: '#cf4b3e' };
const NEWTXT = { W: 'Low', M: 'Moderate', H: 'High', C: 'Catastrophic' };
```

**Viktigt:** axlarna kan ha olika antal steg på var sida (7↔4, 6↔5 i exemplet) — mappningen måste stödja många-till-en (flera gamla steg pekar på samma nya steg), aldrig en-till-många.

### Delat state (gäller båda flikarna)

```ts
type State = {
  map: Record<string, string>;      // gammalt steg-id -> nytt steg-id
  armed: string | null;             // gammalt steg-id som är "valt" och väntar på klick-mål (klick-utan-drag-flödet)
  drag: {                           // aktiv drag-operation, null annars
    axis: 'c' | 'l';
    id: string;                     // gammalt steg-id som dras
    x: number; y: number;           // muspos relativt containern (för spökchip / dragline)
    moved: boolean;                 // false tills muspekaren rört sig >5px
    over: string | null;            // nytt steg-id som muspekaren just nu hovrar över (för highlight)
  } | null;
};
```

`map` är den enda källan till sanning — båda flikarna läser och skriver till samma `map`, så en mappning gjord i 1a syns direkt i 1b och tvärtom om användaren växlar flik.

## 2. Interaktion (identisk logik i båda flikarna)

Två sätt att mappa ett gammalt steg till ett nytt:

1. **Drag-and-drop:** `pointerdown` på ett gammalt steg → sätt `armed` + starta `drag`. Globalt `pointermove`-lyssnare uppdaterar `drag.x/y` och beräknar `drag.over` via `document.elementFromPoint` + leta upp närmaste `[data-tgt]`-element vars axel matchar. Om muspekaren flyttat >5px räknas det som en drag (`moved:true`); annars tolkas `pointerup` som ett enkelt klick. `pointerup` på ett giltigt mål committar `map[id] = targetId`; annars avbryts.
2. **Klick-klick:** klicka ett gammalt steg → `armed = id` (klicka samma igen → avarmerar). Klicka sedan ett nytt steg som tillhör samma axel → committar mappningen och avarmerar.

Extra beteenden:
- Klicka på ett redan mappat gammalt steg (utan att dra) tar bort dess mappning.
- "Föreslå automatiskt": fördelar gamla steg jämnt över nya steg proportionellt efter index (fungerar med olika längder; frekvensaxeln speglas eftersom dess ordning är omvänd — mest sannolikt sist).
- "Rensa": tömmer `map` och `armed`.
- En liten räknarbadge på varje nytt steg visar hur många gamla steg som pekar på det.
- Ett litet spökchip (chip med gammalt stegs kod+beskrivning) följer muspekaren under drag.

## 3. Flik 1a — Kopplingsfält (lista mot lista)

Layout: två kolumner ("Gammal matris" / "Ny matris") separerade av ett tomt mellanrum, upprepat för två sektioner (Konsekvens, Frekvens/sannolikhet). Varje gammalt steg är en rad-chip (kod + beskrivning + statuspunkt), varje nytt steg är en rad-chip (statuspunkt + kod + beskrivning + räknarbadge).

Ovanpå listorna ligger ett `<svg>` (position: absolute, fyller hela kortet, `pointer-events:none`) som ritar en kurvad Bezier-path per aktiv mappning, från högerkanten av den gamla chipen till vänsterkanten av den nya chipen.

**Bezier-kurva:**
```js
function anchorPt(rect, anchor) { // anchor: 'left' | 'right' | 'top' | 'bottom'
  if (anchor === 'left')   return { x: rect.x,            y: rect.y + rect.h/2, dx: -1, dy: 0 };
  if (anchor === 'right')  return { x: rect.x + rect.w,    y: rect.y + rect.h/2, dx:  1, dy: 0 };
  if (anchor === 'top')    return { x: rect.x + rect.w/2,  y: rect.y,           dx:  0, dy: -1 };
  return                          { x: rect.x + rect.w/2,  y: rect.y + rect.h,  dx:  0, dy:  1 };
}
function curve(a, b, fan = 0) {
  const dist = Math.min(120, Math.max(45, Math.hypot(b.x - a.x, b.y - a.y) * 0.3) + fan);
  const c1 = { x: a.x + a.dx * dist, y: a.y + a.dy * dist };
  const c2 = { x: b.x + b.dx * dist, y: b.y + b.dy * dist };
  return `M${a.x} ${a.y} C${c1.x} ${c1.y} ${c2.x} ${c2.y} ${b.x} ${b.y}`;
}
```
`fan` används bara i 1b (se nedan) för att sprida ut flera pilar som annars skulle överlappa — sätt `fan = index * 13` per gammalt steg i frekvens-listan.

**Mätning av positioner:** varje kopplingsbar chip/header får ett `data-node` (unik nyckel, t.ex. `axis|old|stepId`) och `data-anchor` (`left`/`right`/`top`). Efter varje render (och vid resize/fontladdning), mät alla `[data-node]`-element relativt kortets `getBoundingClientRect()` och spara `{x,y,w,h,anchor}` i state. `links()`-funktionen slår upp `pos[old-node]` och `pos[new-node]` för varje mappning i `map` och genererar path-strängen. Rendera om SVG:n varje gång `map` eller `pos` ändras.

Pilarna är helt svarta (`stroke:#1d1f20`) och ligger **bakom** matrisinnehållet (`z-index` lägre än korten) men **framför** bakgrunden, så de aldrig skymmer text. En streckad linje i en ljusare blå ton (`#5980a6`) visar den pågående dragningen innan den släpps.

Sektionen delas i två rader (Konsekvens, Frekvens/sannolikhet) med egna mätta noder — mappningarna för de två axlarna är helt oberoende av varandra.

## 4. Flik 1b — Matris mot matris

Layout: två fullständiga matris-rutnät (den gamla uppe till vänster, den nya nere till höger), där varje matris behåller sin egen form (rader × kolumner, färgade celler) — inte omvandlad till listor. Pilar går direkt mellan axelrubrikerna (inte mellan enskilda celler).

- Gamla matrisen: kolumner = frekvensaxeln (kort kod, t.ex. siffra), rader = konsekvensaxeln (kort kod, bokstav) med de färgade riskcellerna till höger om varje radrubrik. Cellfärg slås upp via `OLDCOL[OLDGRID[colIndex][rowIndex]]`.
- Nya matrisen: kolumner = frekvensaxeln (grekisk bokstav), rader = konsekvensaxeln (siffra), celler färgade via `NEWCOL[NEWGRID[colIndex][rowIndex]]` och visar textetiketten `NEWTXT[...]` (Low/Moderate/High/Catastrophic).
- Axelrubriker (både rad- och kolumnrubriker) är klickbara/dragbara precis som i 1a — samma `data-node`/`data-anchor` + mätningssystem, bara `anchor="top"` för kolumnrubriker och `anchor="left"` för radrubriker.
- Pilarna ritas i samma SVG-overlay-teknik som 1a, med svart färg och en pilspets (SVG `<marker>`, triangel, `fill:#1d1f20`) i änden — eftersom detta är matris-mot-matris behövs pilspetsar tydligare än i listvyn.
- Använd `fan = index * 13` i `curve()` för konsekvensradernas pilar (flera pilar som startar nära varandra på vänstra matrisens rader) så de separeras visuellt istället för att overlappa.
- OBS: axeltilldelningen (vilken axel som visas som rader vs kolumner) styrs av samma "X = Konsekvens / X = Sannolikhet"-väljare som i 1a — koden ska vara generisk nog att axlarna kan bytas plats utan att skriva om logiken (håll en variabel som pekar på vilken rådata-array som för tillfället är "X-axeln" respektive "Y-axeln").

## 5. Delad topplist / verktygsfält (ovanför flikarna, gäller båda)

- Segmenterad kontroll: "X (A–E) = Konsekvens" / "X (A–E) = Sannolikhet" — byter tolkningen av den gamla matrisens axlar. Att byta nollställer `map` (för att undvika felaktiga kvarvarande mappningar mellan fel axlar).
- Framstegstext: "`N` av `M` gamla steg mappade" (M = totalt antal steg över båda axlarna).
- Knapp "Föreslå automatiskt".
- Knapp "Rensa".

## 6. Popup-struktur

- Modal/popup med två flikar i huvudet: **"Kopplingsfält"** (1a) och **"Matris mot matris"** (1b).
- Verktygsfältet i punkt 5 ligger ovanför flikinnehållet och är gemensamt (byts inte ut när man byter flik).
- State (`map`, `armed`, `drag`, axelval) ligger på popup-nivå, inte per flik, så byte av flik aldrig tappar en påbörjad mappning.
- Vid stängning av popup: exponera den färdiga `map` (gammalt steg-id → nytt steg-id) till HAZOP-programmet så den kan användas för att migrera befintliga riskbedömningar.

## 7. Styling-riktlinjer

- Chips/rutor: fyrkantiga hörn (ingen border-radius), 1px kantlinje, vit bakgrund i vila.
- Statusfärger: `armed` (valt, väntar mål) = mörkblå kant/bakgrund (`#1d2d3d` kant, ljusblå bakgrund); `mapped` = accent-färg (använd programmets accent-token); `idle` = neutral grå kant.
- Pilar: alltid svarta (`#1d1f20`), 1.5px, ingen skugga.
- Typsnitt: monospace för kod-etiketter (7px-skala), normal brödtext för beskrivningar, versaler+bokstavsavstånd för sektionsrubriker — följ HAZOP-programmets befintliga typografi istället för att införa nya fontval.
