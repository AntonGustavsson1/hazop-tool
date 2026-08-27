# Codex-instruktioner för HAZOP-projektet

## Omfattning och prioritet

Dessa instruktioner gäller allt arbete under `hazop/`. Projektrotens
`AGENTS.md` beskriver främst SIL-kalkylatorn och ska inte användas som
HAZOP-specifikation när instruktionerna skiljer sig.

## Obligatorisk startkontroll

Innan du analyserar eller ändrar HAZOP-programmet:

1. Läs hela `CLAUDE.md`. Den innehåller projektets arkitektur, teststrategi,
   Git-arbetsflöde och kända PyQt6/SQLite-fällor.
2. Läs hela `NOTES.md` för aktuella beslut, användarpreferenser, kända
   begränsningar och nyligen genomförda ändringar.
3. Kontrollera `crashes/` efter nya kraschrapporter och jämför dem med vad
   som redan dokumenterats i `NOTES.md`.
4. Kontrollera `git status` och bevara alla befintliga användarändringar.

Om äldre dokumentation avviker från aktuell kod, tester eller `NOTES.md`,
ska den aktuella verifierbara implementationen och de senaste besluten väga
tyngst. Uppmärksamma avvikelsen i stället för att tyst anta att den äldre
beskrivningen fortfarande gäller.

## Arbetsregler

- Följ lager- och återexportarkitekturen i `CLAUDE.md`; undvik nya cirkulära
  importer och uppdatera patchmål i tester när kod flyttas mellan moduler.
- Var särskilt försiktig med sammanslagna `QTableWidget`-celler,
  signal/slot-undantag och andra dokumenterade PyQt6-fällor.
- Lägg till eller uppdatera regressionstest för varje beteendeförändring.
- Efter kodändringar: kör syntaxkontroll, `tests.test_smoke` och relevant
  modul- eller integrationstest. Kör hela sviten endast vid större eller
  riskfyllda ändringar, enligt teststrategin i `CLAUDE.md`.
- Uppdatera `NOTES.md` efter varje betydelsefull ändring med beslut,
  verifiering och eventuella kvarstående begränsningar.
- Följ Git-reglerna i `CLAUDE.md`. Lägg aldrig till projektdata eller
  genererade filer såsom `*.db`, `*.pdf`, `*.xlsx`, `__pycache__/`,
  `build/` eller `dist/` i en commit.
- Efter varje färdig, meningsfull ändring ska Codex köra relevanta tester,
  uppdatera `NOTES.md`, committa endast de avsedda käll-/testfilerna och
  pusha committen till aktuell upstream-gren innan arbetet lämnas över.
  Detta gäller utan att Anton behöver be separat om commit/push varje gång.
  Om tester, mergekonflikt, autentisering eller nätverk hindrar push ska Codex
  inte dölja det: lämna inga halvfärdiga commits och redovisa exakt vad som
  återstår innan Anton byter mellan Codex och Claude.
- Codex får aldrig ta med orelaterade befintliga ändringar eller ospårade
  projekt-/indatafiler i en sådan commit. Kontrollera `git status` före
  staging och kontrollera efter push att lokal gren matchar upstream.
- Skilj alltid mellan granskad kod, godkända automatiska tester och ett
  visuellt verifierat programflöde. Påstå inte att GUI-beteendet är visuellt
  verifierat om endast headless-tester har körts.
