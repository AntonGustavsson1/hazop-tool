# LOPA — responsiv layout för PyQt6

## Grundprincip

LOPA är ett sammanhängande arbetsblad. Dokumentets huvudvy är den enda
vertikala rullytan. Tabeller och flerradiga fritextfält visar hela sitt
innehåll och växer nedåt; rullhjulet över dem rullar samma sida. Listan över
LOPA-ark är en separat navigering i sidomenyn.

Huvudvyn ska inte behöva rulla i sidled. De vanliga tabellerna anpassar sina
kolumner till tillgänglig bredd och radbryter innehåll. HAZOP-hierarkins
konsekvenskategorier ligger i separata kolumner när de ryms, och samlas i en
namngiven kategorikolumn när vyn är smal. Eskaleringsfaktorerna flyttas till
fler rader inom sin cell utan att pågående inmatning förloras.

## Sidans ordning

| Del | Layout och beteende |
| --- | --- |
| LOPA-ark | Sidomeny vid bred vy; öppnas via `LOPA-ark` vid smal vy. |
| Huvud och revision | Fält i fyra, två eller en kolumn efter fönsterbredd. |
| Givardel och manöverdel | Sida vid sida på bred skärm; staplade under 900 px. Medlemsrader visas i sin helhet. |
| Scenario | En rad per konsekvens i HAZOP-hierarkin, redigering på plats och scenariotext som växer med innehållet. |
| Oberoende barriärer | Fyra kolumner med sammanhållen barriärtext per källa. Detaljredigering kan fällas ut, med rader i samma sidflöde. |
| Eskalering | Kategori, konsekvens, TEL, redigerbara faktorer och beräknat resultat. Faktorer radbryts vid behov. |
| Beräkningsöversikt, dokumentuppgifter och kommentarer | Tre staplade avsnitt. Tabellrader och längre dokumenttexter får sin faktiska höjd. |

## Brytpunkter

- Under 1024 px döljs sidomenyn tills `LOPA-ark` öppnas.
- Under 900 px staplas givar- och manöverdel.
- Under 600 px visas huvudets fält i en kolumn.
- HAZOP-kategoriernas kompakta läge väljs efter tabellens faktiska
  tillgängliga bredd och kategoriernas namn, inte en fast fönsterbredd.

Tabellernas höjd beräknas om när rader eller kolumnbredder ändras. En öppen
scenarioredigerare behålls vid storleksändring och byter tabelläge först när
redigeringen avslutas. Testdata har granskats vid 600–1500 px fönsterbredd;
fler rader innebär mer vertikal sidrullning.
