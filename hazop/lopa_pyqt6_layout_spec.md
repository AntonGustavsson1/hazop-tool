# LOPA — Responsiv layoutspecifikation för PyQt6

Referens: 1920×1080 desktop. Mål: en normalt ifylld LOPA ska rymmas på en skärm utan global horisontell scroll; endast tabeller får egen intern scroll.

## Globalt

- Endast huvudytan (main) scrollar vertikalt på sidnivå. Horisontell scroll får aldrig uppstå globalt — bara lokalt inuti en tabell-wrapper vid för många kolumner.
- Sidomeny (Analyser) är sticky under nav-baren.
- Tabeller (barriärer, eskalering, kommentarer) har sticky header + egen `overflow-y: auto` inom en maxhöjd.

## Blocktabell

| Block | Placering | Bredd % (1920px) | Höjd | Min/Max | Beteende <1400px |
|---|---|---|---|---|---|
| Nav (topbar) | Helbredd, fixed top | 100% | 56px fast | min-height 48px | Knappar (Skriv ut/Exportera/Spara) kollapsar till ikoner |
| Sidomenu (Analyser) | Vänster, sticky | 10–12% (≈200px fast) | auto, sticky top | min-width 160px / max-width 220px | <1024px: kollapsar till hamburger/overlay |
| Huvudyta (main) | Höger om sidomeny | resterande ≈88% (max-width 1400px, centrerad) | auto, vertikal scroll på sidnivå | — | <1400px: max-width = 100% - sidomeny, mindre padding |
| Header-kort (LOPA-nr, Rev, Datum, Utförd av) | Helbredd i main | 100% | auto (~180–220px) | min-height 160px | Fält-grid 4 kol → 2 kol <900px → 1 kol <600px |
| Givardel-block | Sida vid sida med Manöverdel (50/50) | 50% | auto, intern scroll om >4 grupper | min-height 140px / max-height 320px, overflow-y:auto | <900px: staplas 100% var |
| Manöverdel-block | Sida vid sida med Givardel (50/50) | 50% | samma | samma | samma |
| Scenario-kort | Helbredd | 100% | auto, konsekvenslista intern scroll | min-height 260px, lista max-height 220px overflow-y:auto | Helbredd på alla skärmstorlekar |
| Oberoende barriärer (tabell) | Helbredd, hopfällbar | 100% | max-height 420px, sticky header, intern vertikal scroll | min 2 rader synliga utan scroll | Kolumner krymper proportionellt; ev. lokal horisontell scroll i tabell-wrapper om barriärtyper >6 |
| Eskalering (tabell) | Helbredd, hopfällbar | 100% | max-height 300px, sticky header, intern scroll | min 3 rader synliga | Samma princip som ovan |
| Kriterium + SIL-resultat | Sida vid sida (66/34) | 66% / 34% | auto (~120px) | min-height 100px | Staplas <900px |
| Fritext-kort (Åtgärder/Övriga krav + processäkerhetstid) | Sida vid sida (66/34) | 66% / 34% | auto (~150px) | min-height 130px | Staplas <900px |
| Kommentarer | Helbredd, sist | 100% | lista max-height 240px, intern scroll | min-height 160px | Textarea + knapp staplas alltid |

## Brytpunkter

- **1400px** — main tappar centrerad max-width, mindre padding.
- **1200px** — Scenario / Konsekvens-summering staplas vertikalt.
- **1024px** — sidomeny blir overlay/hamburger.
- **900px** — Givardel/Manöverdel och Kriterium/SIL staplas.
- **600px** — header-fält går till 1 kolumn.

## Implementationsnoter för PyQt6

- Använd `QSplitter`/`QHBoxLayout` med stretch-faktorer för 50/50, 60/40, 66/34-delningarna (sätt `setStretch` proportionellt, inte fasta pixlar) så förhållandet håller vid fönsterändring ner till respektive brytpunkt.
- Byt layout (sida-vid-sida → staplad) genom att lyssna på fönsterbredd (`resizeEvent`) och flytta widgets mellan en `QHBoxLayout` och `QVBoxLayout`, eller använd `QStackedLayout` med två färdiga layouter.
- Tabeller: `QTableWidget`/`QTableView` med `setMaximumHeight()` satt till angiven max-height, `QHeaderView` sticky via `setSectionResizeMode` + fixed header (headers scrollar inte med `QTableView` by default — bekräfta genom att sätta scroll-area runt endast body).
- Sidomenyns sticky-beteende: placera i egen `QDockWidget` eller fast vänsterkolumn i `QSplitter` som inte scrollar med huvudytans `QScrollArea`.
