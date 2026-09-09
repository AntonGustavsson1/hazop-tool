# Wordrapport från HAZOP

Välj **Export → Exportera Word-rapport…**. Rapportdelen är stående A4;
worksheet och rekommendationer är liggande A3 som standard, med A4 som val.
Programmet skapar en ny DOCX från en skrivskyddad ögonblicksbild av studien.
Uppdatera innehållsförteckningen i Word med Ctrl+A och F9 om Word frågar.

## Rapportmall och datakällor

Förlagan `2x20xx-Report-01 HAZOP_svenska_utan SIL.docx` är oförändrad.
Exporten använder den rensade, inbyggda ProSa-rapportstommen för framsida,
dokumentblad (sida 2), sidhuvud, sidfot, Aptos-brödtext och gröna rubrikformat.
Rapportens struktur har renodlats till dokumentstyrning, sammanfattning,
inledning, referenser, deltagare, riskbedömning, resultat samt fyra bilagor.
Sammanfattningen anger antal registrerade analystillfällen och noder och visar
noderna i en numrerad lista. Metoden redovisas i avsnitt 3.1 i dåtid som en
beskrivning av hur studien har genomförts. Bilaga 1 samlar registrerade
avvikelser och rapportens förkortningar. Tabellhänvisningar ligger i de
underkapitel där tabellerna introduceras.
Gamla kundnamn, rapportnummer, slutsatser, standardhänvisningar och
projektspecifika antaganden återanvänds inte som uppgifter om en ny studie.
Ingen SIL-klassning eller riskacceptans härleds automatiskt.

| Rapportdel | Källa i programmet | Hantering |
| --- | --- | --- |
| Försättsblad och dokumentstyrning | Projektuppgifter, egna fält och revisioner | Saknade värden gulmarkeras |
| Inledning och slutsatser | Projektets egna fält enligt listan nedan | Ingen automatisk slutsats om säkerhet |
| Referensdokument | P&ID-bladens ritningsnummer, namn, revision, datum | PDF-sida anges som spårbarhet |
| Deltagare och genomförande | Metodbeskrivning, deltagare, egna kolumner, analystillfällen och närvaro | Metoden skrivs i dåtid; ej registrerad närvaro skiljs från frånvaro |
| Riskbedömning | Studiens riskmatris, skalor, färger och kategoridefinitioner | Samma X-/Y-axel och visningsriktning som i programmet |
| Bilaga 1 Avvikelser och förkortningar | Studiens registrerade avvikelser och rapportens återkommande förkortningar | Kompletterar metodbeskrivningen i avsnitt 3.1 |
| Bilaga 2 Noder | Nodbeskrivning, system, driftdata, P&ID-kopplingar och status | Nodritningar infogas manuellt på gulmarkerad plats |
| Bilaga 3 HAZOP-protokoll | Samma rader och tabellbyggare som Word Worksheet | Samma ordning, kolumner, sammanslagningar och riskfärger |
| Bilaga 4 Rekommendationer | Samma tabellbyggare som rekommendationsexporten | Alla rekommendationer och länkade positioner |

Referenserna följer `studie.nod.avvikelse.orsak.konsekvens`. Upprepade
avvikelsebeskrivningar använder worksheetens gemensamma visningsnummer även
i den fristående rekommendationsexporten. Tomma fortsättningsceller i
sammanslagna tabeller gulmarkeras inte. Saknad beskrivning eller riskbedömning
för en faktiskt registrerad konsekvens markeras däremot.

## Egna fält som fyller rapporten

Lägg till dessa namn under **Projekt → Egna fält** efter behov. Namnen matchas
utan hänsyn till stora/små bokstäver eller omgivande blanksteg. Motstridiga
värden för samma namn ger ett kompletteringsfält.

- Rapportnummer, Rapportdatum, Rapportrevision, Rapportstatus, Distribution
- Framtagen av, Kvalitetsgranskad av, Godkänd av
- Uppdragsansvarig, Kontaktperson kund, Kontaktuppgifter kund
- Kontorsadress ProSa, Kontaktuppgifter ProSa, Kundadress
- Bakgrund, Syfte, Omfattning, Avgränsningar, Driftfall, Analysförutsättningar
- Övriga referensdokument, Metodreferens
- Riskacceptanskriterier, Frekvensunderlag, Barriärunderlag
- Resultat och slutsatser, Uppföljning

Övriga egna fält tas med under dokumentstyrning. Rapportrevision kan även
hämtas från projektets senaste registrerade revision. Framtagen, granskad och
godkänd fylls i som egna fält; en deltagares roll tolkas inte som ett godkännande.

`[KOMPLETTERA: …]` har gul textmarkering. I den smala frekvenskolumnen används
gulmarkerat `[?]`, förklarat i bilagans inledning. Riskmatrisens gula cellfärg är
riskklassning, inte ett kompletteringsfält. Befintliga tomma valfria barriär-
och rekommendationsceller fylls inte med påhittade uppgifter.

## Redigering och begränsningar

Rapporten är redigerbar i Word. Manuella ändringar i en exporterad DOCX eller
i `HAZOP_standardmall.docx` läses **inte** tillbaka av programmet; framtida
exporter bygger på programdata, den inbyggda ProSa-stommen och
rapportstrukturen i `report_word_export.py`.
Återkommande projekttexter bör därför fyllas i som egna fält.

P&ID-bilder och visuella nodgränser kopieras ännu inte automatiskt. Bilaga 2
har uttryckliga gula platser för dessa. Kontrollera och komplettera rapporten
innan den granskas, godkänns eller skickas. Registrerade poster och stängda
rekommendationer innebär inte automatiskt att studien är färdig eller att
kvarvarande risk accepterats.
