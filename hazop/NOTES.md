# NOTES.md — Beslut och kontext

> Denna fil uppdateras automatiskt av Claude Code efter varje session.

## Gruppobjekt: radvis ordning och operatorer (2026-08-28)

Knappen för att byta primär/sekundär är borttagen från objekttaggs-popupen.
Högerklick på en faktisk grupptagg visar i stället `Flytta uppåt` och
`Flytta nedåt`. För rad 2 och framåt finns även en separat koppling till
föregående rad: `&`, `OR` eller `->`. Äldre grupper med en gemensam operator
visas fortfarande korrekt; den nya representationen sparas som operatorer
mellan taggarna i `comp_tag`, exempelvis `A -> B OR C`.

## Fritt höjbar HAZOP Scenario-yta (2026-08-28)

Den yttre vertikala splittern låter nu HAZOP Scenario växa med fönstret och
dras upp med splittern utan en konstgjord maxhöjd. Panelens befintliga
minimihöjd och P&ID-vyns övriga layoutlogik är kvar. Övre panelens
vertikala storlekspolicy är ignorerad så att dolda höga sidor i
QStackedWidget inte blockerar splittern när P&ID-sidan visas.

## Kompakt dragbild i HAZOP Scenario (2026-08-28)

Dragning av Orsak, Konsekvens eller Barriär visar nu en liten etikett med
åtgärd, celltyp och förkortad text. Den tidigare dragbilden var en skärmbild
av hela cellen och blev därför oproportionerligt hög för radbrutna eller
grupperade celler. Drop-payload och flytta/kopiera-beteende är oförändrade.

## Flera fält via Shift-drag i HAZOP Scenario (2026-08-28)

Scenario-tabellen använder nu utökad cellmarkering. Flera Orsaker, Konsekvenser
eller Barriärer i samma kolumn kan markeras med Shift och dras som ett paket.
De skickas i en separat multi-payload och behandlas sedan av samma befintliga
flytta/kopiera-regler som en enskild post. Blandade kolumntyper samlas inte
automatiskt ihop.

## En rekommendations-popup åt gången (2026-08-28)

Rekommendationsrutan som öppnas vid inline-redigering skyddas nu mot dubbla
fördröjda öppningar. Varje editor får en generationsmarkör och en gammal
assist-popup stängs innan den aktuella visas.

## Objektval i trädet visar bara objektets scenario (2026-08-28)

När en enskild Orsak/objektrad väljs i trädet laddas nu just den orsaken med
dess konsekvenser och barriärer. Den gemensamma avvikelsen laddas inte längre
som filter, eftersom det annars visar alla objekt som delar samma avvikelse.

## Tidigare konsekvenser och rekommendationer vid redigering (2026-08-28)

Konsekvenseditorn visar nu historik-popupen explicit även för den multiline-
editor som används i scenariotabellen. Konsekvenser matchas från första
tecknet, medan rekommendationspopupens katalog filtreras på text var som
helst i rekommendationen när användaren skriver. Nummerdelen `R-xxx` ingår
inte i rekommendationssökningen.

## Läsbarare tunna P&ID-linjer (2026-08-28)

P&ID-rastervisningen kan nu förstärka mycket tunna streck med en mild
bildfilterbaserad minsta bredd. Standard är nu 2 px förstärkning per sida,
eftersom första sidan i Sunpine-referensen använder cirka 0,71 pt (0,25 mm)
linjer som blir subpixel-tunna vid översiktszoom.
Funktionen styrs av `P&ID-inställningar` → `P&ID-linjer`, där den kan
stängas av eller justeras mellan 1 och 4 px. Original-PDF, PDF-export och
alla sparade PDF-koordinater påverkas inte; endast skärmrasterbilden byggs om.

## Ramfri inline-redigering (2026-08-28)

Inline-editorn i scenario-tabellen visar inte längre en extra ram eller
fokusram ovanpå cellen. Gruppens primär/sekundär-radval och sparlogik är
oförändrade.

Dubbelklick under sekundärradens visuella område ignoreras nu, så det kan
inte längre öppna den vanliga fullcells-editorn.

Även den tunna vertikala marginalen ovanför första gruppobjektet är nu
passiv; bara den målade textremsan på en objektrad kan starta redigering.

Dubbelklick i gruppens övriga cellområde konsumeras nu redan i eventfiltret,
så Qt:s generella fullcells-editor kan inte öppnas där heller.

Generell gruppredigering är dessutom blockerad via F2, högerklickets
Redigera och programmatisk redigeringsstart. Grupporsaker kan därmed bara
redigeras genom primär- eller sekundärradens egna textområden.

## Flerobjektsgrupper (2026-08-28)

Grupper kan nu innehålla upp till 20 P&ID-objekt. De extra objekten sparas i
en utökad gruppreferens medan de äldre primär/sekundär-fälten behålls för
bakåtkompatibilitet. Varje objekt får en egen visuell rad och egen inline-
redigering med samma tomma start som sekundärraden.

Gruppoperatorn visas nu även separat i objekt-popupen, till exempel
`Gruppkoppling: &`, medan taggfältet fortfarande bara avser det klickade
objektet.

## Gruppoperator vid dragning av flera objekt (2026-08-28)

Vid dragning av flera P&ID-objekt till en avvikelse visas val för AND, OR
eller Chain. De sparas som `&`, `OR` respektive `->` mellan objekttaggarna.
Separata orsaker kan fortfarande väljas.

`Chain` är nu förvalt i frågan. Användaren kan fortfarande välja AND eller OR
innan gruppen skapas.

## Grupporsak: primäreditor tar inte med sekundärhändelsen (2026-08-28)

Äldre grupporsaker kan ha båda händelserna lagrade som en enda piltext.
Editorns inläsning delar nu denna representation innan den valda raden visas,
så primär- och sekundärtexten hålls separata även vid redigering av äldre data.
Verifierat med riktade gruppeditor-tester och smoke-test.

## Dragning till avvikelse: ingen förifylld orsaksbeskrivning (2026-08-28)

När P&ID-objekt dras till en avvikelse skapas orsaken nu med tom beskrivning.
Även den automatiska kontroll/ventil-gruppen behåller sina objektkopplingar
men får ingen påhittad felmekanism eller effekttext. Primär- och
sekundärtext skrivs därmed av användaren. Verifierat med riktade dragtester,
smoke-test och trädpaneltest.

## Grupporsak: editor placeras på rätt visuell rad direkt (2026-08-28)

Gruppeditorns `group_line` sätts nu redan när editorn skapas, före Qt:s första
geometriberäkning. Det förhindrar att redigering av sekundärraden först placeras
överst i cellen. Primär/sekundär-val, textinnehåll och sparlogik är oförändrade.
Verifierat med syntaxkontroll, smoke-test samt riktade primär- och sekundärradtester.

## Inline-editor: bevara markör och scrolläge vid liveformatering (2026-08-28)

Den gemensamma QTextEdit-editorn återställer nu hela markörtillståndet,
inklusive eventuell markering samt vertikalt och horisontellt scrolläge, efter
att P&ID-taggar fetmarkeras. Detta minskar risken att texten hoppar visuellt
under pågående redigering utan att ändra innehåll, radval eller sparlogik.
Smoke-test och riktat regressionstest passerade. Hela Scenario-sviten hann inte
slutföras i denna körning.

## Gruppeditor: dölj aktiv radens spöktext (2026-08-28)

När en primär- eller sekundärrad redigeras undertrycks den statiskt målade
beskrivningen på just den raden, medan den andra grupp-raden ligger kvar som
kontext. Editor- och cellrektangeln jämförs efter explicit mappning till
viewportens koordinatsystem. Verifierat med riktat gruppeditorstest,
smoke-test och syntaxkontroll.

## Grupporsak: sekundärtagg kvar vid ny primärtext (2026-08-28)

En ny grupp kan ha primärtext sparad innan sekundärtexten hunnit skapas.
Renderingen kompletterar då den saknade andra raden med sekundärtaggen, så
den inte försvinner när primärorsaken öppnas för redigering. Verifierat med
ett särskilt integrationstest för detta nygruppsfall.

## Grupporsak: primärtagg kvar efter sekundärredigering (2026-08-28)

Vid sparning av en sekundärrad fylls en tom, orörd grupprad nu med sitt
kopplade objekttaggvärde. Därmed kan primärobjektet inte försvinna när en ny
grupps sekundärtext redigeras och editorn stängs. Verifierat med ett riktat
test för ny grupp samt befintliga grupp- och smoke-tester.

## Grupporsaker i vänsterträdet är inte fetstilta (2026-08-28)

Grupporsakens trädtext använder nu normal font precis som en vanlig orsak.
Fetstil på nod- och övriga hierarkinivåer är oförändrad. Verifierat med
riktat trädtest, smoke-test och syntaxkontroll.

## Grupporsakens objekttagg öppnar samma taggpopup (2026-08-28)

Klick på primär- eller sekundärtaggen i en grupporsak öppnar nu samma
kompakta `CauseTagPopup` som en vanlig orsak. Popupen får samtidigt rätt
objektposition, så en ändring av sekundärtaggen påverkar inte primärobjektet;
fritextklick fortsätter till inline-redigering. Verifierat med riktade popup-
och gruppeditor-tester samt smoke-test.

Grupporsakens primär- och sekundärtagg kan nu öppna taggpopupen direkt från
sin respektive visuella rad.

## Grupporsak: operator före sekundärtagg (2026-08-28)

Sekundärraden visar nu ett klickbart operatorfält med standardvärdet `OR`.
Via fältet kan användaren välja `&`, `OR` eller `->`; valet sparas i den
befintliga gruppens taggmetadata och påverkar inte primär/sekundär-länkarna.
Verifierat med riktade operator- och popup-tester samt smoke-test.

## Objekt kopplat till grupperad avvikelse markeras och numreras rätt (2026-08-28)

En avvikelse som visas som gemensam guideordsrad kan ha en objektspecifik
syskonrad i databasen. Tree-context-scope inkluderar nu alla sådana syskon
med samma text i samma nod, och Scenario-numret delas mellan dem i stället
för att använda syskonets råa databasposition. Verifierat med databas-,
integrations-, scenario- och smoke-tester.

## Grupporsak: sekundärredigering sparas inte som primär (2026-08-28)

Vid fokusförlust efter redigering av sekundärraden normaliseras editorns
`group_line` innan sparning. Därmed sparas texten på rätt rad även när Qt
levererar radvärdet som en annan numerisk typ. Primär/sekundär-logiken i
övrigt är oförändrad. Verifierat med riktat dubbelklickstest och smoke-test.

## Grupporsak: sparad beskrivning visas i Scenario-cellen (2026-08-28)

Grupp-renderingen visade tidigare endast de två objekttaggarna när
`group_choices_set` var noll, även om en beskrivning redan fanns sparad och
syntes i trädet. Villkoret visar nu bara bare taggar för en verkligt tom grupp;
en befintlig beskrivning renderas direkt i cellen. Primär/sekundär-radernas
val- och sparlogik är oförändrad. Verifierat med syntaxkontroll, smoke-test
och grupprelaterade integrationstester.

## Grupporsak: liten extra marginal före fri text (2026-08-28)

Editorns horisontella startposition flyttas endast 5 px åt höger efter den
aktuella radens objekttagg. Samma geometriregel gäller primär och sekundär
rad; radval, felhändelser och sparlogik är oförändrade. Verifierat med
syntaxkontroll, smoke-test och riktat grupp-radtest.

## Grupporsak: objekttaggar öppnar inte längre Primär/Sekundär-popup (2026-08-28)

Objekttaggarna i grupporsakens Orsak-cell är nu presentation-only. Klick eller
dubbelklick på taggen öppnar inte längre de radstyrda Primärhändelse- eller
Sekundärhändelse-popupfönstren; dubbelklick fortsätter i stället till samma
inline-redigering som fritextdelen. Verifierat med syntaxkontroll, smoke-test
och grupprelaterade integrationstester.

## RRF-popup: positiv tillämpningslogik (2026-08-28)

RRF-popupens kategori- och orsaksval visar nu "Gäller för" och är förvalda
som aktiva. Användaren avmarkerar de kategorier eller orsaker där barriären
inte gäller; databasen fortsätter lagra dessa som exkluderingar.

## Deltagarmatris: snabbmarkering per analystillfälle (2026-08-28)

Deltagarmatrisen visar nu en separat snabbknapp under varje analystillfälle.
Knappen markerar eller avmarkerar alla deltagare för just det tillfället och
behåller den befintliga individuella kryssrutan per deltagare.

## Global sökning i P&ID-text (2026-08-28)

Ctrl+F har nu kryssrutan "Sök i text på P&ID". När den är aktiv läses
textlagret i den aktuella PDF:en med UTF-8-bevarande och träffar visas per
sida som skyddade sökträffar.

## Excel-export: valfri sammanslagning och rekommendationslista (2026-08-28)

Excel-exporten frågar nu om identiska intilliggande trädvärden ska slås ihop
och kan slå ihop Nod, P&ID, Orsak, Konsekvens, Safeguards och Åtgärder utan att
slå ihop riskdata. Exportmenyn har dessutom en separat Åtgärder (Excel)-export
för den globala rekommendationslistan.

## Agent 3: rekommendationslistans datum och stabila nummer (2026-08-28)

Rekommendationens R-nummer är nu låst i översiktslistan och fältet "Ska
vara åtgärdat" använder en interaktiv kalender med ISO-datum. Ändringen
behåller redigering av rekommendationstext i HAZOP-scenariot.

## Kraschskydd för tagg-completer och gruppfält (2026-08-28)

Fördröjd taggmatchning avbryts nu säkert om inline-editorn har hunnit tas
bort av en rebuild eller fokusändring. Gruppens tagglista normaliseras från
`None` till en tom lista innan den används. Rekommendationer som råkar
importeras som HTML-dokument rensas vid både skapande och uppdatering, medan
databasen fortsatt lagrar vanlig UTF-8-text.

## Kompaktare riskkolumner och automatisk fyllbredd (2026-08-28)

Kolumnerna Risk före barriär och Slutkonsekvens har smalare standardbredd.
Risktexten centreras i hela cellen utan reserverad ikonmarginal. Fyll bredd
körs automatiskt när panelen startar efter att en riktig viewportbredd finns,
men dess programmatisk breddjustering sparas inte som en manuell ändring.

## Matchande objekttaggar och frånkoppling i HAZOP-celler (2026-08-28)

Taggar som skrivs in i Orsak, Konsekvens eller Safeguard matchas mot aktuell
P&ID-katalog och fetmarkeras. Matchningen sparas som `tagged_refs` för
Konsekvens/Safeguard. Högerklick på en kopplad tagg erbjuder frånkoppling per
tagg; texten lämnas kvar men visas därefter som vanlig löptext.

## Objektuppgifter per blad (2026-08-27)

Noder-tabellen visar nu även objekttyp, hanterade avvikelser och antal per
objekt i separata kolumner. Raderna är fortsatt parallella med bladlistan.

## Kompakt konsekvensdefinitioner (2026-08-27)

Separatorn "Konsekvensdefinitioner" har tagits bort ur riskmatrisens grid.
Definitionstexterna börjar nu direkt efter matrisen, så långa beskrivningar
skapar inte längre ett extra tomt band längst ned.

## Objektdata per nodblad (2026-08-27)

Noder-tabellen visar nu per blad objekttagg, objekttyp, hanterade avvikelser
och antal avvikelser per objekt. Fälten ligger radvis så flera blad och flera
objekt kan läsas utan att informationen blandas ihop.

## Blad och objekt per nod (2026-08-27)

Noder-tabellen visar nu varje blad på egen rad och har en separat kolumn med
objekttaggar per blad i samma ordning. Objekt hämtas från nodkopplade
P&ID-markeringar samt orsaksobjekt under nodens avvikelser.

## Nodnummer och markerade blad (2026-08-27)

Noder-flikens första kolumn använder nu listans löpnummer (`Nod 1`, `Nod 2`)
i stället för databastabellens interna ID. Nodens egen yta räknas som markerad
om `markup_points` innehåller geometri, och visas då på nodens PDF-sida.

## Noder visar relevanta blad (2026-08-27)

Noder-fliken visar nu `Nod <löpnummer>` i första kolumnen. Bladkolumnen
innehåller endast blad där nodgrafik (inklusive redmarkeringar) finns eller
där ett P&ID-objekt är kopplat som orsaksobjekt på nodens avvikelser.

## Redigerbara ritningsuppgifter på Blad (2026-08-27)

Blad-fliken visar nu Ritningsnummer, Ritningsnamn, Revision, Datum och
PDF-sida i en tabell. De fyra första kolumnerna kan redigeras direkt och
sparas på samma bladpost som tidigare.

## Tom objektlista i Avvikelser & Orsaker (2026-08-27)

Den reducerade standardkatalogens dubblettstädning kunde lämna alla
standardavvikelser inaktiva trots att orsakerna fortfarande var aktiva. Vid
databasstart återaktiveras nu avvikelser som har aktiva standardorsaker.

## Justerbar matrisbredd (2026-08-27)

Riskmatrisens celler och konsekvensdefinitioner använder inte längre fasta
widgetbredder. Splitterbredden kan därför justeras åt båda håll utan att
matrisen låser sig på sin ursprungliga storlek eller hoppar tillbaka.

## Analystider och massmarkering av deltagare (2026-08-27)

Varje analystillfälle kan nu få start- och sluttid i dialogen för
sessiondetaljer. Tiderna sparas i `analysis_sessions` och visas i
deltagarmatrisens rubrik. En kryssruta under listan markerar eller avmarkerar
alla deltagare för det valda analystillfället; blandad närvaro visas som
delvis markerad.

## ToR och Report-flik (2026-08-27)

HAZOP Prep har fått en separat flik med dubbla signeringsfält för ToR och
Report: Framtagen av, Kvalitetsgranskad av och Godkänd av. Fälten använder
deltagare som förslag men är redigerbara kombinationsfält för fri text. Värdena
lagras per studie i `app_config`.

## Kompakt markering i HAZOP Scenario (2026-08-27)

Rekommendationsceller ritas nu med raka hörn, kompakt textyta och en smal blå
accent vid markering i stället för en rundad helcellsmarkering. Inline-editorn
har samma platta fokusram och mindre padding så att fler rekommendationsrader
kan läsas samtidigt.

## Generisk orsak eller bindning till P&ID (2026-08-27)

En orsak utan objekt kan nu redigeras direkt med ett vanligt klick för att
skriva en generisk orsak. Dubbelklick öppnar i stället objekt-dialogen där ett
befintligt P&ID-objekt kan bindas. Ett väntande enkelklick avbryts vid
dubbelklick så att endast en funktion öppnas.

## En enda funktion för ej kopplad orsak (2026-08-27)

En orsak utan P&ID-objekt startar inte längre den vanliga inline-editorn efter
ett enkelt klick. Det förhindrar att ett dubbelklick samtidigt öppnar både
inline-redigering och dialogen för `ej på P&ID`.

## Shift-drag till redigerad orsak (2026-08-27)

P&ID-markörer kan nu släppas direkt på den aktiva orsakseditorn. Eftersom
editorn är en QLineEdit ovanpå tabellen fångas drag/drop-händelsen där och
kopplar markörens `equipment_id`, tagg och objekttyp till orsaken.

## Rekommendationseditor: sqlite-rad normaliseras (2026-08-27)

Kraschen vid redigering av en redan länkad rekommendation berodde på att
`_prepare_recommendation_editor` anropade `.get()` direkt på en `sqlite3.Row`.
Raden konverteras nu till en dict innan beskrivningen läses.

## Kategorier placeras på rätt sida av riskmatrisen (2026-08-27)

När konsekvens är Y-axel ligger kategoriurvalet nu till vänster om matrisen
och C-rutorna delar samma höjdlinjer som matrisens konsekvensfält. När
konsekvens är X-axel ligger urvalet under matrisen och följer samma kolumner.
Rutorna använder samma storlek som matrisens celler.

## Flera taggersättningsregler (2026-08-27)

Taggidentifieringens inställning använder nu en rad per regel med X (ursprung)
och Y (ersättning). Plus lägger till en ny rad och minus tar bort raden;
reglerna sparas fortfarande i samma kompatibla semikolonformat.

## Konsekvenspopup följer riskmatrisens axel (2026-08-27)

Riskmatrisens konsekvensval per kategori orienteras nu efter aktuell matris:
vertikala C-rutor när konsekvens ligger på Y-axeln och horisontella C-rutor
när konsekvens ligger på X-axeln. Ordningen följer även eventuell omvänd axel.
Varje nivå har tooltip med den sparade konsekvensbeskrivningen.

## Flera orsaker från samma vänsterklickade objekt (2026-08-27)

Orsaksflödet från objektets avvikelse-popup använder nu objektets aktuella
`equipment_id` direkt för varje ny orsak. Detta gör att taggkopplingen inte
tappas efter den första valda avvikelsen.

## Tagg följer med på flera orsaker från objekt-popup (2026-08-27)

När flera avvikelser kryssas i efter vänsterklick på samma P&ID-objekt används
nu objektets aktuella `equipment_id` direkt vid varje orsaksskapande. Därmed får
andra och efterföljande orsaker samma koppling och tagg som den första.

## Rekommendationsnummer startar om per nytt projekt (2026-08-27)

Vid "Nytt projekt" rensas nu även SQLite:s AUTOINCREMENT-räknare för
rekommendationer. Första nya rekommendationen får därför åter `R-001`, även
när projektfilen måste återanvändas på grund av låsning eller OneDrive-synk.

## Redmark-symboler från kugghjulsknappen (2026-08-27)

Symbolväljaren för redmarkeringar ankras nu mot muspekaren när den öppnas från
kugghjulsknappen i den synliga nodmarkup-ribban. Den interna RedMarkupPanel är
dold och kunde därför tidigare ge en ogiltig popup-position utanför skärmen.

## Sökresultat visar hela PDF-sidan (2026-08-27)

När ett definierat objekt väljs i objektsökningen centreras rätt sida och
passas in i vyn med marginal. Träffen markeras fortfarande blå, men sökningen
zoomar inte längre tätt in på symbolen.

## Grupp av P&ID-objekt i orsak (2026-08-27)

Vid drop av flera objekt på en avvikelse frågar HAZOP-trädet om objekten ska
behandlas som grupp. Nej skapar separata orsaker. Ja skapar en enda funktionell
orsakskedja när ett styrande objekt och ett påverkat objekt kan identifieras;
annars används separata orsaker. Kedjan innehåller båda fetmarkerade taggarna
och beskriver felmekanismen, till exempel `Instrument felar högt → Ventil öppnar fullt`.

## Orsak utan P&ID-objekt (2026-08-27)

Orsaksfält utan kopplad utrustning visar nu **ej på P&ID**. Drag-and-drop från
P&ID till orsaksfältet visar och sparar objektet direkt. Dubbelklick på
placeholdern öppnar tagg-/typdialogen med knappen **Bind till objekt på P&ID**;
därefter väljs ett befintligt objekt genom klick i P&ID-viewern.

## Direkt drop till tom safeguard (2026-08-27)

P&ID-utrustning kan nu dras direkt till en tom safeguard/barriär-rad efter att
en konsekvens skapats. Om raden saknar safeguard-id skapas barriären under
den aktuella konsekvensen automatiskt och får den släppta objektkopplingen.

## Flera öppna avvikelser för samma P&ID-objekt (2026-08-27)

När flera avvikelser, till exempel Lågt flöde och Högt flöde, kryssas för samma
objekt öppnas alla berörda avvikelsegrenar i trädet. Den andra grenen ersätter
inte längre den första efter popupens uppdatering.

## Flera orsaker från P&ID-objekt (2026-08-27)

Varje orsak som skapas genom att kryssa en avvikelse i objektets popup kopplas
nu till samma `equipment_id` som P&ID-markören. Orsaksbeskrivningen lämnas tom
så att användaren själv kan skriva den i HAZOP Scenario; objekttagg och typ
fylls fortfarande i automatiskt.

## Tree-context färgfilter på P&ID (2026-08-27)

När en avvikelse är vald på HAZOP-trädet markeras utrustning i dess orsaks-,
konsekvens- och safeguard-gren. Aktiv knapp ger vald färg; avmarkerad roll
visas grått i stället för att tas bort. Utrustningsknappen döljer fortsatt
objekten helt.
> Den bevarar beslut, avvägningar och uppskjutna funktioner som inte framgår av koden eller git-historiken.
> Sessionsloggar äldre än 2026-08-17 flyttades till `NOTES_ARCHIVE.md` (2026-08-20) för att hålla denna fil kort — den läses i sin helhet varje session (se CLAUDE.md).

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
**Migration:** Gamla `consequences.likelihood`-kolumnen droppas nu automatiskt från äldre databasfiler vid öppning (2026-08-09, se nedan) — inget kvarlämnat att städa manuellt.

### RRF på Safeguard reducerar likelihood
**Beslut:** RRF (Risk Reduction Factor) på en safeguard reducerar sannolikheten med `floor(log10(rrf))` steg.
**Skala:** RRF 10 = −1 steg, RRF 100 = −2 steg, RRF 1000 = −3 steg.
**Varför:** Följer IEC 61511 / SIL-konventionen där PFD ≈ 1/RRF.

### Riskmatris lagras som JSON i app_config
**Beslut:** Riskmatrisen (färger, etiketter, storlek, axelriktning) sparas som JSON under nyckeln `'risk_matrix'` i `app_config`-tabellen.
**Varför:** Flexibelt — användaren kan konfigurera valfri matrisstorlek (2×2 till 10×10) och färgsättning utan kodändring.

### Tvåfilsstruktur → fyra moduler (2026-08-06, uppdaterat)
**Ursprungligt beslut:** Koden delades i `hazop.py` (huvudfönster + DB + panels) och `pid_viewer.py` (P&ID-canvas + skanning).
**Uppdaterat beslut:** Efter Fas 1+2 samt kvällens ventildetekterings-fixar (självkorsande quads, rörledningsnät, bihängda delar, dubbelrenderad tagg-text) hade den icke-Qt-beroende analyslogiken i `pid_viewer.py` vuxit så mycket att Anton själv föreslog att bryta den ut, innan den blir ännu större när "instrument etc." byggs ut. Bröt ut i en NY, fristående modul: **`equipment_detection.py`** — all PDF/tagg/ventilanalys utan Qt-beroende (`scan_pdf_for_equipment`, `detect_equipment_and_valves`, `find_valve_shapes`, `detect_equipment_symbols`, `associate_tags_to_clusters`, `trace_line_info_for_cluster`, OCR-motorwrapprarna, `KNOWN_PREFIXES`/`COMPONENT_TYPES`/`VALVE_COMPONENT_TYPES` m.fl.) — samma princip som `symbol_geometry.py` redan följde ("Pure Python/PyMuPDF — no Qt dependency... kan importeras fristående utan GUI-stacken").

**Vad som INTE flyttades** (medvetet, annan analysdomän): sheet-connector/media-färgläggningssystemet (`_sheet_ref_variants`, `_detect_dialect`, `_propose_layout`, `_MEDIA_*`/`_RE_*`/`_DIALECTS`) och röd-markup-SVG-ikonuppslaget (`_get_red_symbol_svg`/`_RED_MARKUP_SYMBOLS`) — orelaterat till ventil-/utrustningsdetektering, ligger kvar i `pid_viewer.py`. `ensure_ocr_available()` ligger också kvar (visar `QMessageBox`, det enda OCR-relaterade som genuint behöver Qt) men anropar `equipment_detection.ocr_status()` för själva tillgänglighetskollen.

**Genomförande:** `pid_viewer.py` importerar och **re-exporterar** alla namn `hazop.py` redan importerade direkt (`from pid_viewer import KNOWN_PREFIXES, scan_pdf_for_equipment, ...`) — så `hazop.py` behövde INGA ändringar. `test_regression.py`s importer av de flyttade funktionerna pekar nu direkt mot `equipment_detection` istället för `pid_viewer` (bevisar att modulen verkligen fungerar fristående, inte bara att den råkar re-exporteras). Verifierat: `python -m unittest test_regression test_symbol_geometry` — 173/173 gröna, samt `find_valve_shapes()` ger identiskt resultat anropad via `pid_viewer.find_valve_shapes` och via `equipment_detection.find_valve_shapes` direkt (7/7 resp. 6/6 på de två LKAB-testritningarna).

**Varför den ursprungliga uppdelningen fanns:** P&ID-komponenten är stor och fristående nog för att motivera separation. Underlättar framtida utbyte av viewer-implementationen — skälet gäller lika mycket den nya uppdelningen.

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
| Slå ihop "Skanna P&ID" och "Analysera P&ID" till en enda skanning (2026-08-05) | Anton märkte att "🔍 Skanna P&ID" (Utrustningsregistret) hittade betydligt färre taggar än "📋 Analysera P&ID" (P&ID-verktygsraden). Grundorsak: två helt separata tagg-matchare. `scan_pdf_for_equipment()`s `_parse_tag()`-fallback (pid_viewer.py) krävde explicit `len(pfx) >= 2`, vilket tyst kastade bort enbokstavs-prefix utan separator (`P101`, `T12`, `E205`) — och slog aldrig ihop text som PDF:en delat upp i flera textobjekt (`"20"` `"-"` `"PCV"` `"-"` `"101"`). `_analyze_pid()`s `_pick_best_tag()`/`_spatial_combine()` gjorde båda dessa saker redan, därav fler resultat. **Fix (efter Antons val: slå ihop helt, inte bara dela logik):** `_spatial_combine()` utökad att returnera `(text, x0, y0, x1, y1)` — tre befintliga anropsställen (`_extract_tag_from_rect`, `_analyze_pid`, tagg-highlight-passet) uppdaterade att packa upp tupeln. `scan_pdf_for_equipment()`s Pass 2 skrivet om att använda `_spatial_combine`+`_pick_best_tag` istället för rå per-ord `_parse_tag` — samma teknik som redan bevisat fungera, ingen regression eftersom `_pick_best_tag` internt faller tillbaka på `_parse_tag`. `_analyze_pid()` tog bort sin egen skanningsloop helt och anropar nu `scan_pdf_for_equipment()` direkt (fick även samma OCR-fråga-dialog som Skanna P&ID redan hade — Analysera P&ID saknade OCR helt innan). Två nya delade hjälpfunktioner i pid_viewer.py (måste ligga där, inte i hazop.py som ursprungsplanen sa — hazop.py importerar FRÅN pid_viewer.py, omvänt import skulle vara cirkulärt): `apply_scan_result_to_equipment_catalog(db, scan_result)` och `upsert_identified_tags_from_scan(db, scan_result)`, anropade från BÅDA `EquipmentPanel._scan` (hazop.py) och `_analyze_pid` (pid_viewer.py) — oavsett vilken knapp man klickar hamnar samma taggar nu i både `equipment_catalog` (Utrustningsregistret/nodskapande/utrustningsmarkörer) och `pid_identified_tags` ("Identifierade objekt" i Inställningar, vars bekräfta-komponenttyp-arbetsflöde `confirmed_comp_for_tag()` lämnades helt orört). Städade även bort `PIDPanel._scan_equipment` (pid_viewer.py) — en oanvänd dubblett av `EquipmentPanel._scan` utan några anropsställen. **Observerad avvägning vid verifiering mot riktiga `P&ID ref/`-filer:** den mer tillåtande matchningen (ingen `len()`-spärr) hittar fler riktiga taggar men även mer brus från ritningsramar/titelblock-text (t.ex. "DATUM", "SKALA", "REV") — detta är samma avvägning "Analysera P&ID" redan hade före sammanslagningen, inget nytt problem, men värt att känna till om Utrustningsregistret känns "skräpigt" efter en skanning. **Testfynd under implementationen (headless-artefakt, inte en app-bugg):** `QProgressDialog.wasCanceled()` returnerar spuriöst `True` direkt efter `.close()` under offscreen QPA-plattformen som headless-tester kör med (reproducerades inte i en riktig fönstersession) — löst med en lätt Python-stub istället för att patcha en riktig `QProgressDialog`s metoder (undvek en genuin hängning som uppstod av att mocka Qt-klassens metod direkt). 7 nya tester: `UnifiedTagScanTests` (5 st, inkl. regressionstest för `P101`-fallet) + `ReloadAllPanelsDbSwapTests` räknas i föregående rad. Alla 107 tester passerar. |
| Bow-tie-ventilformsdetektion, inkl. ventiler utan tagg (2026-08-05) | Anton: "Programmet måste bli bättre på att identifiera ventiler. Börja med de ventilerna som ser ut som Bow-tie" — och valde det större omfånget (hitta ventiler UTAN tagg också) när frågan ställdes. **Ny geometrisk klassificerare** `symbol_geometry.bowtie_score(primitives, index_group)`: samplar 20 punkter längs varje primitivs kant (rects/quads samplas längs alla 4 sidor, inte bara hörnen — annars misstolkas mellanliggande skivor utan datapunkter som "hopklämda" istället för "ingen primitiv passerar här", vilket fick en helt vanlig rektangel att felaktigt score:a 1.0), mäter tvärgående spridning i 9 skivor längs axeln och scorar hur mycket profilen klämmer ihop sig i mitten relativt ändarna. Testar BÅDA axlarna (x och y) och tar det högsta — en bowtie på en vertikal rörledning har en lika kvadratisk bbox som en på en horisontell, så bbox-aspect ensam kan inte avgöra vilken axel klämningen ligger på (en tidig version använde `w>=h` och fick en vertikal bowtie att scora 0.33 istället för ~0.72). `find_symbol_clusters()` beräknar nu `bowtie_score` för varje kluster oavsett `min_confidence` (en riktig bowtie kan scora lågt på den generiska "är detta en symbol"-klassificeraren av orelaterade skäl). **Ny reverse-lookup** `find_nearby_tag_text(page, point, radius=150.0)` — motsatsen till den redan existerande `find_tag_position_on_page`: given en punkt (en formklusters mittpunkt), hitta närmsta taggliknande text via samma `_spatial_combine`+`_pick_best_tag`-teknik som den enhetliga skanningen använder. **Ny orkestrering** `find_valve_shapes(pdf_doc, pages=None, min_bowtie_score=0.5)`: skannar sidor efter bowtie-formade kluster, föreslår tagg via `find_nearby_tag_text` (tom sträng — inte gissad, inte bortfiltrerad — när inget rimligt finns i närheten, så granskningsdialogen kan fyllas i manuellt). Ny knapp "🦋 Hitta ventilformer" i P&ID-verktygsraden (`PIDPanel._find_valve_shapes`) öppnar samma `EquipmentMarkerReviewDialog` som taggbaserad autodetektering ("🎯 Hitta på P&ID") redan använder — löser upp `equipment_id` mot en befintlig `equipment_catalog`-rad om taggen matchar, annars lämnas den `None` så dialogens spara-logik (från förra sessionens Task 20) skapar en ny rad. **Kritiska fynd vid verifiering mot riktiga `P&ID ref/`-filer (annars hade detta skeppats trasigt):** (1) `page.get_text()`/`page.get_drawings()` rapporterar ALLTID koordinater i sidans råa OROTERADE mediabox, aldrig i den roterade rymden `page.rect`/`page.get_pixmap()` som resten av appens markörplacering (`pdf_to_scene`/`scene_to_pdf`) förutsätter — upptäckt genom att en form nära en kant på en riktig roterad Hybrit-sida (`/Rotate 270`) rapporterades långt utanför den renderade sidans gränser. Fixat centralt i `symbol_geometry.extract_primitives()` (multiplicerar varje punkt med `page.rotation_matrix`, no-op identitetsmatris för orotersade sidor) samt i pid_viewer.py via ny delad hjälpfunktion `_rotate_words(words, page)`, kopplad in på alla fyra ställen som läser `get_text("words")`/`search_for()` för positionsdata (`_words_from_native`, `scan_pdf_for_equipment` Pass 2, `find_nearby_tag_text`, tagg-highlight-passet) — detta var samma bugg som redan påverkade förra sessionens taggbaserade autodetektering (`detect_equipment_symbols`/`find_tag_position_on_page`) på roterade sidor, inte unikt för bowtie-arbetet, men upptäcktes och fixades här. Nya regressionstester: `RotatedPageCoordinateTests` (test_symbol_geometry.py) + `test_find_valve_shapes_and_tag_link_survive_page_rotation` (test_regression.py), båda med en icke-kvadratisk sida + 90°-rotation så en axel-omkastningsbugg inte kan råka se korrekt ut av misstag. (2) `bowtie_score` ensam räcker inte på riktiga ritningar — en titelblocks rutnätslinjer på en Hybrit-sida gav 13 kluster som scorade 0.9–1.0, och en instrumentbubbla (cirkel+ledningsstubbar) scorade också högt. Det som faktiskt skiljer dem från riktiga ventiler är storlek/proportion RELATIVT sidan, vilket `bowtie_score` avsiktligt ignorerar: falsklarmen var alla antingen mycket mindre än sidans egen textstorlek (`norm_size` under 1.5) eller kraftigt utsträckta (aspect >> 3, som en lång rörledning). `find_valve_shapes()` filtrerar nu även på `aspect <= 3.0` och `1.5 <= norm_size <= 40.0` — samma etablerade gränser som `classify_cluster()` redan använder för "rimlig diskret symbol" (inga nya trösklar uppfunna). Verifierat före/efter på 6 riktiga filer: Hybrit-titelbladet gick från 61 falska träffar till 0; ITS-filen (`XFB_31301`) höll kvar 14 riktiga, visuellt bekräftade bowtie-ventilsymboler (`HV_31301_64` m.fl., inkl. en vertikalt orienterad — bekräftar axel-fixen) med korrekt tagglänkning; Smurfit Kappa-filen höll kvar riktiga ventiler (`PV-107` m.fl.). (3) Anton påpekade (efter att detta redan verkat klart): "Eftersom ledningar ritas vertikalt eller horisontalt kommer oftast symbolerna för ventiler innehålla streck som går på diagonalen eller delvis" — en stark, tidigare outnyttjad signal. Rörledningar är alltid strikt ortogonala i P&ID-konvention, så ett diagonalt linjesegment kan ENDAST komma från en symbols egen geometri (en bowties triangelkanter), aldrig från en rörledning, ett titelblocksrutnät eller en instrumentbubblas raka skaft. Ny funktion `symbol_geometry._prim_is_diagonal()` (vinkel modulo 90°, tolerans 12°) + `has_diagonal` i `cluster_features()`; `find_valve_shapes()` kräver nu även detta. Verifierat om mot samma 6 filer efter tillägget: inga tidigare visuellt bekräftade riktiga ventiler försvann, en spridd falsklarm i ITS-filen försvann. **Medveten avgränsning:** filtren eliminerar INTE alla falsklarm (ett kluster i Loket-filen överlevde båda filtren — vid visuell granskning en sammanslagen klump av flera små rörsymboler/pilhuvuden, inte en enskild ventil; klustring vs. formklassificering är olika felkällor) — geometrisk formigenkänning på verkliga, brusiga P&ID:er är en heuristik, inte ett bevis. 13 nya tester i `test_symbol_geometry.py` (`BowtieScoreTests` 9 st + `RotatedPageCoordinateTests` 2 st) och `test_regression.py` (`BowtieValveDetectionTests` 11 st). Alla 127 tester passerar. |
| Av-/aktiverbar avvikelse-checkbox, numerisk frekvens, direktredigering av konsekvens, drag-and-drop tagg (2026-08-07) | EquipmentDeviationBars kryssruta kan nu avaktiveras igen (med bekräftelse om orsaker finns), numerisk händelse/år-frekvens visas och är klickbar-redigerbar, KON-celler i scenariotabellen stödjer samma enkelklick-redigering som ORS/SG, och en utrustningsmarkör kan Shift-dras från P&ID-vyn till en KON-cell för att sätta dess taggnummer (visas i en ny remsa högst upp, precis som ORS — fritexten orörd). Se separat avsnitt nedan för full detalj. |
| "🔧 Objekt" i högerklicksmenyn, direkt konsekvensinmatning, städat verktygsfält (2026-08-07) | P&ID:s högerklicksmeny kan nu skapa ett fristående utrustningsobjekt (typ + valfri tagg) som direkt öppnar EquipmentDeviationBar. Att lägga till en orsak via trädets "+" eller worksheetens Ctrl+Enter skapar nu automatiskt en tom konsekvens också, med redigeringsläget direkt på KON-cellen — ingen popup krävs längre. De redundanta "⚙️ Orsak"/"⚠️ Konsekvens"-lägesknapparna i P&ID-verktygsraden är borttagna. Se separat avsnitt nedan för full detalj. |

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

## Äldre sessionsloggar (2026-07-29 till 2026-08-16) — se NOTES_ARCHIVE.md

1972 rader, ~50 sessionsposter (prestandaoptimeringar, uppstart, Utrustningsregistret-omskrivning, ventildetektering fas 1–2, pump-/instrumentdetektering, P&ID-navigering, symbolmatchning m.m.) flyttades hit 2026-08-20 eftersom NOTES.md annars läses i sin HELHET varje session (se CLAUDE.md "Session context") och hade vuxit till 2500+ rader. Läs `NOTES_ARCHIVE.md` om du behöver historisk kontext från den perioden — annars räcker denna fils "Funktioner implementerade"-tabell ovan och "Arkitekturella beslut" för det mesta.

---

## Granska autodetekterad utrustning — smalare Metod, Typ som gardinlista, Autodetektera tagnummer, större ruta (2026-08-17)

**Rapport:** Anton: "I popupen granska autodetekterad utrustning kan 'metod' vara en mycket mindre kolumn. Typen skall vara en gardinlista enligt vad som finns under standardobjekt. Jag vill även ha en knapp som heter autodetekytera tagnummer som tar den närmaste, eller och presenterar rätt. Tag nummer. detta skall vara redigerbart idenna vyn. Du kan göra hela rutan lite större så blir det lättare att se." Fyra delar, alla i `EquipmentMarkerReviewDialog` (pid_viewer.py):

1. **Metod-kolumnen smalare:** `_C_METHOD`-bredden sänkt 160→90px. Fulla etikettexten ("📐 Ledarlinje" m.fl.) syns fortfarande via tooltip på cellen, eftersom den nu ofta klipps.
2. **Typ som gardinlista:** `_C_TYPE`-cellen är nu en `QComboBox` (editerbar, så en ovanlig befintlig comp_type-sträng ändå visas) istället för fri text. Ny `_type_options()`-metod bygger EXAKT samma lista som hazop.py:s egen `_equipment_type_options(db)` gör för Utrustningsregistret: `COMPONENT_TYPES` + valfria egna typer redan använda i katalogen + varje namn från Standardobjekt (Inställningar → Standardobjekt) + "Rörledning"/"Övrigt / Okänd" sist. `_save()`/`_apply_mass_tag()` uppdaterade till att läsa/skriva `cellWidget(...).currentText()`/`.setCurrentText(...)` istället för `.item(...).text()`/`.setText(...)`, eftersom cellen inte längre är en `QTableWidgetItem`.
3. **Ny knapp "🔍 Autodetektera tagnummer":** kör `equipment_detection.find_tag_near_point` (samma närmaste-native-text-sökning den ursprungliga skanningen redan använder) på nytt för varje ikryssad rads egen (sida, x, y) och fyller i Tagg-cellen om något hittas — lämnar cellen orörd annars. Mest användbar för "untagged"-rader vars Tagg-cell fortfarande visar ett tillfälligt id. Kräver `pdf_path` (samma "ingen PDF, knappen inaktiverad"-konvention som "✏ Form"-kolumnen redan har).
4. **Tagg redan redigerbar** — bekräftat sedan tidigare (2026-08-15), ingen ändring behövdes.
5. **Större ruta:** `setMinimumSize` 760×480 → 1000×640.

**Test:** `test_metod_column_is_narrow`, `test_typ_dropdown_includes_standardobjekt_entries`, `test_dialog_is_larger_than_before`, `test_autodetect_tags_button_disabled_without_pdf_path`, `test_autodetect_tags_fills_in_the_nearest_native_text_for_checked_rows`, `test_autodetect_tags_leaves_tag_untouched_when_nothing_found_nearby`, `test_autodetect_tags_with_nothing_checked_shows_info` (alla nya). `test_editing_typ_cell_corrects_the_saved_type` och `test_mass_apply_sets_type_and_tag_sequence_on_checked_rows_only` uppdaterade till combobox-läsning. Röd→grön verifierat för samtliga nya tester. Full svit (811 tester) grön.

## Radbilder i granskningsdialogen klippte igenom LKAB-stilens avlånga instrumentbubblor (2026-08-17)

**Rapport:** Anton: "I granska och detektera får du gärna visa en lite mer utzoomad bild." Följt av förtydligande: "På varje objekt" — gäller alltså båda grenarna i `EquipmentMarkerReviewDialog._render_thumbnail`, inte bara ett specialfall.

**Root cause:** bekräftat direkt mot ett riktigt LKAB-P&ID (S0000156) — en punktbaserad rad (ingen outline, bara x/y) använde `pad = max(page_scale*1.5, 15.0)`, vilket för en avlång "PI"-instrumentbubbla (ISA-stil kapsel, vanlig i LKAB:s ritningar) klippte rakt igenom den — de rundade ändarna hamnade helt utanför bildrutan, bara mittbandet och texten syntes. Samma eftersläpande snävhet fanns i outline-grenen (`pad = max(w,h)*0.25+4.0`), om än mindre allvarligt eftersom den redan utgår från symbolens egen uppmätta form.

**Fix:** båda grenarnas padding fördubblad ungefär: punktfallet `1.5×/15pt-golv` → `3.0×/28pt-golv`, outline-fallet `0.25×+4pt` → `0.5×+8pt`. Verifierat direkt mot den riktiga PI-bubblan och en riktig ventil-bowtie på samma fil — hela symbolen ryms nu med bekväm marginal i båda fallen.

**Test:** `test_thumbnail_crop_is_wider_than_before_for_a_point_only_row` (ny, spionerar på `image_symbol_matching.render_gray`s bbox-argument, röd→grön verifierad). Full svit (812 tester) grön (körningen tog ovanligt lång tid denna gång, ~460s mot normalt ~120s — sannolikt tillfällig systembelastning, ingen ny hängning: alla test passerade).

## Nodnamn på P&ID uppdaterades inte vid namnbyte (2026-08-17)

**Rapport:** Anton: "Det finns en funktion som sätter ut nodnamnen på P&ID under editera nodmarkup. När jag sedan uppdaterar namnet på noden vill jag att detta uppdateras även på P&ID."

**Root cause:** "Lägg ut nodnamn"-verktyget i Editera nodmarkup (`NodeMarkupPanel`s text-verktyg) fryser noddens namn som en egen sträng i `node_markups.label` i samma ögonblick användaren klickar ut den (`PIDPanel._on_viewer_markup_drawn`, pid_viewer.py) — ingen levande referens till `nodes.name`. En redan existerande metod `Database.sync_node_text_markups(node_id, new_name)` fanns för att synka detta, men var bara kopplad till EN av tre namnbytesvägar (NodePanels egen spara-knapp, via en extern signalkoppling i `MainWindow.__init__`). De andra två — `TreePanel._rename_node` ("Döp om" i trädet) och `PropertiesRibbon._edit_node_name` (Namn/P&ID-ref-popupen) — anropar `Database.update_node()` direkt utan att någonsin synka `node_markups`.

**Fix:** flyttade synken IN i `Database.update_node()` självt — den enda delade skrivvägen alla tre namnbytesflöden redan går igenom — istället för att varje anropsställe ska komma ihåg sitt eget uppföljningsanrop (exakt den sortens inkonsekvens som orsakade buggen). Tog samtidigt bort den nu överflödiga externa `sync_node_text_markups`-anropet i `MainWindow.__init__` (databasen sköter det nu själv). `PropertiesRibbon`s namnbytesväg saknade dessutom en P&ID-omritning helt (`_on_props_changed` uppdaterade bara träd + scenariopanel) — lade till ett villkorat `pid_panel.refresh_markup_overlays()`-anrop när det ändrade objektet är en nod, så etiketten faktiskt syns direkt istället för att vara korrekt i databasen men visuellt inaktuell tills något annat råkar trigga en omritning.

**Test:** `test_rename_updates_node_name_markup_on_pid` (TreeNodeRenameTests, via "Döp om"), `test_on_props_changed_refreshes_pid_overlays_for_a_node`/`test_on_props_changed_does_not_touch_pid_overlays_for_non_node_items` (SafeguardCreatedDoubleRebuildTests, alla nya). Röd→grön verifierat för båda fixarna. Full svit (815 tester) grön.

## HAZOP scenario: kolumnnamn, dubbelklick på tom safeguard, "hitta liknande"-objekt syntes inte (2026-08-17)

**Rapport 1:** Anton: "Döp om kolumnen FA / ANt. Övriga till Enablers i hazop scenario." — `ScenarioTablePanel._COLS[ScenarioTablePanel._C_LOPA]` (hazop.py) bytt från `'FA / Ant. / Övriga'` till `'Enablers'`. Test: `test_lopa_column_header_renamed_to_enablers`.

**Rapport 2:** "Gör även så jag kan dubbelklicka på safeguards för att redigera den direkt även om inget ligger tillagt. (precis som konsekvens i hazopscenario)". Root cause: en KON-cell har alltid en riktig (om än tom) `consequences`-rad bakom sig — varje orsak får automatiskt en vid skapande — så cellen är alltid redigerbar. En SG-cell saknar denna auto-skapelse; när `sg is None` sätts `~ItemIsEditable` explicit (`_add_row`), och `_on_cell_double_clicked` returnerar direkt om cellen inte är redigerbar — dubbelklick på en tom safeguard-cell gjorde alltså ingenting. Fix: när `col == self._C_SG` och cellen inte är redigerbar, anropa `_quick_add_safeguard(cons_id)` (samma "inget popup, rakt till radigering" som Enter/plus-raden redan använder) istället för att bara returnera. Test: `test_double_click_on_empty_safeguard_cell_quick_adds_one`, `test_double_click_on_existing_safeguard_cell_still_edits_in_place`.

**Rapport 3:** "När jag har identifierat objekt genom 'hitta liknande' så kommer inte dessa objekten upp i hazop scenario trots att jag har lagt till orsaker och klickar på dem." Följt av bekräftelse: "Om jag har lagt till ett objekt manuellt och lägger till orsaker får jag upp detta i hazop scenario men om jag har lagt till objekt via hitta liknande får jag inte upp det." Root cause (djupgrävd): manuell enstaka objektplacering (`place_equipment_marker`, pid_viewer.py) öppnar `EquipmentDeviationBar` direkt efter skapande med `active_node_id=self._active_node_id` — den baren tilldelar noden till det nya objektet där. `EquipmentMarkerReviewDialog._save()` (den delade batch-spara-dialogen för BÅDE "Hitta liknande symbol" och mall-baserad sökning) öppnar aldrig den baren för någon rad — ett nytt objekt hamnar därför permanent med `node_id=NULL`, och `EquipmentDeviationBar._activate_deviation()` avfärdar TYST varje avvikelse/orsak man kryssar för ett `node_id=NULL`-objekt (`if node_id is None: ... return`). Exakt matchar "jag har lagt till orsaker och klickar på dem" — kryssen "tar", men skrivs aldrig till databasen.

**Fix:** `EquipmentMarkerReviewDialog` fick en ny `active_node_id`-parameter, given av båda "Hitta liknande symbol"-anropsställena (`self._active_node_id`, PIDPanel) men INTE av den vanliga dokumentbreda "Hitta objekt på P&ID"-skanningen i `EquipmentPanel` (hazop.py), som saknar ett meningsfullt "aktiv nod"-begrepp helt. `_save()` tilldelar nu `active_node_id` till varje NYSKAPAD utrustningsrad via det redan existerande `Database.set_equipment_node()`. Hittade samtidigt en relaterad bugg under vägen: `_save()` kollade aldrig `get_equipment_by_tag(tag)` innan den skapade en ny rad (till skillnad från `place_equipment_marker`, som redan gör det) — samma fysiska objekt hittat igen av en likhetssökning skulle alltså skapa en DUBBLETT (ny, `node_id=NULL`) istället för att återanvända den redan nod-kopplade posten. Fixad samtidigt (samma commit, samma root cause-familj).

**Test:** `test_save_assigns_active_node_to_newly_created_equipment`, `test_save_without_active_node_leaves_new_equipment_unassigned`, `test_save_reuses_existing_equipment_by_tag_instead_of_duplicating` (alla nya). Röd→grön verifierat för samtliga tre buggar denna session. Full svit (821 tester) grön.

## Krasch vid "Lägg till deltagare" + Standardobjekt-CRUD flyttad in i Standardorsaker (2026-08-17)

**Rapport 1 (krasch):** Anton: "Programmet kraschar när jag klickar på lägg till deltagare. Fixa detta." → "Se senaste logg". Krasch-loggen (`crashes/crash_20260817_125839_ProgrammingError.json`) visade `sqlite3.ProgrammingError: Cannot operate on a closed database` i `Database.add_participant()`, anropad från `ParticipantMatrixPanel._add_participant`.

**Root cause:** samma buggfamilj som redan är dokumenterad och fixad flera gånger tidigare i `MainWindow._reload_all_panels()` (Worksheet, EquipmentPanel._model, admin_panel._pid_mgmt, analysis_panel._model) — en nästlad delpanel som fångade `db`-objektet vid SIN EGEN konstruktionstid och aldrig fick det uppdaterat vid projektbyte. `SettingsPanel` har redan en namngiven lista med delpaneler som behöver uppdateras (`_std_causes_panel`, `_std_objects_panel`, `_tag_memory_panel`, `_tag_db_panel`, `analysis_panel`) — men `_participant_matrix_panel` (Deltagarfliken, tillagd 2026-08-11) hade aldrig lagts till i den listan. Efter ett projektbyte pekade den fliken permanent på den gamla, stängda anslutningen.

**Fix:** lade till `'_participant_matrix_panel'` i den redan existerande listen i `_reload_all_panels()` — samma mönster, ingen ny mekanism. Test: `test_participant_matrix_panel_gets_new_db`, `test_add_participant_does_not_crash_after_db_swap` (ReloadAllPanelsDbSwapTests). Röd→grön verifierat — den återställda koden reproducerade exakt kraschmeddelandet.

## HAZOP scenario / P&ID-objektflöde: djupare utredning + Standardobjekt-funktioner i Standardorsaker (2026-08-17)

Uppföljning på "hitta liknande"-buggen ovan: Anton bekräftade root cause ("Om jag har lagt till ett objekt manuellt... får jag upp detta... men om jag har lagt till objekt via hitta liknande får jag inte upp det") och bad om två till saker:

**"Slå ihop popupen där man väljer objekttyp, objekttagg och avvikelser":** utredning visade att typ+tagg REDAN är EN enda popup (`EquipmentTagPopup`, hazop.py) — bara avvikelse-checklistan (`EquipmentDeviationBar`) är ett separat steg. Detta är en MEDVETEN, redan dokumenterad designbeslut (NOTES.md 2026-08-12/13): ett försök att bygga in "starta ny placering" i avvikelsebaren byggdes och togs bort samma vecka ("en popup förankrad vid ett REDAN placerat objekt är fel plats att starta placeringen av ett NYTT objekt från"). Inte byggt om denna omgång — flaggat till Anton för bekräftelse innan ett omvänt beslut görs, eftersom det redan finns ett fungerande mönster (`CauseObjectPopup`, "Orsak på P&ID") som visar HUR en sammanslagning skulle kunna se ut om han fortfarande vill ha den.

**"Implementera de funktioner som finns i standardobjekt även i standard orsaker så man kan lägga till nya objekt under standardorsaker":** byggd. `StandardCausesSettingsPanel`s Objekt-kolumn (kolumn 2 av 4, Inställningar → Standardorsaker) saknade helt +/−/↑/↓-knappar och redigerbarhet — till skillnad från grannkolumnerna Avvikelse/Orsaker/Orsaksbeskrivningar. Lade till exakt samma CRUD (`_add_obj`/`_del_obj`/`_move_obj`/`_on_obj_changed`) mot samma `standard_objects`-tabell och samma `Database`-metoder (`add_standard_object`/`update_standard_object`/`delete_standard_object`/`reorder_standard_objects`) som `StandardObjectsSettingsPanel`s egen lista redan använder — ingen ny tabell, ingen migrering. Ett nytt objekt har 0 orsaker än, så "Visa alla objekt" tvingas på vid tillägg för att det ska synas direkt. Test: `test_add_obj_creates_a_new_standard_object_and_shows_it`, `test_renaming_obj_item_persists_and_strips_the_count_suffix`, `test_del_obj_removes_the_standard_object`, `test_move_obj_persists_new_sort_order` (StandardCausesObjectCrudTests). Röd→grön verifierat.

**Test totalt:** full svit (827 tester) grön.

## Ny huvudflik "HAZOP preparation" — Projekt/Deltagare/Riskmatris/Standardorsaker flyttade ut ur Inställningar (2026-08-17)

**Rapport:** Anton: "jag vill flytta om flikarna lite. Skapa en ny huvudflik i Claude med namnet HAZOP preperation. Fliken ska samla följande administrativa underlag: Projekt, Deltagare, Riskmatris, Standardorsaker. Denna fliken ska ligga ute i det svarta fältet till vänster högst upp." Planerad i plan mode (se `polymorphic-booping-papert.md` i sessionens plan-katalog) innan implementation, med explicit bekräftelse: "Denna knappen ska ligga först i view_stack" — inte bara visuellt överst i navigeringsraden, utan strukturellt `view_stack`-index 0. Ikonval `check` och att behålla "Riskmatris & Kategorier" oförändrat bekräftades via AskUserQuestion.

**Genomförande:**
1. **Ny klass `HAZOPPreparationPanel`** (hazop.py, före `SettingsPanel`) — en `QTabWidget` med "Projekt", "Deltagare" (`ParticipantMatrixPanel`, oförändrad klass), "Riskmatris & Kategorier" (hela den gamla `SettingsPanel`s matrix/palett/kategori-kod + alla 17 tillhörande metoder flyttade hit oförändrade — det VAR i praktiken nästan hela den gamla klassen), "Standardorsaker" (`StandardCausesSettingsPanel`, oförändrad klass). Egen `_load_all()` med bara matrix/palett/kategori/projekt-delen.
2. **`SettingsPanel` krympt** till de fem kvarvarande flikarna: "P&ID-inställningar", "Tagdatabas", "Identifierade objekt", "Standardobjekt", "Smart igenkänning".
3. **`view_stack`-index skiftade +1** för alla FEM befintliga sidor (P&ID-vy 0→1, Worksheet 1→2, Utrustning 2→3, Studiehantering 3→4, Inställningar 4→5) eftersom `HAZOPPreparationPanel` blev index 0 — inte bara sist med visuell layout-ordning, exakt vad Anton bad om. Detta krävde att räkna om VARJE hårdkodat `self._switch_view(N)`-anrop i hela hazop.py (9 st: hazop.py rad 6108/6204/6226/6262/20179/20212/20445/20478/20965) plus `_switch_view()`s egen kropp (varje `page == N`-jämförelse) och tre hårdkodade `view_stack.currentIndex()`-jämförelser i testsviten.
4. **`matrix_changed`-signalen finns nu på BÅDA panelerna** — `SettingsPanel` behåller sin egen (TagDatabasePanel forwardar dit, orelaterat till riskmatrisen själv) och `HAZOPPreparationPanel` fick en egen (den faktiska matris/kategori-signalen) — `MainWindow.__init__` kopplar båda till samma `_on_matrix_changed`, ingen cross-panel-signalvidarebefordran behövdes.
5. **`_reload_all_panels()`** — `hazop_prep_panel` tillagd i topp-nivå-listan; `_std_causes_panel`/`_participant_matrix_panel` flyttade från `SettingsPanel`s nästlade-underpanel-loop till en motsvarande loop för `hazop_prep_panel` (samma mönster, nytt värdobjekt).

**Test:** ~20 befintliga tester som konstruerade `SettingsPanel(self.db)` för att testa Projekt/Riskmatris/Kategorier-funktionalitet pekades om till `HAZOPPreparationPanel(self.db)` (de fem testerna för P&ID-inställningar/OCR/sidorientering som FAKTISKT stannade i SettingsPanel lämnades oförändrade — verifierat en och en, inte en blind sök-och-ersätt). `win.settings_panel._participant_matrix_panel` → `win.hazop_prep_panel._participant_matrix_panel` i de två db-swap-kraschtesterna. Tre `view_stack.currentIndex() != 2` (Utrustning) → `!= 3`. Full svit (832 tester) grön. Manuell rimlighetskontroll i headless-läge: alla sex knappar hoppar till rätt sida, alla fyra HAZOP-preparation-flikarna innehåller rätt widgets.

## Stort UI/UX-paket: Projekt/Deltagare/Riskmatris/Avvikelser & Orsaker, Blad+Noder, PDF-export, trädredigering, nodmarkup+Red markup (2026-08-17)

**Rapport:** Anton skickade en lång önskelista (~11 punkter) i en enda konversation, som planerades i plan mode (`polymorphic-booping-papert.md`) och byggdes i sex omgångar (Fas A var redan klar sedan tidigare samma dag, se föregående avsnitt). Full regressionssvit (746 tester) grön efter varje delsteg; en commit per delsteg.

**Fas B1 — Projekt:** Tar bort HAZOP-ledare-fältet (rollen görs istället tillgänglig som en fri Deltagare-kolumn, se B2). Lägger till Projektnummer + Kund/Företag. Ersätter den enda Revision-textraden med en tabell (Rev/Datum/Beskrivning, `project_revisions`-tabell) där "+ Lägg till rad" föreslår nästa bokstav (A, B, C...) automatiskt. Ny "Egna fält"-sektion (`project_custom_fields`) för fritt namngivna namn/värde-par.

**Fas B2 — Deltagare:** Nytt analystillfälle väljs nu via en riktig datumväljare (`_AnalysisSessionDateDialog`, QDateEdit + "Idag"-knapp) istället för en fri textdialog. Enter på en enkelklickad rad lägger till en ny deltagare (monkeypatchad `keyPressEvent`, samma mönster som `StudyManagementPanel._sheet_list`). Tar bort den hårdkodade Roll-kolumnen till förmån för egna, användarnamngivna kolumner (`participant_columns`/`participant_column_values`) placerade mellan Efternamn och analystillfällena.

**Fas B3 — Riskmatris** (döpt om från "Riskmatris & Kategorier" — föregående avsnitts AskUserQuestion-svar hade bekräftat att BEHÅLLA namnet oförändrat, men Anton bad uttryckligen om namnbytet igen i denna omgång, omprövat och bekräftat via en ny AskUserQuestion): "Vänd X"/"Vänd Y"-kryssrutorna ersatta med klickbara pil-`QToolButton`s (samma `isChecked()`/`toggled`-kontrakt, ingen ändring i `_build_matrix_grid`s rotationslogik). Konsekvenskategoriernas textfält (Frekvens→X-läget) är nu flerradiga (`QTextEdit` istället för `QLineEdit`) och hela radens höjd (radhuvud + cellknappar) följer den högsta cellens innehåll. **Bugfix:** "<"/"≥"-tecken i långa axeletiketter klipptes av — `QLineEdit.setText()` lämnar markören i SLUTET av texten, och en etikett bredare än det fasta 80px-fältet (uppmätt: "< 0.1/år" ≈ 96px vid 8px-fonten) fick widgeten att scrolla för att hålla markören synlig, vilket gömde den inledande karaktären. Fix: `setCursorPosition(0)` efter varje `setText()` på dessa fält.

**Fas B4 — Avvikelser & Orsaker** (döpt om från "Standardorsaker"): Ny kolumn längst till vänster listar nodtyper (`node_types`-tabell, seedas lazy med "Processnod"). Filtrerar Avvikelse-listan till vald typ (NULL `node_type_id` faller tillbaka till förstavald/standardtyp för bakåtkompatibilitet med befintlig data). Drag-and-drop en avvikelse till en nodtyp SKAPAR EN KOPIA (djup, oberoende — inkl. alla dess `standard_causes`) — bekräftat med Anton via AskUserQuestion att det ska kopiera, inte flytta/länka. Tar bort Orsaksbeskrivningar-kolumnen helt: UI (4:e kolumnen), `Database`-CRUD (`cause_descriptions`/`add_cause_description`/etc.), seedningsfunktionen `_seed_cause_descriptions` + dess `_CAUSE_DESCRIPTIONS`-datakälla — verifierat att de bara användes av denna panel plus en envägs JSON-export, aldrig av träd/scenario/exportkod. `cause_descriptions`-tabellen lämnas orörd i schemat (ingen destruktiv migrering).

**Fas C — Blad + Noder:** "Blad"-fliken (bladordning/döp om/ta bort) flyttad från Studiehantering → PID-hantering in i `HAZOPPreparationPanel`; `PIDManagementPanel` behåller bara "Revisioner". Ny P&ID-revision-kombobox per blad (länkar mot `pid_sheets.revision_id`, som saknade UI innan). Bladnamn genereras nu som `"{Filnamn} – sida {N}"` istället för `"Blad {N}"`, både vid första import och vid sammanfogning av fler PDF:er (`Database.ensure_sheets_initialized`/`append_sheets` tar nu emot pdf_path). Ny "Noder"-flik listar alla noder (nummer, namn, vilka blad de finns på via nya `Database.nodes_on_page()`/`pages_for_node()`), med "+ Ny nod" och dubbelklick-döp om — speglar HAZOP-trädet åt båda hållen via `structure_changed`-signaler mellan `TreePanel` och `HAZOPPreparationPanel`.

**Fas D — PDF-export, rotationsfix:** Föregående samma dags fix (se ovan) fick sidans `/Rotate`-attribut att stämma i exporten, men transformerade aldrig markörernas egna koordinater. Markör-`(x,y)` och nodmarkupens punkter lagras i den LIVE VYNS redan roterade visningsrymd (`scene_to_pdf` är ren skala+offset, ingen rotationsmatris) — PyMuPDF:s rit-API adresserar dock alltid sidans råa, oroterade content-space oavsett `/Rotate`. Fix: transformera varje punkt via `page.derotation_matrix` innan ritning (verifierat matematiskt: content(100,50) → rotation_matrix → "lagrat" värde → derotation_matrix → tillbaka till exakt (100,50)), applicerat på C/K/S/O-markörer OCH nodmarkup-polygoner/annoteringar. Samtidigt: `_draw_pid_marker` ritar nu en rundad "kort"-ruta med bokstav+etikett inbakad istället för en cirkel+text-bredvid, närmare appens egna badge-stil.

**Fas E1 — Inline-redigering i trädet:** Dubbelklick på Nod (utan markup)/Avvikelse/Orsak/Konsekvens/Safeguard byter till redigeringsläge direkt i trädet. Den dekorerade visningstexten ("    ⚙ 1. Beskrivning...") byts tillfälligt mot den råa beskrivningen (hämtad färskt från DB, inte reverse-engineerad ur den dekorerade strängen) under redigering; en fullständig `refresh()` efter commit återställer dekorationen. Ny `TreePanel.item_edited_inline`-signal utlöser `scenario_panel.refresh()` + `pid_panel.reload_overlays()` — samma "editera → full refresh"-arkitektur som scenariotabellens egna redigeringar, fast i motsatt riktning. Känd, accepterad begränsning: Escape för att avbryta en redigering lämnar cellen odekorerad tills nästa refresh (Qt:s egen item-editerings-maskin ger ingen krok för detta) — självläkande, inte värt en full delegate-baserad editor bara för detta.

**Fas E2 — "Ej definierad"-hantering:** Ett utrustningsobjekt utan typ visade "TAG-ABC —" (bindestreck + tom `equipment_type`-sträng); visar nu "TAG-ABC, ej definierad" (kursivt) eller "TAG-ABC, ventil" (rakt) för ett typat objekt. Dubbelklick öppnar en typväljare (Standardobjekt-listan). Gäller bara den mer sällsynta, oflätade EQUIP_T-raden (flera avvikelser på samma utrustning+ledord, i praktiken bara nåbar via `add_deviation()` direkt eftersom `get_or_create_deviation()` är idempotent) — det vanliga fallet där en enda avvikelse flätas in till DEV_T/CAUSE_T fortsätter fungera som vanlig inline-textredigering (Fas E1), och kan redan få sin typ satt via scenariotabellens befintliga "🏷 Redigera objekttyp och tag-ID"-flöde.

**Fas F1 — Nodmarkup dockas till höger:** "Editera nodmarkup" gömde tidigare `tree_panel`/`props_ribbon`/`scenario_panel` helt (ersatte i praktiken hela fönstret med P&ID-canvas + en smal ribbon). `tree_panel`/`props_ribbon` förblir nu synliga — `node_markup_panel` satt redan strukturellt längst till höger i `_h_splitter` (efter `props_ribbon`), ingen omflytning behövdes. Nedre fältet växlar fortfarande som standard till Nodmarkeringar-tabellen (att visa scenariotabellen samtidigt som man ritar markup vore förvirrande), men en ny ⇄-knapp i nodmarkup-ribbon (`bottom_panel_toggled`-signal) låter användaren växla till HAZOP scenario utan att lämna redigeringsläget.

**Fas F2 — Red markup konsolideras:** Skrotar allt i Red markup utom "Välj/flytta" (behövs för att markera en redan utplacerad symbol inför storlek/rotation-redigering) och "Lägg ut P&ID-symbol" — polygon/polylinje/smart polylinje/kommentar, färg/opacitet/tjocklek-popupen och visa/dölj-alla-knappen borttagna. Trädets fristående "🔴 Editera redmarkup"-kontextmenyval borttaget helt. Knappen flyttas in i nodmarkup-panelen som enda ingångspunkt (`NodeMarkupPanel.place_symbol_requested`-signal). **Arkitekturbeslut** (bekräftat med Anton via AskUserQuestion): de två redigeringslägena (`enter_markup_edit`/`enter_red_markup_edit`) hålls medvetet tekniskt SEPARATA under huven — egna tillståndsmaskiner i `PIDGraphicsView`, hårt testad P&ID-ritkod — snarare än att slås ihop till ett enda läge, för att undvika regressionsrisk. `MainWindow._on_place_symbol_requested` växlar tillfälligt till red-markup-läget och öppnar symbolväljaren direkt (`RedMarkupPanel.open_symbol_picker()`, hoppar över ett extra klick); `_on_close_red_markup` kollar en `_return_to_node_markup_node_id`-flagga och återgår automatiskt till nodmarkup-redigering för samma nod istället för att stänga allt, eftersom användaren bara bad om att placera en symbol, inte att lämna nodmarkup-redigering.

## Drag-and-drop mellan nivåer i HAZOP-trädet, Shift = kopiera (2026-08-17)

**Rapport:** Anton: "implementera även drag and drop I hazop trädet mellan olika nivåer. drar man konsekvens till ett objekt skall exempelvis både konsekvens och safeguard hänga med. håller man inne shift och drar skall det kopieras." En research-agent bekräftade innan implementation att all nödvändig DB-lager move/copy-infrastruktur redan fanns (bevisad av `ScenarioTablePanel`s egen drag-and-drop) — detta minskade omfånget avsevärt: bara UI-sidan (mime-data, drop-målupplösning, `eventFilter`-hantering) behövde byggas.

**Genomförande:** `TreePanel.tree.setDragEnabled(True)` + en instans-monkeypatchad `self.tree.mimeData` som kodar `f'hzp:treeitem:{type_}:{id_}'` (endast Orsak/Konsekvens/Safeguard — mime-data för Nod/Avvikelse kodas medvetet inte, de flyttas/kopieras inte via denna mekanism). `_tree_reparent_target_at`/`_handle_tree_reparent_drop` i `eventFilter` löser drop-målet och anropar de redan existerande DB-lager-metoderna `move_cause`/`copy_cause` osv (samma de `ScenarioTablePanel` redan använde) — `Shift`-modifieraren (`QApplication.keyboardModifiers()`) väljer copy- istället för move-varianten. Dropp av en Orsak på en annan Avvikelse tar automatiskt med dess Konsekvenser+Safeguards (redan DB-lagrets beteende via cascade, inget nytt UI-arbete krävdes för det). Dropp av en Orsak direkt på en Nod skapar/återanvänder en "Övrigt"-avvikelse under den noden.

**Bugfix under utveckling:** mime-textens fyra delar (`hzp`, `treeitem`, `type_`, `id_`) parsades först som tre (`parts[1]`/`parts[2]` istället för `parts[2]`/`parts[3]`) — `int('treeitem')` kastade `ValueError`, tyst fångad av ett generellt `except ValueError: event.ignore()`, vilket fick ALLA reparent-drop-tester att fela på "acceptProposedAction anropades aldrig" medan no-op/hover-testerna råkade passera av fel anledning (de kollade bara `event.ignore()`, vilket hände oavsett). Fixat genom att korrigera index; no-op-testet omverifierades separat för att bekräfta att det nu passerade av RÄTT anledning.

**Test:** Ny testklass `TreeInternalReparentDragDropTests` (8 tester) — mockar `panel.tree.itemAt` (inte riktig geometri, samma etablerade mönster som `TreePanelEquipmentGroupingTests` av samma anledning: en bar, aldrig `.show()`ad `TreePanel` saknar riktig widget-geometri) och `hazop.QApplication.keyboardModifiers`. 754 tester gröna.

## Förenkla koden + dela upp hazop.py i fler filer (2026-08-17/18)

**Rapport:** Anton: "kan du försöka arbeta lite med att förenkla och förbättra koden i hela programmet. skulle programmet bli bättre av att styckas upp i fler .py?" Tre parallella Explore-agenter kartlade `hazop.py` (22 361 rader, ~65 klasser) och `pid_viewer.py` (10 898 rader, 25 klasser) — båda kraftigt överdimensionerade "gudfiler". Planerat i detalj i plan mode innan implementation (`polymorphic-booping-papert.md`); Anton valde det mest ambitiösa alternativet: städa död kod/dubbletter OCH göra en full uppdelning i fler filer i en enda lång session, inklusive att dela upp pid_viewer.py:s två "jättar" (`PIDGraphicsView`/`PIDPanel`) — se `CLAUDE.md`s Architecture-avsnitt för den fullständiga, aktuella modul/lager-listan; detta avsnitt fokuserar på VAD som hände och VARFÖR, inte på att duplicera den referensen.

**Genomförande, i ordning, `py_compile` + full regressionssvit (752 tester) grön efter VARJE steg, en commit per steg:**
1. **Död kod borttagen** — `RiskBadge`/`SafeguardEditor`/`WelcomePanel`/`NodePanel`/`ConsequencePanel`/`SafeguardPanel` (~700 rader) instansierades bara i en `QStackedWidget` som aldrig lades till i någon synlig layout — ersattes tidigare av `PropertiesRibbon` men togs aldrig bort.
2. **Dubblettmetoder i `PIDPanel` slogs ihop** — fyra nästan identiska metodpar (nodmarkup- vs redmarkup-läge) parametriserades till `_enter_markup_mode`/`_exit_markup_mode`/`_set_markup_tool`/`_refresh_markup_overlays(markup_class='node'|'red')`; de publika metoderna kvarstår oförändrade som tunna wrappers.
3. **`MainWindow` frikopplad från panelernas privata internals** — innan panelerna flyttades till egna filer fick de publika motsvarigheter för det `MainWindow` tidigare nådde in i direkt (`ScenarioTablePanel.rebuild()`/`schedule_rebuild()`/`get_equipment_filter()`/`position_near_row()`, en riktig `equipment_renamed`-signal istället för att sätta en callback-attribut direkt, `EquipmentPanel.set_db()`/`autodetect()`, `SettingsPanel.refresh_tag_memory()`).
4. **10 nya moduler extraherade** ur `hazop.py`/`pid_viewer.py` via ett genomgående **lager + återexport**-mönster: varje modul importerar bara från lager UNDER sig, och varje lager återexporterar namnen dess anropare redan förlitade sig på — `from hazop import X`/`from pid_viewer import Y` fungerar oförändrat oavsett vilken fil `X`/`Y` faktiskt bor i nu. `test_regression.py` behövde i praktiken noll ändringar av denna anledning (bara enstaka `mock.patch()`-strängar, se nedan). Resultat: `hazop.py` krympte från 22 361 till ~3 050 rader (−86%), `pid_viewer.py` från 10 898 till ~5 100 rader.
5. **Två genuint cirkulära beroenden löstes med fördröjd (lazy) import** istället för modulnivå-import: `HAZOPWorksheet.__init__` (i `worksheet.py`) gör `from hazop import ScenarioTablePanel` INUTI `__init__`, inte på modulnivå, eftersom `ScenarioTablePanel` (i `scenario_panel.py`) importeras TILLBAKA in i `hazop.py` — en modulnivå-import i `worksheet.py` hade blivit cirkulär. Samma mönster för `ScenarioTablePanel._open_recommendation_editor`s `from hazop import RecommendationEditorDialog`. `pid_viewer.py`s egen `from pid_graphics_view import PIDGraphicsView` / `from pid_panel_mod import PIDPanel, EquipmentDeviationBar`-återexport löstes annorlunda: placerad längst NER i filen (efter att alla delade konstanter/hjälpfunktioner redan är definierade), inte högst upp.

**Återkommande testfälla, hittad och fixad sex separata gånger under arbetet:** `unittest.mock.patch('hazop.QMenu', ...)` (eller vilken klass/Qt-inbyggd som helst) fångar bara ett anrop om den KONSTRUERANDE koden fortfarande bor i den modulen. Flyttas koden till en ny fil utan att uppdatera patch-strängen ger INGET fel — patchen blir bara tyst verkningslös, och en RIKTIG (ofta modal, `.exec()`-blockerande) dialog konstrueras istället, vilket kan HÄNGA testsviten (inte bara fela den) tills den isoleras via en verbose testkörning. Drabbade: `hazop.QMenu` → `tree_panel.QMenu` (`TreePanel._context_menu`, 5 tester) och → `scenario_panel.QMenu` (`ScenarioTablePanel._on_context_menu`, 1 test); `hazop.CauseTagPopup`/`CauseObjectPopup` → `scenario_panel.X` (`_show_cause_obj_popup`); `hazop.ReductionFactorsDialog` → `scenario_panel.ReductionFactorsDialog` (`_edit_extra`, en riktig ~500s hängning innan den isolerades); `pid_viewer.QDrag` → `pid_graphics_view.QDrag` (7 tester, `PIDGraphicsView`s drag-and-drop); `pid_viewer.SimilarSymbolSearchDialog`/`SymbolTemplatePickerDialog`/`EquipmentMarkerReviewDialog` → `pid_panel_mod.X` (12 tester). Patchar av typen `patch.object(hazop.Klass, 'metod', ...)` eller `patch('hazop.Klass.metod', ...)` berördes ALDRIG av detta — de muterar det delade klassobjektets egen `__dict__`, oavsett vilken modul man når klassen via.

**Ett par genuina, om än smala, buggar som denna omflyttning annars hade exponerat, fixade i förbigående:** `pid_viewer.py`s PIL-importskydd satte bara `HAS_PIL = False` i `except`-grenen, inte `_PILImage = None` (till skillnad från motsvarande `pytesseract`/`QSvgRenderer`-mönster i samma fil) — ofarligt så länge allt låg i samma fil (koden bakom `if HAS_PIL:` nåddes aldrig), men hade gett ett omedelbart `ImportError` vid modulimport på ett system utan PIL installerat efter att `_PILImage` blev en ovillkorlig `from pid_viewer import _PILImage` i `pid_graphics_view.py`. Fixat genom att lägga till `_PILImage = None` i `except`-grenen, konsekvent med det befintliga mönstret.

**Metod, inte bara resultat:** varje ny fils beroenden identifierades genom att extrahera kandidatblocket till en temp-fil och `grep`-jämföra mot varje redan skapad moduls faktiska namnrymd (constants/database/pid_viewer/ui_helpers/tree_panel/etc), snarare än att gissa. Trots detta missades enstaka modulnivå-konstanter/stdlib-importer flera gånger (t.ex. `CONFIG`/`HAS_PYMUPDF`/`Path` i `equipment_panel.py`; `functools.partial` i `settings_panels.py`; `Z_PAGE`/`Z_HIGHLIGHT`/.../`Z_TEMP`, `HAS_PIL`/`HAS_TESSERACT`/`HAS_EASYOCR`, `_PageRenderer`, `fitz`, `os`/`shutil`/`tempfile`, `symbol_geometry`/`equipment_detection` i pid_viewer.py-uppdelningen) — dessa ger omedelbara, tydliga `NameError`/`ImportError` vid nästa `py_compile`+importkontroll+testkörning, så risken är extra iterationer, inte tyst felaktigt beteende. Ett försök att bygga ett AST-baserat statiskt verktyg för att hitta alla odefinierade namn i förväg övergavs — för många falska positiva från nästlade closures och lokala (fördröjda) importer gjorde det mindre pålitligt än att bara köra kompilera+importera+testa-cykeln, som redan var etablerad från tidigare steg.

**Slutresultat:** 15 moduler (`constants.py`, `database.py`, `ui_helpers.py`, `tree_panel.py`, `node_markup.py`, `worksheet.py`, `scenario_panel.py`, `equipment_panel.py`, `settings_panels.py`, `hazop.py`, `pid_viewer.py`, `pid_graphics_view.py`, `pid_panel_mod.py`, plus de redan existerande `equipment_detection.py`/`symbol_geometry.py`/`image_symbol_matching.py`), 752 tester gröna, `test_regression.py` med minimal diff (bara `mock.patch()`-strängar pekade om, aldrig testlogik). `CLAUDE.md`s Architecture-avsnitt uppdaterat med hela lager-listan och en varning om patch-fällan ovan för framtida filflyttar.

**Efterföljande verkliga kraschar (2026-08-18):** trots ovanstående metod hittade Anton tre riktiga `NameError`-kraschar i den faktiska appen kort efter pushen — `Path` i `PIDManagementPanel.refresh()` (settings_panels.py), `_StylePopup` och `ConsCategoryMatrixPopup` i node_markup.py. Alla tre triggade bara vid RIKTIG data (en revision med `pdf_path` satt) eller RIKTIG interaktion (klick på ett ritverktyg) — ingen befintlig test konstruerade panelerna på det sättet. Ett riktigt AST-baserat verktyg (denna gången utan de nästlade-closure-relaterade falska positiva som fick det tidigare försöket att överges — se ovan) kördes i efterhand mot ALLA tio flyttade moduler jämfört med git-historikens förextraktions-innehåll och hittade två till (samma node_markup.py-problem, samt fyra saknade `equipment_detection`-OCR-hjälpare i pid_graphics_view.py/pid_panel_mod.py, bara nådda vid faktisk taggigenkänning).

## Snabbare testcykel: test_smoke.py (2026-08-18)

**Rapport:** Anton: "Det är väldigt omfattande att köra full regression test efter varje ombyggnation... Jag skulle vilja begränsa regression test till kanske 10 test efter varje build. Med möjlighet att full regression test på begäran. Dock är det bra att du bygger ut alla regression test eftersom precis som du gör idag." — den fulla sviten (750+ tester) tar 4-5 minuter, för långsam för varje enskild ändring under iterativt arbete.

**Genomförande:** Ny `test_smoke.py` (11 tester, <1 sekund) — INTE en ersättning för `test_regression.py`, utan ett snabbt förhandstest. Designad specifikt kring de tre riktiga kraschklasserna ovan (data-beroende, interaktions-beroende): seedar en liten men REALISTISK databas (en nod/avvikelse/orsak/konsekvens/safeguard, EN REVISION MED pdf_path SATT, ett utrustningsobjekt) och konstruerar varje större panel mot den — inklusive att klicka på VARJE nodmarkup-/redmarkup-ritverktygsknapp på riktigt (inte bara konstruera panelen). Verifierat empiriskt att den fångar båda de faktiska kraschklasserna genom att temporärt återinföra den trasiga `node_markup.py`-koden och bekräfta att testet då faller.

**Teknisk fälla hittad under byggandet:** PyQt6:s standardbeteende för en ohanterad exception INUTI ett signal/slot-anrop (t.ex. `knapp.click()`) är att skriva ut den via `sys.excepthook` och sedan AVBRYTA HELA PROCESSEN, inte låta anroparen fånga den — ett buggigt knappklick i ett test hade tyst dödat hela testkörningen istället för att fela EN test. Löst genom att temporärt byta ut `sys.excepthook` under klicket och samla upp undantaget för att sedan `self.fail()` med det, istället för att förlita sig på normal Python-exception-propagering.

**Nytt arbetsflöde (dokumenterat i CLAUDE.md):** `python -m unittest test_smoke -v` efter varje kodändring under iterativt arbete; `python -m unittest test_regression` (fulla sviten) innan commit, efter en stor/riskfylld ändring, eller på begäran. Fortsätt bygga ut `test_regression.py` som vanligt vid varje bugfix/funktion — `test_smoke.py` är ett komplement, inte en ersättning. Känd begränsning: `test_smoke.py` täcker INTE djupa interaktionsvägar som OCR/tagg-skanning (de kräver fortfarande fulla sviten).

## Analystillfälle utan popup, inline-döp om header (2026-08-18)

**Rapport:** Anton bad om att skapandet av ett analystillfälle i Deltagarmatrisen inte längre ska öppna en popup, och att kolumnrubriker (analystillfällen + egna kolumner) ska kunna döpas om direkt i tabellhuvudet.

**Genomförande:** `ParticipantMatrixPanel._add_session()` (settings_panels.py) skapar nu kolumnen direkt med dagens datum som defaultetikett (`QDate.currentDate()`) — ingen `_AnalysisSessionDateDialog`-popup längre, klassen borttagen helt. Direkt efter skapandet startas en inline-redigering av den nya rubriken automatiskt så användaren kan justera datumet/etiketten utan att lämna tabellen. Ny `_edit_header_label(col)` (kopplad till `horizontalHeader().sectionDoubleClicked`) lägger en `_InlineHeaderEdit` (QLineEdit-subklass, Escape avbryter utan att spara — vanlig QLineEdit har inget sådant beteende inbyggt utanför en item-delegate) som barn direkt på headern, positionerad över kolumnsektionen. Fungerar för både analystillfällen (`update_analysis_session`, fanns redan) och egna kolumner (ny `Database.update_participant_column()`). De fasta Förnamn/Efternamn-kolumnerna är inte omdöpningsbara (`_header_kind()` returnerar `None` för dem).

**Test:** `_AnalysisSessionDateDialog`-mockningen i fyra befintliga tester (`test_panel_add_session_creates_column` m.fl.) ersatt med direkta `panel._add_session()`-anrop; ny etikettassertion är en regex (`\d{4}-\d{2}-\d{2}`) istället för ett hårdkodat värde eftersom datumet nu kommer från `QDate.currentDate()`. Fem nya tester: inline-redigering döper om session/egen kolumn, Escape avbryter utan att spara, dubbelklick på en fast kolumn gör ingenting, ny kolumn startar redigeringsläget direkt. Full svit (766 tester) grön.

## Trädet: numrering bryts ut ur inline-redigeringen (2026-08-18)

**Rapport:** Anton: "När jag dubbelklickar på trädet så försvinner numreringen. Kan du bryta ut nummereringen så denna inte påverkas av namngivningen." — Fas E1s inline-redigering (samma dag, se ovan) bytte hela cellens dekorerade text ("  ⬡  1. Beskrivning") mot bara den råa beskrivningen medan man redigerade, vilket fick numret/ikonen att försvinna tills en full `refresh()` efteråt återställde den.

**Genomförande:** `TreePanel._begin_inline_edit()` (tree_panel.py) använder inte längre Qt:s inbyggda `editItem()`/`itemChanged`-mekanism (som bara kan redigera HELA cellens text). Istället läggs en flytande `_InlineTreeEdit` (QLineEdit-subklass, Escape avbryter utan att spara) som barn på `tree.viewport()`, positionerad EFTER radens numrerings-/ikonprefix — själva `QTreeWidgetItem`s kolumn-0-text rörs aldrig, så numreringen syns hela tiden, även under redigeringen. Varje rads prefix ("  ⬡  1. ", "    ⚙ 2. " osv) sparas nu separat som item-data (`_PREFIX_ROLE = UserRole + 2`) vid byggtillfället i `refresh()`, istället för att försöka räkna ut var prefixet slutar och den råa texten börjar i efterhand (skulle inte gått pålitligt: KON/SG-radernas visningstext trunkeras vid 40/35 tecken, och en "kaka på kaka"-sammanslagen orsaksrad kan visa en helt annan text — utrustningstaggen — än sin egen råa beskrivning). Editorns x-position räknas ut från `QFontMetrics(item.font(0)).horizontalAdvance(prefix)` plus en ikonbredds-approximation (`_PREFIX_ICON_W = 18`) för Nod-raden (enda typen med en riktig `QIcon`, övriga har ikonen inbakad som emoji-tecken i själva textsträngen).

**Bieffekt:** `_on_tree_item_text_edited`/`itemChanged`-kopplingen (fanns bara för detta syfte) är borttagen helt — commit-logiken (skriv till DB, `refresh()`, emit `item_edited_inline`) flyttad till en fristående `_commit_inline_text(type_, id_, text)` som anropas direkt av editorns `editingFinished`. `ItemIsEditable`-flaggan sätts inte längre alls på trädobjekten (behövdes bara för Qt:s inbyggda editering).

**Test:** `TreeInlineEditTests._commit()`-hjälparen (test_regression.py) anropar nu `_commit_inline_text` direkt istället för att simulera `itemChanged`. Omskriven `test_double_click_deviation_starts_inline_edit` verifierar att `item.text(0)` FÖRBLIR den dekorerade texten (inte längre den råa beskrivningen) och att en flytande `QLineEdit` hittas i `tree.viewport()`. Två nya tester: `test_numbering_prefix_survives_inline_edit` (direkt regressionstest för den rapporterade buggen — "1." syns både under och efter redigeringen) och `test_escape_cancels_inline_edit_without_saving`. `test_editable_flag_cleared_after_commit` omdöpt/utökad till `test_editable_flag_never_set_by_inline_edit`. 768 tester gröna.

## Trädet: dubbelklick på taggrad öppnar Tag+Typ-popup, ingen OK/Avbryt (2026-08-18)

**Rapport:** Anton, direkt uppföljning på ovanstående numrerings-fix: "Nu funkar det bättre men dubbelklickar jag på ett objekt (taggen) så kommer texten avikelsetexten upp." Rotorsak: den "kaka på kaka"-sammanslagna utrustningsraden (en enda avvikelse per utrustning+ledord, se refresh()) visar utrustningens tagg men BÄR identiteten DEV_T/CAUSE_T internt (för drag-and-drop/"lägg till orsak" m.m.) — dubbelklick föll därför igenom till den vanliga inline-textredigeringen och visade avvikelsens/orsakens råa text istället för något taggrelaterat. Anton bad dessutom uttryckligen om att återanvända SAMMA Tag+Typ-popup (`CauseTagPopup`, redan i bruk för ett klick på ORS-taggzonen i HAZOP scenario-tabellen) för trädets dubbelklick, och att ta bort OK/Avbryt-knapparna helt: "Du kan ta bort dialogrutorna ok och avbryt och låta mig trycka och tillåta redigering utan bekräftande knapptryck."

**Genomförande:**
- Ny `TreePanel._EQUIP_TAG_ROLE` (item-data, satt på VARJE utrustnings-header-rad oavsett om dess egen type_/id_-identitet är EQUIP_T eller den sammanslagna DEV_T/CAUSE_T-varianten). `_on_item_double_click` kollar denna rollen FÖRE `_INLINE_EDIT_TYPES`-grenen, så en taggrad alltid öppnar tag-popupen — aldrig inline-textredigering av den avvikelse/orsak den råkar stå in för.
- `TreePanel._assign_equipment_type` (QInputDialog.getItem, bara typval) ersatt av `_open_equipment_tag_popup`/`_apply_equipment_tag_edit`, som öppnar `CauseTagPopup` (tag_panel.py) positionerad vid radens `visualItemRect()` och skriver båda fälten till `equipment_catalog` via `update_equipment_item`.
- `CauseTagPopup` (tree_panel.py) gjord om till en självstängande `Qt.WindowType.Popup`-ruta utan OK/Avbryt-knappar — tag-fältets `editingFinished` (Enter/fokusbyte) och typ-comboboxens `activated` committar var för sig direkt, ingen väntar på ett knapptryck. Dess enda tidigare anropsplats (`scenario_panel._show_cause_obj_popup`, klick på ORS-taggzonen) uppdaterad från `.exec()` till `.show()` eftersom popupen inte längre är modal och inte har något att "acceptera".

**Test:** `test_double_click_undefined_equipment_opens_type_picker_and_persists`/`..._emits_item_edited_inline` (tidigare `QInputDialog.getItem`-mockade) skrivna om mot den riktiga `CauseTagPopup`-instansen (`panel.findChildren(CauseTagPopup)`, sätter combon och anropar `_commit()` direkt). De två "kaka på kaka"-testerna från föregående omgång uppdaterade till att verifiera att popupen öppnas (inte längre att `QInputDialog.getItem` anropas). Ny `test_equipment_tag_popup_edits_tag_and_type_live` verifierar att både tagg- och typfältet skriver till DB var för sig utan någon knapp. `scenario_panel`-sidans `test_tag_zone_click_opens_cause_tag_popup_not_cause_object_popup` uppdaterad från `fake_popup.exec.assert_called_once()` till `.show`. 771 tester gröna.

## Objektets identitet: P&ID, HAZOP scenario, trädet och Utrustningsregistret bundna ihop (2026-08-18)

**Rapport:** Anton: "Objektets identitet på P&ID, HAZOP scenario och trädet måste höra ihop. Bind dessa så de lirar och alltid på alla tre ställen oavsett var man editerar. Jag tror även det finns någon databas för objektet i programmet men hittar inte denna fliken." Följt av en precisering: "Den vinröda / brunröda texten uppdateras alltså inte som den ska på det utsatta objektet på P&ID just nu" — bekräftade att felet satt i markörens EGEN etikett på P&ID-canvasen, inte i scenariotabellen eller trädet.

**Svar på andra delen:** databasen han letade efter finns redan — **"Utrustning"-fliken** i navigationsfältet (svart fält till vänster, blixt/mutter-ikon), byggd av `EquipmentPanel` (`equipment_panel.py`). Den visar hela `equipment_catalog`-tabellen (Utrustningsregister) med tagg/prefix/sida/OCR/typ/beskrivning, redigerbar direkt i tabellen, plus knappar för att skanna P&ID/autodetektera/skapa HAZOP-noder.

**Rotorsak till bindningsbristen:** `equipment_catalog` (tabellen `EquipmentPanel` visar) är redan den enda sanningskällan som TRÄDET och SCENARIOTABELLEN läser LIVE via en `equipment_id`-FK (`ScenarioTablePanel._cause_tag_display`/`_equipment_for_dev`, befintligt sedan 2026-08-13) — men P&ID-MARKÖRERNAS egen `equipment_markers`-tabell har SINA EGNA `tag`/`comp_type`-kolumner, satta en gång vid utplaceringstillfället och ALDRIG uppdaterade efteråt. `PIDPanel._load_overlays()` läste dessa frusna kolumner direkt istället för att slå upp `equipment_catalog` via `equipment_markers.equipment_id` (samma FK-mönster som redan fanns, bara oanvänt här) — det är den vinröda/bruna markörtexten han syftade på.

**Genomförande, fyra separata luckor:**
1. **`PIDPanel._load_overlays()`** (pid_panel_mod.py) — löser nu `tag`/`comp_type` LIVE från `equipment_catalog` via `equipment_markers.equipment_id` när kopplad, annars faller tillbaka till markörens egna frusna kolumner (okopplad markör). Samma fix i `_export_pdf()`s motsvarande loop, så en PDF-export inte visar en förnamnd taggs GAMLA namn.
2. **`_EquipmentTableModel`** (equipment_panel.py) — ny `identity_changed`-signal emitteras när tagg/typ ändras inline i Utrustningsregistret; `EquipmentPanel` vidarekopplar den till sin egen redan kopplade `markers_saved`-signal (ingen ny parallell signalkedja). Innan denna ändring uppdaterade en inline-redigering här ingenting alls utanför tabellcellen.
3. **`MainWindow.__init__`** — `equipment_panel.markers_saved` kopplas nu även till `scenario_panel.schedule_rebuild` och `tree_panel.refresh` (var bara kopplad till `pid_panel.reload_overlays` innan).
4. **`scenario_panel.equipment_renamed`** (redan kopplad till `pid_panel.reload_overlays` sedan 2026-08-13) kopplas nu ÄVEN till `tree_panel.refresh`. **`MainWindow._on_equipment_edit_requested`** (P&ID:s "✏️ Redigera objekt") anropade redan `pid_panel.reload_overlays()` + `scenario_panel.schedule_rebuild()` men glömde `tree_panel.refresh()` — tillagd.

**Test:** `EquipmentIdentityLiveResolveTests` (2 tester) — markören visar det nya namnet efter en direkt `equipment_catalog`-omdöpning, en okopplad markör (equipment_id=None) behåller sin egen frusna tagg. `EquipmentIdentityCrossPanelSyncTests` (2 tester) — verifierar FUNKTIONELLT (riktiga trädrader/scenariotabellceller efter en riktig omdöpning) snarare än genom att mocka de anslutna slot-metoderna direkt: ett `.connect(self.tree_panel.refresh)`-anrop binder till den URSPRUNGLIGA metoden vid anslutningstillfället, så att i efterhand patcha `tree_panel.refresh` på instansen fångar INTE upp anrop som redan går via den etablerade Qt-signalen (upptäckt när de första mockbaserade testförsöken tyst föll igenom trots att den riktiga koden fungerade). Ny `test_export_shows_renamed_equipment_tag_not_stale_marker_tag` i `ExportPdfMarkupTests`. `EquipmentEditRequestedHandlerTests.test_committing_new_tag_and_type_updates_existing_catalog_row` utökad med samma `tree_panel.refresh`-assertion. 775 tester gröna.

## Trädet öppnar sig by default bara till "objektet" (2026-08-18)

**Rapport:** Anton: "För att få bättre överblick i trädet vill jag att du by default inte öppnar upp trädet mer än till objektet. Dvs du kan skippa orsakstexten, konsekvensen och safeguards. Dessa skall ju såklart vara öppna manuellt som idag men inte så fort de läggs till."

**Rotorsak:** `TreePanel.refresh(select_type, select_id, ...)` kallade `self.tree.scrollToItem(target)` för att göra ett nytillagt/valt objekt synligt — verifierat direkt mot PyQt6 (litet fristående testskript) att `setCurrentItem()` ensamt INTE expanderar något alls, men `scrollToItem()` tyst expanderar VARJE hopfälld förälder ända upp till roten för att objektet ska bli synligt. Eftersom varje ny orsak/konsekvens/safeguard — oavsett om den läggs till från trädet, HAZOP scenario eller någon annanstans — går via `refresh(select_type, select_id)`, vecklade trädet successivt ut sig helt bara av att man arbetade, vilket ätit upp exakt den överblick trädet är tänkt att ge.

**Genomförande:** Ny `TreePanel._reveal(item)` ersätter de två `setCurrentItem()+scrollToItem()`-paren i `refresh()`. Den sätter alltid `setCurrentItem()` (ofarligt), men hoppar över det tvingande `scrollToItem()`-anropet om objektets typ är `CAUSE_T`/`CONS_T`/`SG_T` (ny `_COLLAPSE_BY_DEFAULT_TYPES`) — såvida inte HELA förälderkedjan redan råkar vara expanderad sedan tidigare (då finns inget att tvinga upp, och scroll dit är bara en bekvämlighet). Nod/Ledord/Utrustning/Avvikelse ("objektet") påverkas inte — de fortsätter avslöjas automatiskt precis som förut. Manuell expandering (klick, pil-höger, "Expandera allt"-knappen) är helt oberörd — bara den AUTOMATISKA avslöjningen vid tillägg/val stryps.

**Gränsdragning:** Orsak (`CAUSE_T`) räknas som "under objektet", inte som en del av det — Antons egen formulering radade upp "orsakstexten, konsekvensen och safeguards" som tre separata saker att skippa, så en orsaksrads EGEN synlighet (inte bara dess barn) hålls stängd som standard, precis som konsekvens/safeguard.

**Test:** Ny `TreeAutoExpandCappedAtObjectLevelTests` (6 tester) — en tillagd konsekvens/safeguard öppnar inte sin ägande orsak/konsekvens, ett valt orsaksobjekt tvingar inte heller upp sin egen förälderkedja, en avvikelse ("objektet") fortsätter avslöjas som förut, en redan manuellt öppnad gren störs inte, och "Expandera allt"-knappen är helt opåverkad. 781 tester gröna.

## P&ID: kombinerad objekt+typ+avvikelse-meny med async taggsökning (2026-08-18)

**Rapport:** Anton: "När jag håller högerknappen och markerar med gummibandet får jag upp menyn att definera objekt + objekttyp. Detta kan slås ihop med avvikelser dvs att jag definerar objekt + objekttyp + avikelse i listan. Idag sker detta i två steg men jag tror det snabbar upp flödet med bara en meny. Dessutom vill jag att menyn dyker upp direkt även om inte en tag har kunna identiferas och hellre någon form av timglas eller liknande som visar att den fortfarande letar efter en tag. ... Jag vill också implemetera en inställning som gör hur länge programmet maximalt letar efter en tag. Standard ska vara 2 sekunder, sedan avbryts det för manuellt inmatning. Lägg denna funktion på P&ID inställningar."

**Före:** rubberband-släpp/högerklick "🔧 Objekt" körde native-text/OCR-taggsökning SYNKRONT (`PIDGraphicsView._extract_tag_from_rect`/`equipment_detection.find_tag_near_point`) innan `EquipmentTagPopup` (tagg+typ, modal) ens visades — sedan öppnade `place_equipment_marker()` AUTOMATISKT `EquipmentDeviationBar` (avvikelse-kryssrutor) som ett andra, separat popup-steg.

**Genomförande:**
1. `equipment_detection.extract_tag_from_rect(pdf_doc, page_num, x0,y0,x1,y1)` — ren funktion, kroppen flyttad ut ur `PIDGraphicsView._extract_tag_from_rect` (nu en tunn wrapper) så den går att köra i en bakgrundstråd med sitt EGET `fitz.Document`.
2. Ny `EquipmentTagSearchWorker(QThread)` (pid_viewer.py, bredvid `EquipmentAnalysisWorker`) — kör `extract_tag_from_rect` + `find_tag_near_point`-fallback (rektangelfallet) eller bara `find_tag_near_point` (vanligt högerklick), emitterar `finished_search(str)` exakt en gång.
3. `_on_zone_drawn`/`_on_context_action('equipment')` (pid_panel_mod.py) emittar nu `equipment_placement_requested` OMEDELBART med `tag=''` — ingen synkron sökning kvar.
4. Ny `_DeviationChecklist(QWidget)` — kryssruteledlogiken (`_rebuild_checklist`/`_build_deviation_row`/`_activate_deviation`/`_deactivate_deviation` m.fl.) extraherad ur `EquipmentDeviationBar`, som nu är ett tunt skal (titel + denna widget) med oförändrat publikt API — inbäddningsbar, återanvänd rakt av i den nya menyn.
5. Ny `EquipmentPlacementPopup` — Tag-fält + Typ-combo (samma "+"-lägg-till-typ-mönster som `EquipmentTagPopup`) + inbäddad `_DeviationChecklist`, INGA OK/Avbryt-knappar (samma no-confirm-button-mönster som `CauseTagPopup` fick tidigare samma dag) — tagg/typ committar live. En "⏳ Söker tagg…"-etikett visas medan sökningen pågår.
6. `PIDPanel.place_equipment_marker()` — skapande-logiken ("hitta befintlig via tagg annars skapa ny") oförändrad, men visar nu `EquipmentPlacementPopup` istället för `_equipment_bar`, och om tagg var tom vid anropet startar `_start_equipment_tag_search()` workern + en `QTimer.singleShot(timeout_ms, ...)` — vilken som blir klar först vinner (`state['done']`-flagga). `equipment_tag_search_timeout_ms` läses från `db.get_config(...)`, standard `'2000'`.
7. Ny inställning i "P&ID-inställningar" (`SettingsPanel`): `QDoubleSpinBox` "Max söktid per tagg" (0.5–10s, steg 0.5, standard 2.0).
8. En tagg som skrivs in/upptäcks EFTER att en tom platshållarrad redan skapats kan visa sig redan finnas i katalogen — `EquipmentPlacementPopup._reassign_to_existing()` slår då ihop till den befintliga raden (tar bort platshållaren) om den ännu inte har någon riktig data (inga ikryssade avvikelser); annars lämnas den som en (sällsynt, informativ) dubblett hellre än att riskera att förlora redan sparad data.

**Bieffekt/bugfix upptäckt under arbetet:** `EquipmentDeviationBar.db` kunde tidigare bara bytas ut som en enkel attributtilldelning (`MainWindow._reload_all_panels()`) — sedan `_DeviationChecklist`-extraktionen har den en EGEN `db`-kopia som inte uppdaterades av det. Fixat med en `db`-property som propagerar tilldelningen vidare till den inbäddade checklistan.

**Test:** `EquipmentTagSearchWorkerTests` (workern hittar tagg i rektangel/fallback/punkt, emitterar alltid även vid ogiltig sökväg), `EquipmentPlacementAsyncSearchTests` (spinnervisning, sen träff skriver inte över en redan itippad tagg, timeout lämnar fältet tomt, dubblett-sammanslagning med/utan redan kopplad data, kryssruteflödet skapar orsaker). `test_placement_opens_equipment_deviation_bar` omdöpt/omskriven mot den nya popupen. Ny inställningstest för timeout-värdet. 799 tester gröna (se nästa avsnitt för resten av ökningen).

## HAZOP scenario: frekvens ner i orsaksfältet, ta bort pluppen, avdubblera taggen (2026-08-18)

**Rapport:** Anton, med en print screen (`Screenshot 2026-08-18 134727.png`): "Frekvensen som står i hazop scenario skall stå längst ut till höger men flyttas från objektbannern till orsaksfältet då det hör hemma mer här. Varje orsak skall ha en frekvens. Detta gör också att när man står på ett objekt i hazop trädet behöver inte dubbla objektbanners visas som idag. Du kan även skrota pluppen som syns som grön och orange baserat på vad som är ifyllt." En uppföljningsfråga bekräftade exakt vad "dubbla objektbanners" syftade på: när Utrustning-kolumnen (`_C_UTR`) redan är synlig (bara i `load_all()`s "Visa samtliga noder"-läge — annars döljs den redan sedan tidigare, se `_set_all_nodes_columns_visible`s egen dokumentation om exakt samma dubblett-problem från en tidigare omgång) visar ORS-remsan SAMMA taggidentitet en gång till.

**Genomförande**, allt i `_PidDelegate.paint()`s ORS-gren (scenario_panel.py) + dess delade geometrihjälpare:
1. **Statuspluppen borttagen helt** — `_STATUS_COLORS`-uppslagningen och dess `drawEllipse`-anrop borttagna. Kommentar-pluppen (svart, visar om orsaken har en kommentar) rörd inte. Den underliggande ifyllnadsstatus-beräkningen (`_status_icon`/`_status_tip`) behålls — används fortfarande för cellens tooltip, bara den visuella pluppen (och dess nu döda `UserRole+6`-lagring på ORS-posten) är borta.
2. **Frekvensen flyttad ur remsan, ner i orsaksfältet, högerställd** — ny `_ORS_HEADER_H = _ORS_STRIP_H * 2` (tagg-remsa + en ny frekvensrad, samma höjd som remsan) ersätter `_ORS_STRIP_H` överallt en rads höjd räknas ut (sizeHint/`_compute_row_height`/`_wrap_col_row_height`/`updateEditorGeometry`). Gamla `_ors_tag_zone_geometry()` (returnerade tagg- OCH frekvensgeometri ihop) delad i två: `_ors_tag_zone_width()` (taggzonen, nu bara begränsad av kommentar-pluppens marginal, ingen frekvens att dela utrymme med längre) och `_ors_freq_zone_geometry()` (frekvenszonen, högerställd inom orsaksfältets egen bredd). Klick-hit-testet i `eventFilter()` flyttat på samma sätt (frekvensklick kollar nu `_ORS_STRIP_H <= y < _ORS_HEADER_H` istället för `y < _ORS_STRIP_H`).
3. **ORS-remsans egen tagg döljs när Utrustning-kolumnen redan visar den** — `has_tag = bool(comp_tag or comp_type) and not self._table.isColumnHidden(self._C_UTR)`. Taggzonens klickyta (öppnar `CauseTagPopup`) förblir aktiv oavsett — bara den DUBBLA visningen togs bort, inte redigeringsvägen.

**Test:** Ny `OrsStripReworkTests` (3 tester, pixel-rendering av en riktig cell): statuspluppen (röd/grön) ritas aldrig, taggen syns i normalläget (`load_node()`, Utrustning dold som vanligt) men försvinner i `load_all()`-läget (Utrustning synlig), frekvenstext syns i den NYA raden under remsan men inte längre i själva remsan. Befintliga `OrsStripTagFreqLayoutTests`/`OrsFrequencyZoneClickTests`/`OrsStripHeightConsistencyTests` uppdaterade mot de nya delade geometrimetoderna och den nya `_ORS_HEADER_H`-höjden — flera av deras gamla premisser (tagg-zonen "återtar utrymme från frekvensen") är numera överflödiga eftersom frekvensen inte längre delar remsan alls. 799 tester gröna totalt (Del 1 + Del 2 tillsammans).

## Rättning: dubbla dialogfönster vid P&ID-placering, frekvens/beskrivning på samma rad, kompaktare skyddsrader (2026-08-18, samma dag, uppföljning)

**Rapport 1:** Anton, direkt efter ovanstående två avsnitt gick live: "Nu är det dubbla dialogfönster när jag drar höger med gummibandet. Först en med objekt och sedan en med objekt och sedan en med utrustning + objekttyp + avvikelser. Står jag någonstans i en nod skall det vara denna som objektet läggs till till. Avikelselistan att välja från skall också stämma överens med dem i noden."

**Grundorsak:** `MainWindow._on_equipment_placement_requested` (hazop.py) hade av misstag ALDRIG uppdaterats när `place_equipment_marker()` fick sin nya, egna `EquipmentPlacementPopup` (se föregående avsnitt) — den byggde och visade fortfarande den GAMLA `EquipmentTagPopup` (tagg+typ) själv, och anropade `place_equipment_marker()` FÖRST i dess `committed`-callback. Resultatet blev exakt två popup-fönster i rad för samma placering, precis som rapporterat.

**Fix:** `_on_equipment_placement_requested` kastade den gamla `EquipmentTagPopup`-koden och anropar nu `self.pid_panel.place_equipment_marker(suggested_tag, '', scene_pos, page, pdf_rect=pdf_rect)` direkt — ett enda anrop, en enda popup. De två andra klagomålen (fel/ingen aktiv nod, avvikelselistan matchar inte noden) visade sig vara SYMPTOM av samma bugg, inte separata fel: `place_equipment_marker()` skickade redan korrekt `active_node_id=self._active_node_id` vidare till `popup.load_checklist()` → `_DeviationChecklist.load()` (som redan auto-tilldelar till aktiv nod och filtrerar avvikelselistan därefter) — det gamla extra dialogsteget körde bara aldrig den koden, så det såg ut som att den saknades.

**Rapport 2** (mitt i samma turordning, innan föregående fix ens hunnit svaras på): "Orsaksbeskrivningstexten och frekvensen hamnar nu på olika rader vilket tar onödigt mycket plats. Dessutom vill jag att du krymper höjden på safeguards till 1/3. För att spara plats när man lägger till flera safeguards."

**Fix, frekvens/beskrivning:** Frekvensens egna reserverade rad (`_ORS_HEADER_H = _ORS_STRIP_H * 2`, infört bara timmar tidigare samma dag) togs bort igen — `_ORS_HEADER_H` är nu bara `_ORS_STRIP_H` (tagg-remsans egen höjd). Frekvensen ritas istället OVANPÅ orsaksbeskrivningens egen första rad, högerställd, med en ogenomskinlig bakgrundslapp bakom sig (målad EFTER beskrivningstexten) så den läses som en flytande etikett snarare än överlappande text — om beskrivningens första rad råkar vara lång täcks bara det hörnet över. Klick-hit-testet i `eventFilter()` flyttat till samma zon (`_ORS_HEADER_H <= y < _ORS_HEADER_H + _ORS_STRIP_H`).

**Fix, skyddsradernas höjd:** Varje säkerhetsfunktion (SG) i en konsekvens med FLERA skyddsåtgärder får en egen fysisk tabellrad (`_apply_spans` spannar NOD/UTR/DEV/ORS/KON/LOPA/REK/RFORE/SLUT över dem alla) — bara SG-kolumnen har eget innehåll på de extra raderna. `_compute_row_height` tvingade tidigare ändå ALLTID minst `one_line_h` (samma radhöjd som en vanlig textrad) på varje rad, oavsett om raden faktiskt hade eget innehåll i någon annan kolumn eller ej — det var detta, inte SG-textens egen formel, som satte den faktiska golvhöjden. Ny `ScenarioTablePanel._sg_row_height()` är nu den enda källan för hur högt en skyddsrad behöver vara; `_compute_row_height` använder detta golv istället för `one_line_h` för rader UTAN eget innehåll i någon annan kolumn (dvs alla skyddsrader utom den första/ankarraden i en grupp).

**Uppföljning samma dag** ("Det var inte texten i safeguard som skulle krympa till 1/3 utan höjden på själva cellen"): första försöket krympte även TYPSNITTET i SG-cellen (-2 punkter) för att komma närmare en tredjedel — fel tolkning. `_sg_row_height()` använder nu tabellens VANLIGA typsnitt oförändrat, bara paddingen runt texten är trimmad till minimum (`fm.height() + 2` istället för `+ 6`) — texten är exakt lika stor som i alla andra celler, raden bara så kort som en rad text tillåter.

**Test:** `OrsFrequencyZoneClickTests`/`OrsStripReworkTests` uppdaterade mot den nya overlay-geometrin (samma mönster som föregående avsnitts uppdateringar, bara en gång till samma dag). Full `test_regression`-svit (799 tester) grön efter båda fixarna.

## Fixar: tyst avvikelse-refresh-bugg och saknad standardorsak i den nya placeringspopupen (2026-08-18, samma dag, andra uppföljningen)

**Rapport:** Anton: "Just nu behöver jag stå på en avvikelse i trädet. Det ska även räcka med att stå på noden för att jag ska kunna lägga till hierarkin. Lägger jag till något så skall detta också dyka upp i hazop scenario precis som tidigare, dvs alla orsaker tillagda till objektet. Jag ser dessutom inget i hazop scenario när jag klickar." — och strax efter: "Dessutom läggs det inte till någon standardorsak när jag definerat objekttyp + avikelse som innan. saknar detta."

**Reproducerat och verifierat med ett fristående skript** (skapa nod, `set_active_node`, `place_equipment_marker('', '', ...)`, kryssa en avvikelse) innan någon kod ändrades — bekräftade två separata, verkliga buggar i `EquipmentPlacementPopup` (den nya kombinerade popupen från tidigare samma dag), båda missade av `EquipmentPlacementAsyncSearchTests` eftersom dess befintliga test skapade utrustningen med typen redan ifylld, vilket aldrig träffar någon av de två kodvägarna nedan:

1. **`EquipmentPlacementPopup` vidarebefordrade aldrig `deviation_added`/`deviation_removed`** från sin inbäddade `_DeviationChecklist` — till skillnad från `EquipmentDeviationBar`, som redan gör exakt detta. `PIDPanel.place_equipment_marker()` kopplade följaktligen aldrig `_on_equipment_deviation_added`/`_removed` (som anropar `scenario_panel.load_equipment()` och trädets refresh) till den nya popupen. Orsaken/avvikelsen skapades korrekt i databasen, men INGET ritade om trädet eller hazop scenario — exakt "Jag ser dessutom inget i hazop scenario när jag klickar". Fix: nya `deviation_added`/`deviation_removed`-signaler på `EquipmentPlacementPopup` (samma vidarebefordrings-mönster som `EquipmentDeviationBar`), kopplade i `place_equipment_marker()`.
2. **`EquipmentPlacementPopup._commit_type()` byggde aldrig om checklistan** efter att en typ valts — checklistans rader (och deras förberäknade standardorsaks-förslag, `_build_deviation_row`s `causes`-lista) byggs EN gång vid `load_checklist()`, som körs precis efter att objektet skapats med `comp_type=''` (rubberband/högerklicks-flödet skickar alltid en tom typ dit, användaren väljer typ i den HÄR popupen efteråt). Att kryssa en avvikelse efter att ha valt typ skapade avvikelsen men aldrig den typ-matchade standardorsaken, eftersom radens `causes`-lista fortfarande var beräknad med det tomma typvärdet — exakt "läggs det inte till någon standardorsak när jag definerat objekttyp + avikelse som innan". Fix: `_commit_type()` anropar nu `self._checklist._rebuild_checklist()` efter att ha sparat typen.

**Om "att stå på en nod ska räcka" (rapportens första del):** verifierat med samma reproduktionsskript att `_active_node_id` redan sattes korrekt och att utrustningen redan knöts till rätt nod när man bara stod på NODEN (inte en avvikelse) — `set_active_node`/`set_active_deviation`/`set_active_cause`/`set_active_consequence` (pid_panel_mod.py) räknar alla redan ut och sätter samma underliggande `_active_node_id`, oavsett vilken nivå man står på. Detta var alltså inte en separat bugg utan ytterligare ett symptom av bugg 1 ovan — utan refresh-signalen syntes ingenting oavsett vilken nivå man stod på, vilket kan ha sett ut som att bara avvikelse-nivån "råkade fungera".

**Test:** Två nya tester i `EquipmentPlacementAsyncSearchTests`: `test_checking_a_deviation_creates_a_cause_when_type_is_picked_after_placement` (placerar med tom typ, väljer typ i popupen EFTERÅT, kryssar en avvikelse, kontrollerar att standardorsaken skapas) och `test_checking_a_deviation_refreshes_tree_and_scenario` (kontrollerar att `equipment_deviation_created` verkligen avfyras). Full `test_regression`-svit grön efter fixen.

## Objekt-tagg i orsaksbannern: rätta regel (dedupe på upprepning, inte kolumnsynlighet) + två tysta radhöjdsbuggar hittade under tiden (2026-08-18, samma dag, tredje uppföljningen)

**Rapport:** Anton: "Orsaken har tidigare visat objekt-tagen i bannern men denna är nu borttagen. Jag vill att denna syns. Men om det visas flera avikelser efter varandra som tillhör samma objekttagg behöver denna inte repeteras. Då kan tagbannern försvinna på nummer två i listan och nedåt för att spara plats."

**Root cause:** tidigare samma dags "dubbla objektbanners"-fix (se ovan) dolde ORS-remsans tagg med ett BINÄRT villkor — `not utr_visible` — närhelst Utrustning-kolumnen överhuvudtaget var synlig. Det råkade fungera för det ursprungliga fallet (en HEL grupp rader delar samma Utrustning-värde, döljs allihop), men i vyer där Utrustning ALLTID är synlig och EN specifik tagg fyller HELA listan (t.ex. `load_equipment()` när man klickar ett objekts P&ID-markör) doldes taggen på VARJE rad, inklusive den första — taggen försvann helt istället för att bara dedupliceras.

**Fix:** regeln ersatt med en sekvensbaserad dedup, oberoende av kolumnsynlighet — döljs bara på en rad vars objekt (comp_type+comp_tag) är EXAKT samma som föregående orsak-rad i listan. Beräknas EN gång per orsak (inte per fysisk säkerhetsfunktions-rad) i `_build_rows()`s huvudloop genom att jämföra `_cause_tag_display(cause_d)` mot föregående orsaks värde, sparas som ny `UserRole+8`-flagga på ORS-posten (`repeats_previous_tag`), läses av `_PidDelegate.paint()` istället för `utr_visible`. En sentinel "avvikelse utan orsak"-platshållarrad bryter INTE kedjan (har ingen egen tagg att jämföra, lämnas orörd).

**Två tysta radhöjdsbuggar hittade under felsökningen** (samma rotorsaksklass, ingen av dem kopplad till dagens tagg-rapport i sig — upptäcktes av misstag när jag verifierade `_row_meta`/`table.item()`-antaganden för att bygga dedup-logiken ovan):
1. **Tidigare samma dags "krympa safeguard-höjd"-fix var en tyst no-op.** Den avgjorde "har den här raden eget innehåll i en annan kolumn" via `table.item(row, c) is not None` — men `_add_row()` skapar ALLTID ett NYTT item/widget per fysisk rad oavsett spann (`setSpan`/`setCellWidget` styr bara hur Qt MÅLAR täckta celler, tar inte bort deras innehåll), så det villkoret är alltid Sant. Varje säkerhetsfunktions-rad behöll därför sin fulla höjd trots att koden såg ut att krympa dem.
2. **`_resize_rows()`s egen ORS-minimihöjdsgolv (och en likadan kopia inuti `_compute_row_height()` självt) hade SAMMA bugg** — kollade `ors_item and ors_item.text()` rakt av, vilket är Sant på VARJE fysisk rad i en orsaks spann (samma item-dubblett-orsak), inte bara ankarraden.
3. Fixat genom att jämföra `_row_meta`/`_row_cat_info` mot FÖREGÅENDE rad istället (samma mönster som taggens dedup-fix ovan) — en rad är bara en "ren säkerhetsfunktions-fortsättning" om cons_id OCH kategori-info är oförändrade jämfört med föregående rad. `_sg_row_height()` justerad `+2` → `+4` eftersom `verticalHeader().setMinimumSectionSize(fm.height()+4)` ändå satte ett hårt Qt-golv där — `+2` gav ett värde funktionen påstod men Qt aldrig faktiskt använde.

**Test:** `OrsStripReworkTests`s gamla `test_tag_hidden_when_utrustning_column_visible_shown_when_hidden` ersatt med två tester som täcker den nya regeln (`test_tag_shown_regardless_of_utrustning_visibility_when_it_is_the_only_occurrence`, `test_tag_hidden_only_on_a_consecutive_repeat_of_the_same_object`). Ny `SafeguardRowHeightCompactionTests` (3 tester) för radhöjdsbuggarna — verifierar att rad 2/3 av tre säkerhetsfunktioner faktiskt KRYMPER (den ursprungliga no-op-buggen hade ingen sådan test alls). 805 tester gröna totalt.

## Slå ihop nodmarkup i nodinställningar — en högerpanel istället för två (2026-08-19)

**Rapport:** Anton: "När jag klickar på en nod idag i trädet får jag upp två menyer till höger. dels En meny där jag kan editera nodnamn, nodinställningar etc samt en för nod markup. Jag vill att den för nodmarkup integreras i den med nodinställningar så det bara blir en. Det kommer göra designen mycket snyggare. Notera att det är den med nodinställningars design som ska vara kvar och de andra knapparna ska flyttas dit." Kördes helt autonomt över natten (uttrycklig tillåtelse: "du kan köra detta på auto utan frågor för jag ska sova").

**Genomförande:** `NodeMarkupPanel` (node_markup.py, en egen 58px-bred ribbon i `_h_splitter`, index 3) togs bort helt och dess tillstånd/knappar flyttades rakt in i `PropertiesRibbon` (samma fil, 62px, index 2 — den ribbon Anton pekade ut som "designen som ska vara kvar"). `PropertiesRibbon` fick nya signaler (`tool_changed`/`all_vis_toggled`/`style_changed`/`snap_changed`/`navigate_node_requested`/`bottom_panel_toggled`/`place_symbol_requested`, samma namn/payload som NodeMarkupPanel hade) och en ny `_build_markup_tools()`-metod som bygger rit-verktygen (välj/polygon/polylinje/smart/text/kommentar), "Lägg ut P&ID-symbol", färgruta, synlighets-toggle och bottenfälts-växlingsknapp — allt bara synligt när `self._markup_active` är sant.

**Design-beslut: engångs-stäng-knappen ("✕") blev en kryssbar växlingsknapp ("✏️").** Verifierat FÖRE någon kodändring (`PIDPanel.enter_markup_edit`/`exit_markup_mode`, pid_panel_mod.py): markup-redigeringsläge tar över P&ID-canvasens musinteraktion HELT — vanlig markörnavigering och högerdrag-gummibandet för att placera nya objekt slutar fungera medan läget är aktivt. Den gamla stäng-knappen var alltså inte kosmetisk — enda sättet att tillfälligt återfå normal P&ID-klickbarhet utan att lämna noden i trädet. En kryssbar knapp bevarar exakt den funktionen men låter man dessutom slå på det igen för SAMMA nod utan att välja bort och välja tillbaka den (den gamla stäng-knappen hade ingen sådan väg tillbaka).

**Standardläge oförändrat:** markup-läge går fortfarande på automatiskt så fort en nod väljs (2026-08-18-beteendet rörs inte) — växlingsknappen ger bara en reversibel väg att stänga av det tillfälligt.

**Bonusfix hittad under verifieringen, samma kodväg:** föregående/nästa-nod-knapparna (⬆/⬇) anropade `_on_edit_node_markup` direkt, förbi `_on_selected` — det betydde att TRÄDETS egen markering och `PropertiesRibbon`s visade fält (namn/beskrivning-knapparna) aldrig synkades vid navigering, en tyst gap som var osynlig när de två panelerna var separata widgets men hade sett aktivt trasigt ut nu när de delar samma ribbon. Ett första försök att fixa detta genom att alltid anropa `tree_panel.refresh(...)` inuti `_on_edit_node_markup` orsakade en RIKTIG regression (`RuntimeError: wrapped C/C++ object ... has been deleted`) — den metoden kan köras REENTRANT inifrån ett pågående `tree_panel.refresh()`-anrop (t.ex. nod-omdöpning), och ett nästlat refresh-anrop rev då bort samma QTreeWidgetItem det yttre anropet fortfarande höll en referens till. Hittades av den redan existerande `HAZOPPreparationBladNoderTests.test_renaming_node_from_tree_syncs_to_noder_tab`. Fixat genom att flytta trädsynken till en ny, dedikerad `_on_markup_navigate_node_requested`-metod som BARA nås via en vanlig knappklick (aldrig nästlad inuti ett pågående refresh-anrop) — `_on_edit_node_markup` självt rör aldrig `tree_panel` längre.

**Ytterligare bugg hittad och fixad samma dag:** `PIDPanel._enter_markup_mode` (pid_panel_mod.py) kopplade `markup_draw_finished`/`markup_item_clicked` utan `Qt.ConnectionType.UniqueConnection`-skydd — att gå in i markup-läge igen (nu vanligare med en riktig av/på-knapp istället för bara en gång per nod) staplade dubbla kopplingar, så en ritning skulle ha triggat handlern flera gånger. Fixat med `UniqueConnection` + `try/except TypeError` (PyQt6 kastar `TypeError` vid ett dubbel-anslutningsförsök med den flaggan, till skillnad från C++ Qt som bara returnerar `False` — verifierat empiriskt innan fixen skrevs).

**Test:** `NodeMarkupPanelNavigateTests`/`NodeMarkupDockingTests`/`NodeMarkupAutoOpenTests`/`RedMarkupConsolidationTests` uppdaterade mot `PropertiesRibbon` istället för den borttagna `NodeMarkupPanel`-klassen (`.isHidden()` → `._markup_active`, `.node_id` → egenskap som proxar `._id`). `test_smoke.py`s knapp-klick-test likaså. Full `test_regression`-svit (805 tester) grön efter båda buggfixarna ovan.

## Översta safeguarden fortfarande oproportionerligt hög — dela delade krav jämnt över radspannet (2026-08-19, samma dag, uppföljning)

**Rapport:** Anton: "Översta safeguarden blir 3 rader lång. Den är nog kopplad till FA, ant+övriga medan nedanstående safeguards bara blir en rad. Försök fixa detta så även första safeguarden blir rätt lika låg som numer 2 och 3 osv."

**Root cause:** samma dags tidigare "krymp safeguard-höjd"-fix (se ovan) stoppade visserligen dubbletträkning på fortsättningsrader, men mätte fortfarande varje DELAT krav (LOPA-widgetens fasta höjd `_ROW_H*3+2=50px`, ORS-textens radbrytningshöjd, ORS:s 2-radersgolv `fm.height()*2+20`) i sin HELHET på bara ankarraden. En spannad cells målningsyta är UNIONEN av hela radgruppen — ett delat krav behöver bara få plats NÅGONSTANS i den unionen, inte i en enda fysisk rad. Verifierat: `_LopaWidget.sizeHint()=48`, `min_ors=52` — jämförbara storleksordningar, båda föll tidigare i sin helhet på ankarraden medan fortsättningsraderna (redan korrekt krympta) stod kvar vid ~20px, vilket gav en ~2.5× höjdskillnad som visuellt ser ut som "3 rader mot 1 rad".

**Fix:** `_compute_row_height()` delar nu varje delat krav med hur många fysiska rader dess eget spann täcker (`_span_group_size()`, samma gruppering som `_apply_spans`s egen `_span_col` redan vandrar igenom) och applicerar den resulterande ANDELEN på VARJE rad i gruppen — inte bara ankarraden. Fungerar utan att särskilja ankare eftersom varje fysisk rad redan bär sin egen dubblett-post/widget (samma insikt som förra fixen byggde på). Matematiskt garanterat att aldrig underförse utrymme: `sum(ceil(h/n) for _ in range(n)) >= h` alltid. `_resize_rows()`s separata, nu redundanta (och direkt motverkande) ORS-golv-ombäddningspassage togs bort helt — den skulle annars ha återställt ankarraden till det odelade golvet och gjort själva distributionen om intet.

**Verifierat manuellt innan test skrevs:** kort orsakstext → tre safeguard-rader blev 22/20/20px (tidigare 52/20/20). Lång, riktigt radbrytande text (inga hopslagna ord som skulle lura `TextWordWrap` att inte bryta alls — en fälla jag själv gick i under verifieringen) → total spannad höjd över tre safeguard-rader (405px) matchade EXAKT vad en ensam safeguard med samma text hade fått (405px), bara nu jämnt fördelat (135px vardera) istället för allt på rad ett.

**Test:** Två nya tester i `SafeguardRowHeightCompactionTests`: `test_first_safeguard_row_is_not_disproportionately_tall` (kort text, ankarraden får inte vara dramatiskt högre än sina syskon) och `test_long_description_total_height_preserved_across_safeguard_group` (lång text, gruppens TOTALA höjd måste fortfarande räcka till lika mycket som en ensam safeguard med samma text — skyddar mot att distributionen av misstag underförser utrymme och återinför 2026-08-11-buggen "text göms på raderna"). 807 tester gröna totalt.

## Objekt-väljare för safeguards: 🏷-ikon öppnar rullista med P&ID-objekt + typfilter (2026-08-19)

**Rapport:** Anton: "Kan du implementera så att när jag väljer safeguards i hazop scenario får jag upp en rullista med objekt (dvs de som definerats på P&ID) jag måste också kunna välja fritt själv. Du kan även inkludera en inställningsknapp i rulllistan där jag kan klicka på och välja vilka typer av objekt. Exempelvis bara instrument." Uppföljning på var kontrollen skulle sitta: "Jag vill ha en liten ikon i cellen som jag kan klicka på som gör att pop-upen med definerade objekt dyker upp där jag kan välja vilket objekt."

**Läge innan:** ingen klickbar väg att koppla ett P&ID-objekt till en safeguard fanns — bara dra-och-släpp av en P&ID-markör (bygger en löpande mening i fritextbeskrivningen, `Database.append_tag_to_safeguard`) eller Shift-klick medan cellens texteditor råkade vara öppen.

**Genomförande:**
1. Ny liten 🏷-ikon längst till vänster i safeguard-cellen (`_SG_TAG_ICON_ZONE_W=18px`, scenario_panel.py) — kostar bara BREDD, inte höjd, så den påverkar inte denna sessions hårt krympta safeguard-radhöjder. Ikonen tonas mörkare när ett objekt redan är kopplat. Klick-zonen är spegelvänd mot den befintliga RRF-badge-zonen till höger (samma `eventFilter()`-mönster).
2. Ny `SafeguardObjectPopup` — redigerbar `QComboBox` (välj ur listan ELLER skriv fritt, med `QCompleter` för filtrering medan man skriver) + en kugghjulsknapp. Live-committar direkt (samma "inga OK/Avbryt"-konvention som `CauseTagPopup`/`EquipmentPlacementPopup`). Anropar avsiktligt `db.set_safeguard_tag()` (bara taggen) — INTE `append_tag_to_safeguard()` (som bygger en löpande mening i fritexten) — en dedikerad "byt objekt"-kontroll ska inte lämna gamla taggfragment kvar i beskrivningen om man väljer om sig.
3. Ny `_SgObjectTypeFilterDialog` — en riktig MODAL dialog (inte ännu en live-commit `Qt.WindowType.Popup`) för kryssrutelistan av objekttyper: att stapla två frameless auto-dismiss-popuper är skört i Qt (den andra som tar musgrabben stänger typiskt den första direkt). Sparas projekt-brett via `db.set_config('sg_object_type_filter', json.dumps(...))`. Inga typer ikryssade = visa alla (standard).
4. Nya `ui_helpers._equipment_tags_for_types(db, types)` och `_resolve_comp_type_for_tag(db, tag)` — typfiltrerad tagglista respektive exakt katalogslagning för att auto-sätta `comp_type` när en tagg väljs/skrivs.

**Test:** Ny `SafeguardObjectPickerTests` (11 tester) — klick-zonerna är ömsesidigt uteslutande (ikon vs RRF), känd tagg slår upp rätt typ, fritext utan träff ger tom typ, "— Inget objekt —" nollställer, beskrivningstexten rörs aldrig, typfiltret begränsar/vidgar listan korrekt och persisteras, en redan satt tagg visas även om dess typ senare filtrerats bort, cellen kraschar inte i ett helt tomt projekt utan `equipment_catalog`-rader. 818 tester gröna totalt.

## Objekt inte längre fetstilta i HAZOP-trädet (2026-08-20)

Anton: "Objekt behöver inte vara fetstilta i hazopträdet. justera till normaltext." `TreePanel`s utrustnings-/objekt-banner-rader (grupperade under varje ledord, `tree_panel.py`s `for eq_id, eq_devs in equipment_groups.items():`-loop) körde `eq_font.setBold(True)` — borttaget. Den kursiva "ej definierad"-markeringen (`eq_font.setItalic(undefined)`) är oförändrad. Enda träffen i hela filen — nod-radernas egen fetstil (NODE_T, ett helt annat koncept) rörd inte. 818 tester gröna.

## Dela upp test_regression.py i per-modul testfiler (2026-08-20)

**Bakgrund:** Anton bad om förslag på hur programmet kunde göras lättare för mig att arbeta i utan att funktionerna blir lidande. Två punkter valdes ut att göra först (av fem + en större, medvetet uppskjuten idé — se svaret i sessionen för hela listan): (1) dokumentera "duplicerat item per fysisk rad i spannade celler"-fällan i CLAUDE.md istället för bara NOTES.md (klart, se `CLAUDE.md`s nya "Known traps"-avsnitt), och (2) den här: dela `test_regression.py` (18 611 rader, 136 `TestCase`-klasser) i mindre per-modul-filer, eftersom hela filen annars måste läsas/grep:as i sin helhet även för en enda riktad testklass.

**Genomförande:** en engångs Python-migreringsskript (körd via Bash, ingen manuell omskrivning av testkroppar — exakt radbaserad klyvning på riktiga `^class`-gränser i den verkliga filen, för att undvika avskrivningsfel över 18 000+ rader) delade upp filen i:
- `test_helpers.py` — delad infrastruktur (`_ensure_qapp`, `_menu_action_labels`, `_fake_pdf_loaded`, `_TempDbMainWindow`, `_find_tree_item`).
- 13 per-modul-filer (`test_database.py`, `test_scenario_panel.py`, `test_pid_viewer.py`, `test_pid_panel_mod.py`, `test_pid_graphics_view.py`, `test_tree_panel.py`, `test_equipment_panel.py`, `test_equipment_detection.py`, `test_settings_panels.py`, `test_worksheet.py`, `test_node_markup.py`, `test_hazop.py`, `test_ui_helpers.py`).
- `test_integration.py` — 37 klasser (~5 360 rader) som medvetet spänner över flera moduler (MainWindow-drivna cross-panel-tester, t.ex. `_TempDbMainWindow`-baserade synk-tester) och inte tvingades in i en enda modulfil.

**Två fällor hittade under verifieringen (fixade innan commit):**
1. Sektionsbanner-kommentarer (`# ═══...`) som står direkt ovanför en klass för att förklara VARFÖR den finns hamnade fel — en naiv "klass N:s block slutar där klass N+1 börjar"-gräns lämnar en sådan banner kvar sist i klass N:s FIL istället för att följa med klass N+1 in i dess nya fil, om de två klasserna hamnade i olika bucket-filer (14 av 17 sådana fall i denna fil). Fixat genom att låta varje klass "äga" sin egen direkt föregående kommentarblock (gå bakåt förbi tomrader + hela det sammanhängande kommentarblocket) istället för att anta att gränsen alltid ligger exakt vid `class`-raden.
2. `_find_tree_item()` (en fristående funktion, inte en klass) låg mitt i filen mellan två klasser som hamnade i OLIKA bucket-filer — extraherad separat till `test_helpers.py` istället för att av misstag hamna kvar i endera klassens nya fil.

**Verifiering:** 818 `def test_`-metoder i originalfilen, 818 i de nya filerna sammanlagt (exakt matchning). Varje klass' "fingeravtryck" (klassrad + sista icke-tomma kroppsrad) återfanns ordagrant i exakt en ny fil. Full körning av alla 14 nya filer + `test_smoke` gav samma 818 gröna tester som originalfilen gjorde precis innan borttagningen — ingen regression, inget tyst borttappat test.

**Uppdaterat:** CLAUDE.md:s testavsnitt (nya körkommandon, ny "kör bara den modulens fil"-nivå mellan smoke och full svit) och arkitektur-avsnittets text om modul-uppdelningen; `test_smoke.py`s docstring.

## Centraliserad zon-geometri i scenario_panel.py + två helt döda menyer hittades (2026-08-20)

**Bakgrund:** Punkt 3 av samma förbättringslista som testfils-uppdelningen ovan — centralisera `_PidDelegate.paint()`s och `eventFilter()`s klick-zon-geometri hårdare, så de aldrig kan glida isär (mönster som redan fanns för tagg-zonen/frekvens-zonen, se tidigare NOTES.md-poster).

**Genomförande:** fem nya delade geometri-metoder på `ScenarioTablePanel` (`_ors_comment_dot_geometry`, `_sg_icon_zone_geometry`, `_sg_rrf_zone_geometry`, `_kon_cat_zone_geometry`, `_plus_badge_geometry`), anropade från BÅDE `paint()` och `eventFilter()` istället för att varje sida räknade ut samma rektangel separat.

**Två riktiga, redan levande buggar hittades under arbetet (inte hypotetiska — bekräftat att de var helt oåtkomliga via UI:t):**

1. **ORS-cellens kommentar-klick var matematiskt omöjlig att träffa.** `eventFilter()`s gamla zon skrevs `pos.x() >= cmt_right and pos.x() < cr.right() - 18` där `cmt_right` RÅKADE vara definierad som exakt `cr.right() - 18` — dvs `x >= N and x < N`, alltid falskt. `_open_comment_popup()` hade alltså ingen fungerande ingång alls. Fixat: zonen använder nu `_ors_comment_dot_geometry()` (samma rektangel `paint()` faktiskt ritar prick i), lite marginal tillagd för lättare klick, och zonen är bara aktiv när `_has_comment` är sant (matchar att pricken bara ritas då).
2. **En "klona scenario"-zon bredvid pricken träffade tomt utrymme, inte något synligt.** Klick där gjorde `_clone_scenario()` utan någon visuell indikation om att det skulle hända. Tog bort zonen helt — klona-funktionen behöver ingen inline-zon eftersom den redan finns i högerklicksmenyn.
3. **Själva högerklicksmenyn med "Duplicera scenario…", "Kommentar…", "Redigera konsekvenskedja (Del1–Del5)…" och "Ändra RRF..." var HELT DÖD KOD.** `ScenarioTablePanel._on_table_context_menu` byggde en riktig meny med dessa fyra åtgärder, men var aldrig kopplad till någon signal — `self._table.customContextMenuRequested.connect(...)` pekar bara på den ANDRA, redan existerande `_on_context_menu`-metoden (Redigera/Duplicera/Flytta/Ta bort för orsak/konsekvens/barriär). Ingen av de fyra åtgärderna i den döda metoden gick alltså att nå från UI:t överhuvudtaget, oavsett hur man högerklickade. Fixat genom att slå ihop alla fyra åtgärderna in i den riktiga, kopplade `_on_context_menu` (rätt kolumn-gren: orsak/konsekvens/barriär) och ta bort `_on_table_context_menu` + dess hjälpmetod `_cell_has_item` helt — inget förlorades, eftersom RRF redan gick att nå en annan väg (badge-klick i safeguard-cellen), men konsekvenskedje-redigeraren, scenario-duplicering och (fram till detta commit) kommentar-redigering hade ingen fungerande väg alls innan.

**Test:** fyra nya tester i `OrsCommentClickZoneTests` (test_scenario_panel.py) — klick på pricken öppnar popupen, ingen klick-zon när ingen kommentar finns, klick nära pricken (gamla klona-zonens plats) gör inget, och kontextmenyn erbjuder både "Kommentar…" och "Duplicera scenario till annan avvikelse…" (verifierat via samma mock-QMenu-mönster som `TagDetachContextMenuTests`) — plus en regressionsvakt som säkerställer att `_on_table_context_menu` inte tyst återuppstår. 823 tester gröna totalt (103 → 108 i test_scenario_panel.py, 818 → 823 totalt).

**Lärdom för framtida sessioner:** en åtgärd som SER ut att vara kopplad (finns i menykod, har en `.triggered.connect(...)`) kan ändå vara helt oåtkomlig om själva menybyggande METODEN aldrig kopplas till en signal. "Grep:a efter anropsstället" räcker inte för att verifiera att en UI-funktion faktiskt går att nå — man måste följa kedjan hela vägen till `connect()`.

## NOTES.md trimmad, äldre loggar flyttade till NOTES_ARCHIVE.md (2026-08-20)

**Bakgrund:** Punkt 4 av samma förbättringslista (se ovan) — NOTES.md hade vuxit till 2513 rader och läses i sin HELHET varje session per CLAUDE.md:s egen "Session context"-instruktion, vilket kostar tokens/tid varje gång oavsett hur relevant den äldsta historiken är för dagens uppgift.

**Genomförande:** de kronologiska sessionsposterna från 2026-07-29 till 2026-08-16 (1972 rader, ~50 poster — prestandaoptimeringar, uppstartsarbete, Utrustningsregistrets omskrivning, ventil-/pump-/instrumentdetektering, P&ID-navigeringsprestanda, symbolmatchning m.m.) flyttades ORÄNDRADE till en ny fil `NOTES_ARCHIVE.md`, med en kort förklarande header överst. De evergreen-sektionerna ("Arkitekturella beslut", "Funktioner implementerade"-tabellen, "Uppskjutna funktioner", "Kända begränsningar och tekniska skulder", "Användarpreferenser", underhållsinstruktionerna) och alla sessionsposter från 2026-08-17 och framåt ligger kvar i NOTES.md, oförändrade. En kort pointer-paragraf ersätter de borttagna raderna, med radantal och en kategorisk sammanfattning av vad som flyttades. Resultat: NOTES.md 2513 → ~550 rader (≈78 % minskning).

**Varför gränsen ligger vid 2026-08-17, inte t.ex. en vecka bakåt eller en fast radgräns:** filen är redan strikt kronologisk och organiserad i dagvisa sessionsblock; 2026-08-17 var den naturliga starten på den täta, fortfarande direkt relevanta arbetsperioden (modul-uppdelningen av hazop.py/pid_viewer.py, objektidentitets-sammanslagningen, safeguard-objektväljaren) som denna och kommande sessioner sannolikt fortfarande bygger vidare på — äldre poster (ventildetektering fas 1, tidig P&ID-prestandaoptimering) är stabil, avslutad historik som sällan behöver slås upp i.

**Uppdaterat:** ingen kodändring — bara NOTES.md/NOTES_ARCHIVE.md. CLAUDE.md:s "Session context"-avsnitt bör nämnas läsas tillsammans med denna post om framtida sessioner behöver historisk kontext.

## Städat arbetskatalogen: .gitignore utökad, en-off-skript/rapporter flyttade till egna mappar (2026-08-20)

**Bakgrund:** Punkt 5 av samma förbättringslista — `git status` visade konstant 50+ ospårade filer i projektroten (loggar, krasch-rapporter, skärmdumpar, en 93 MB referens-PDF-mapp, en 2026-08-02-revisionsgranskning av konsekvenskedjor, fyra engångs-analysskript, tre inaktuella planerings-md:er), vilket gjorde det svårare att se på en blick vad som faktiskt var relevant att arbeta med i katalogen.

**Genomförande:**
1. **`.gitignore` utökad** med `*.log`, `*.hzp`, `crashes/`, skärmdumpsmönster (`Screenshot *.png`, `connector_demo.png`) samt de stora lokala referensmapparna `P&ID ref/` (93 MB — se NOTES_ARCHIVE.md 2026-08-05: "riktiga P&ID ref/-filer används bara för lokal manuell körning, aldrig som testfixturer"), `HAZOP ref/`, `Old screenshot/`, `icon_requests/`, `2026-08-12 Design/`.
2. **Fyra engångs-analysskript** (`extract_tags.py`, `extract_tags2.py`, `generate_html_report.py`, `replace_causes.py` — RDS-PP-taggmönsteranalys resp. standardorsaks-migrering, inga anropsställen i produktionskoden) flyttade till ny mapp `dev_scripts/`.
3. **2026-08-02-konsekvenskedjegranskningen** (11 filer — `AUDIT_*.txt`, `HAZOP_AUDIT*.txt`, `HAZOP_CONSEQUENCE_AUDIT*.txt`, `README_AUDIT.txt`, `audit_report.{html,json}`, `consequence_chains_audit.json`) flyttade till ny mapp `audit_2026-08-02/` — genererade rapporter från en databasgranskning, inte källkod, men behöll dem (inte gitignorade) eftersom de är en verklig historisk revisionsrekord.
4. **Tre inaktuella planeringsdokument** (`MUTABLE_GLOBAL_STATE_PLAN.md`, `SEKUNDAR_VERKAN_EXTRACTED.md` — "Sekundär verkan"-funktionen redan implementerad, se "Funktioner implementerade"-tabellen ovan, `hazop_style_patch.md` — stilen redan applicerad, se tidigare NOTES_ARCHIVE.md-poster om blå accentfärg) flyttade till ny mapp `docs_archive/`.
5. **Tre tomma/värdelösa skräpfiler raderade** (grepp:at igenom koden först för att bekräfta inga anropsställen): `orsaker.txt` (414 bytes, en kategorilista redan seedad i DB:n — bara nämnd i en docstring-kommentar i `settings_panels.py`, inte inläst vid körning), `utr_out2.txt` (gammal `test_regression`-testkörningsutskrift — den filen finns inte längre, se 2026-08-20-uppdelningen ovan), en 0-bytes fil med ett trasigt filnamn (`C:Temphazop_out.txt`, egentligen ett Unicode-privatbruksomrode-tecken — rest av en missad omdirigering).

**Resultat:** `git status` i hazop/ visar nu bara faktiskt relevanta, avsiktliga ändringar istället för 50+ konstanta ospårade filer varje gång.

## Uppföljning: allt som inte hör till programmet flyttat till ej_programfiler/ (2026-08-20)

**Rapport:** Anton, uppföljning på städningen ovan: "Skulle du kunna städa upp lite i hazop programmet mappar och lägga allt som inte hör till programmet i en separat mapp, dvs print screens etc." — förra rundan bara gitignorade skräpet, flyttade inte det fysiskt ur mapp-strukturen.

**Verifierat INNAN något flyttades (för att inte råka förstöra det körande programmets tillstånd):** `database.py`s `DB_PATH = Path(__file__).parent / "hazop_project.db"` är default-databasen appen öppnar — och dess `pid_revisions`-tabell pekar faktiskt på `hazop_project_pid.pdf` i samma mapp (verifierat med en riktig SQL-fråga mot den levande databasen, inte antaget). Dessa två filer rördes INTE — att flytta dem hade gjort att appen antingen tappat bort P&ID-filen eller (om DB-filen flyttats) tyst skapat en NY, tom databas vid nästa start. `crashes/` och `hazop_backups/` är likaså hårdkodade sökvägar i `hazop.py`/`database.py` (`CRASH_DIR`, backup-katalogen) — riktig programinfrastruktur, inte skräp, lämnade orörda.

**Vad som verifierades vara säkert att flytta (grep + en SQL-dump av hela den levande databasen för att bekräfta att inget refererar dem):** `hazop_project_pid2/3/4.pdf`, `nytt_projekt(2).hzp`, `hazop_project_backup_2026-06-15.db`, `hazop_rapport.xlsx`, `HAZOP_stoddatabas_v2_unika_orsaker.xlsx` — noll träffar i databasdumpen, `nytt_projekt`-strängen i `hazop.py` är bara ett default-filnamnsförslag i en Spara-som-dialog (läses aldrig tillbaka), `hazop_rapport.xlsx` samma sak för Excel-export. `hazop_crash.log` (18,7 MB) är skriv-bara "legacy"-loggning (kommentar i koden säger uttryckligen "for backward compatibility") — läses aldrig tillbaka, säker att flytta.

**Genomförande:** ny mapp `ej_programfiler/` med fyra undermappar:
- `screenshots/` — 17 skärmdumpar (`Screenshot *.png`, `connector_demo.png`).
- `logs/` — `hazop_crash.log`, `hazop_debug.log`, `hazop_launch.log`, `hazop_output.log`, `test_run.log`.
- `reference_material/` — `P&ID ref/` (93 MB), `HAZOP ref/`, `Old screenshot/`, `icon_requests/`, `2026-08-12 Design/`, `Red markup/` (samma mappar `.gitignore` redan uteslöt förra rundan, nu faktiskt flyttade ut ur rotkatalogen också).
- `old_project_files/` — de sex verifierat-orefererade filerna ovan.

Två ytterligare engångsskript (`analyze_refs.py`, `render_layout.py` — kopplingsstatistik/PNG-förhandsvisning mot `P&ID ref/`-biblioteken, se NOTES_ARCHIVE.md) hade missats i förra rundans `dev_scripts/`-flytt — flyttade dit nu (`git mv`-mönster, registrerades som ren rename i git).

**`.gitignore` förenklad:** ersatte fem separata katalogmönster med en enda `ej_programfiler/`-rad, eftersom allt som behövde gitignoras nu bor under den ena mappen.

**Verifiering:** `Database(path=DB_PATH)` konstruerad direkt mot den orörda `hazop_project.db` efter flytten — ingen krasch, samma `pid_revisions`-rad som innan. Full `test_smoke`-svit grön.

## Dela upp settings_panels.py (2026-08-21)

**Bakgrund:** Anton bad om en genomgång av om koden kunde struktureras/delas upp bättre. Sju parallella agenter läste igenom varsin klunga av de stora produktionsfilerna (efter att jag själv fastställt att "max 500 rader" inte är en regel jag känner igen, men att sökbarhet/sammanhållning är den verkliga poängen). `settings_panels.py` (3374 rader) var det tydligaste fallet: en samling av 12 i praktiken oberoende klasser, grupperade bara för att de alla är "en inställningsflik" — inte för att de delar logik. Anton: "Genomför 1 och 2" (punkt 1 = denna uppdelning, punkt 2 = konsolidera tagg-matchningen i equipment_detection.py, se separat post nedan).

**Verifierat SJÄLV innan något flyttades** (agentens kartläggning användes som utgångspunkt, inte facit): grep:ade varje klass för korsreferenser till sina syskon. Bekräftat att `DraggableColorSwatch`/`MatrixCellButton` bara instansieras inuti `HAZOPPreparationPanel`, att `SeverityDefinitionsPanel` genuint aldrig instansieras någonstans i hela kodbasen (dödkod — låg kvar orörd, togs INTE bort eftersom det inte var vad som efterfrågades), att `SettingsPanel` instansierar både `StandardObjectsSettingsPanel` och `TagMemoryPanel`, och att `HAZOPPreparationPanel` instansierar både `ParticipantMatrixPanel` och `StandardCausesSettingsPanel` — dessa korsberoenden styrde vilka nya filer som behöver importera från vilka.

**Genomförande:** mekanisk radbaserad klyvning via ett engångsskript (samma metod som testfils-uppdelningen 2026-08-20 — riktiga `^class`-radnummer i den faktiska filen, inget manuellt omskrivet). Fem nya filer:
- `standard_causes_panel.py` — `StandardCausesSettingsPanel`
- `standard_objects_panel.py` — `StandardObjectsSettingsPanel`
- `tag_memory_panel.py` — `TagMemoryPanel`
- `participant_matrix_panel.py` — `ParticipantMatrixPanel` + dess privata `_InlineHeaderEdit`
- `hazop_preparation_panel.py` — `HAZOPPreparationPanel` + dess privata `DraggableColorSwatch`/`MatrixCellButton`, importerar `ParticipantMatrixPanel` och `StandardCausesSettingsPanel` från de nya filerna ovan

`settings_panels.py` krymper till paraplyfil (3374 → 565 rader): `SeverityDefinitionsPanel` (dödkoden, kvar oförändrad), `SettingsPanel`, `PIDManagementPanel`, `StudyManagementPanel` (+`AdminPanel`-aliaset) ligger kvar direkt, plus re-export-imports av de fem utflyttade klasserna — samma lager+re-export-mönster som redan används överallt annars i kodbasen (se CLAUDE.md). `hazop.py`s `from settings_panels import (...)`-block behövde **noll ändringar** — verifierat direkt: `python -c "import hazop; print(hazop.HAZOPPreparationPanel, ...)"` löser fortfarande upp alla sju namnen, bara från sina nya moduler.

**Verifiering:** alla 12 klasser återfanns exakt en gång vardera i rätt fil (grep-kontroll). `py_compile` på alla 6 filer. `test_smoke` (11 tester), `test_settings_panels.py` (79 tester), `test_integration.py` (208 tester) och den fulla 14-filssviten (823 tester) — alla gröna, ingen regression.

**Uppdaterat:** CLAUDE.md:s arkitekturlista (nytt 13a–13e under punkt 13) och `py_compile`-kommandot i "Köra programmet"-avsnittet.

## Konsolidera tagg-matchning i equipment_detection.py (2026-08-21)

**Bakgrund:** Punkt 2 av samma "Genomför 1 och 2"-begäran som filuppdelningen ovan. Samma strukturgenomgång flaggade att `equipment_detection.py` hade TRE separata, handsynkade implementationer av "är den här texten en giltig P&ID-tagg" (`_parse_tag`, `_pick_best_tag`, `_score_tag_word`) — kommentarer i koden erkände redan att de måste hållas i synk manuellt ("same as _parse_tag's own EXT_TAG_RE branch", "see _parse_tag's identical guard"), exakt det mönster som redan orsakat en riktig, dokumenterad dubbel-tagg-bugg (LKAB, 2026-08-13, se NOTES_ARCHIVE.md "Dubbla taggar vid skanning").

**Verifierade avvikelserna konkret innan jag rörde något** (körde alla tre funktionerna mot samma testfall, inte bara läste koden):
1. **`_pick_best_tag` saknade validering helt** i sin `_EXT_TAG_RE`-gren — vilket regex-mönster som helst med rätt FORM (area-segment + instrumentkod) accepterades direkt, även om `_equip_prefix_from_tag` inte hittade något verkligt prefix. Konkret bevisat: `_pick_best_tag("DN50-PN16")` (två rör-specifikationskoder — DN=nominell diameter, PN=nominellt tryck — som `_equip_prefix_from_tag`s egen `skip`-lista finns specifikt för att avvisa) returnerade `"DN50-PN16"` **ordagrant** som om det vore en giltig tagg.
2. **`_score_tag_word` normaliserade aldrig sin träff** — returnerade rå, onormaliserad text (`"E1.M1.GPA4"`) medan `_parse_tag`/`_pick_best_tag` båda normaliserar till `"E1.M1-GPA4"`. Exakt samma "två olika strängar för samma instrument"-bugg som `_normalize_ext_tag()` skrevs för att förhindra 2026-08-13 — återinförd via en tredje kodväg (`find_tag_near_point`) som den fixen aldrig rörde vid.
3. **`_score_tag_word` kräver dessutom `len(prefix) >= 2`** medan `_parse_tag`/`_pick_best_tag` accepterar enbokstavsprefix (t.ex. "20-E-101") — bedömdes vara ett MEDVETET, striktare konfidensval (inte en bugg) och bevarades oförändrat.

**Genomförande:** en ny delad funktion `_match_ext_tag(candidate, min_prefix_len=1)` — validerar OCH normaliserar en `_EXT_TAG_RE`-träff i ett enda ställe. Alla tre funktioners `_EXT_TAG_RE`-grenar anropar nu denna: `_parse_tag`/`_pick_best_tag` med default `min_prefix_len=1` (oförändrat beteende för alla legitima taggar), `_score_tag_word` med `min_prefix_len=2` (bevarar dess egna, redan striktare konfidensval).

**Ett djupare, separat fynd — flaggat men INTE åtgärdat** (utanför denna begärans omfång): `DN50-PN16` slinker fortfarande igenom via en ANNAN gren (`_FULL_TAG_RE`, som `_pick_best_tag` faller tillbaka på) och returnerar `"DN-50"` — fortfarande fel, bara via en annan kodväg. Roten är att varken `_TAG_RE`- eller `_FULL_TAG_RE`-grenarna i någon av de tre funktionerna någonsin konsulterar `skip`-listan (bara `_equip_prefix_from_tag`s EGEN interna kedja gör det) — ett bredare, redan existerande hål som inte är del av 3-vägs-avvikelsen jag ombads konsolidera. Värt en egen framtida titt.

**Verifiering:** manuell sweep av alla tre funktionerna mot 12 representativa fall (alla plattformskonventioner i modulens egna docstrings, plus de tre avvikelsefallen) före/efter ändringen — bara de tre avsedda skillnaderna ändrades, inget annat. 5 nya tester i `ExtTagConsolidationTests` (test_equipment_detection.py): låser fast båda fixarna samt de två medvetet oförändrade besluten (enbokstavsprefix fortfarande accepterat av parse/pick_best, fortfarande poängsatt 0 av score). 66 tester i `test_equipment_detection.py`, 828 totalt i hela sviten — alla gröna, ingen regression.

## Flytta alla test_*.py till en egen tests/-mapp (2026-08-21)

**Rapport:** Anton: "Kan du lägga alla .py som har med test i en egen testmapp?" — de 18 `test_*.py`-filerna låg blandade rakt in bland de 21 produktionsfilerna i hazop/-roten.

**Den enda verkliga risken, identifierad innan något flyttades:** varje testfil har en `_HAZOP_DIR = Path(__file__).resolve().parent`-rad högst upp vars enda syfte är att lägga hazop-rotens mapp på `sys.path` så att `from hazop import ...`/`from scenario_panel import ...` m.fl. fungerar oavsett varifrån testerna körs (kommentaren i koden säger det uttryckligen). Att flytta filerna en mapp-nivå ner UTAN att justera denna rad hade tyst bytt vad `_HAZOP_DIR` faktiskt pekar på (från hazop-roten till den nya tests/-mappen) — alla produktionsimporter hade slutat fungera, och `test_integration.py`s egen kontroll av att `hazop_project.db` är rätt fil (`_HAZOP_DIR / "hazop_project.db"`) hade tystare pekat på fel plats.

**Genomförande:**
1. Ny mapp `tests/` + tom `tests/__init__.py`.
2. `git mv` av alla 18 `test_*.py`-filer (inkl. `test_helpers.py`) till `tests/` — registrerades som rena renames i git.
3. Mekanisk regex-ersättning (engångsskript, körd och sedan borttagen) av `_HAZOP_DIR`-blocket i 17 av filerna: `_HAZOP_DIR = Path(__file__).resolve().parent` → `_TEST_DIR = Path(__file__).resolve().parent; _HAZOP_DIR = _TEST_DIR.parent`, och lägger nu BÅDA `_HAZOP_DIR` (hazop-roten, för produktionsimporter) OCH `_TEST_DIR` (tests/-mappen själv, för `from test_helpers import ...` mellan testfilerna) på `sys.path`. `test_smoke.py` hade aldrig detta block alls (gjorde bara sena, in-metod-importer och förlitade sig på att `-m unittest` körs direkt från hazop/-roten) — fick blocket tillagt manuellt.
4. Detta gör testsviten körbar på BÅDA sätt: `python -m unittest tests.test_smoke -v` (från hazop/-roten, punktad modulsökväg) ELLER `cd tests && python -m unittest test_smoke -v` (bart modulnamn) — verifierat att båda faktiskt fungerar, inte bara antaget.

**Verifiering:** `py_compile` på alla 18 filer. Full 14-filssvit (828 tester) grön via `python -m unittest tests.test_database tests.test_scenario_panel ...`. `test_symbol_geometry`/`test_image_symbol_matching` (140 tester) gröna. `test_smoke` verifierad grön i BÅDA körsätten (från hazop/-roten och från tests/-mappen).

**Uppdaterat:** CLAUDE.md:s hela "Testing during iterative development"-avsnitt (alla körkommandon fick `tests.`-prefix, ny inledande förklaring av de två stödda körsätten).

## Paketera HAZOP-appen som en installationsfil — del 1: frozen-build-sökvägar (2026-08-21)

**Rapport:** Anton: "Kan du skapa en fungerande exe-fil för att köra programmet? Ger det några fördelar om det är en installationsfil istället för ett program som kör direkt?" Efter en planeringsrunda (se plan-filen från sessionen): målgrupp = kollegor på ProSa (motiverar en riktig installer, inte bara en portabel exe), OCR = bara `rapidocr_onnxruntime` behöver fungera (redan obligatoriskt beroende och förstahandsval i koden, ingen extern binär — Tesseract/EasyOCR lämnas som de valfria reservalternativ de redan är).

**Kritiskt problem hittat under planeringen, löst FÖRST innan något paketeringsarbete:** fyra ställen i produktionskoden antog att appens egen data (databas, kraschrapporter, backuper, loggfil) OCH dess medföljande ikoner låg relativt till skriptets egen fil (`Path(__file__).parent`). Detta bryter helt när PyInstaller paketerar appen — `__file__` pekar då på en plats INUTI paketet, inte på var den installerade exe:n faktiskt ligger (och i onefile-läge är det en TILLFÄLLIG uppackningsmapp Windows kan radera mellan körningar). Utan fix hade appen antingen tappat bort en riktig databas/kraschlogg, eller (värre) tyst skapat en ny tom databas i en försvinnande mapp.

**Fix:** två nya hjälpfunktioner i `constants.py` (lägsta lagret, inga cirkulära importer):
- `_app_dir()` — för SKRIVBAR användardata (databas, krascher, backuper, logg): `Path(sys.executable).resolve().parent` när `sys.frozen` är satt (PyInstaller sätter detta), annars oförändrat `Path(__file__).resolve().parent`.
- `_bundle_dir()` — för SKRIVSKYDDADE medföljande resurser (ikoner): `Path(sys._MEIPASS)` när fryst (PyInstaller sätter alltid detta, både onefile och onedir), annars samma fallback.

Uppdaterade: `database.py`s `DB_PATH` → `_app_dir()` (`_backup_dir()` ärver detta automatiskt via `self.path.parent`, ingen separat ändring behövdes där), `hazop.py`s `CrashReporter.CRASH_DIR` och `_LOG` → `_app_dir()`, `pid_viewer.py`s `_ICONS_DIR` → `_bundle_dir()` (ny `from constants import _bundle_dir`, fanns ingen constants-import i den filen tidigare).

**Verifiering:** detta är en no-op när appen INTE är paketerad — bekräftat genom att direkt skriva ut `DB_PATH`/`_ICONS_DIR`/`CRASH_DIR` före/efter och se att de är identiska. Full 14-filssvit (828 tester) + `test_smoke` gröna, ingen regression. Ny fristående testfil `tests/test_constants.py` (5 tester, Qt-fri) mockar `sys.frozen`/`sys.executable`/`sys._MEIPASS` för att låsa fast BÅDA lägena (opaketerat OCH paketerat) — annars finns inget som någonsin skulle träna den fryst-grenen, eftersom ingen vanlig testkörning faktiskt är fryst.

**Nästa steg (samma åtagande, kommande commits):** `.hzp`-kommandoradsargumentet (finns redan som obrukad parameter i `MainWindow.__init__`), själva PyInstaller-bygget, samt Inno Setup-installern. Se plan-filen för fullständig sekvens.

## Paketera HAZOP-appen som en installationsfil — del 2: .hzp-filkoppling + en riktig databasförlust-bugg hittad på vägen (2026-08-21)

**Genomförande av den avsedda funktionen:** `MainWindow.__init__(self, hzp_path: str = None)` tog redan emot en sökväg men gjorde ALDRIG något med den — `self._hzp_path` sattes ovillkorligt till `None` några rader senare, så en `.hzp`-fil dubbelklickad via en framtida Windows-filkoppling hade bara öppnat appen tom. Fix: anropar nu den redan existerande `self._load_hzp(hzp_path)` i slutet av `__init__` om en sökväg gavs. `__main__`-blocket skickar nu `sys.argv[1]` vidare — men bara om det faktiskt ser ut som en riktig `.hzp`-fil (rätt filändelse + filen existerar), så ett oväntat kommandoradsargument inte av misstag tolkas som ett projekt.

**En riktig, sedan tidigare existerande databasförlust-bugg hittades UNDER TESTSKRIVANDET** (inte teoretiserad — reproducerad direkt mot den riktiga `Database`-klassen innan något ändrades): `_load_hzp()`s ordning var kopiera-ny-databas-över-DB_PATH ➜ stäng-den-gamla-anslutningen ➜ öppna-om. Att stänga en fortfarande öppen WAL-läges SQLite-anslutning utför en checkpoint som skriver DEN ANSLUTNINGENS EGNA buffrade (gamla, för-kopieringen) WAL-data tillbaka till vad `DB_PATH` nu heter — vilket tyst skrev över den precis inkopierade nya databasen med den gamla projektets tomma/gamla tillstånd. Detta drabbar BÅDA vägarna in i `_load_hzp`: det nya `.hzp`-kommandoradsargumentet OCH den redan existerande "Öppna (.hzp)…"-menyn (`_hzp_open`, en tunn wrapper runt samma metod) — dvs att byta till ett ANNAT projekt i ett redan körande fönster kan ha tystat tappat bort det nya projektets data i produktion, inte bara i mitt testscenario.

**Fix:** vände ordningen — stäng den gamla anslutningen FÖRST (checkpointen landar då på den GAMLA filen, som ändå skulle kastas, innan den nya någonsin skrivits dit), sedan kopiera. Den ursprungliga avsikten bakom den gamla ordningen (kommentaren sa uttryckligen "Close the old connection AFTER the copy succeeds so a failed copy... leaves self.db in a working state") bevaras ändå: om kopieringen misslyckas öppnas `DB_PATH` bara om igen (den filen rördes aldrig av ett misslyckat `shutil.copy2`), så samma återhämtningsgaranti kvarstår utan att den fungerande-kopian-blir-överskriven-bieffekten finns kvar.

**Verifiering:** 3 nya tester i `tests/test_hazop.py` (`MainWindowOpensHzpPassedOnConstructionTests`) — `.hzp` given vid konstruktion laddas faktiskt (inte bara accepteras tyst), inget argument startar fortfarande på det tomma standardprojektet som förut, OCH ett separat test som specifikt reproducerar bytet-av-projekt-i-ett-redan-körande-fönster-scenariot (den mer sannolika verkliga triggern) och bekräftar att rätt projekts data faktiskt laddas. Alla tre skrevs MOT den riktiga `Database`-klassen (två separata temp-databaser + en riktig `.hzp`-fil byggd via den riktiga `_write_hzp()`, inte handbyggda zip-fixturer) för att verkligen träna WAL-checkpoint-vägen, inte bara mocka runt den. Full 14-filssvit (831 tester, upp från 828) + `test_constants` (5) + `test_smoke` gröna.

## Paketera HAZOP-appen som en installationsfil — del 3: PyInstaller-bygge (2026-08-21)

**Genomförande:** `pip install pyinstaller` (6.22.2). Ny `hazop.spec` (onedir-läge, `hazop.py` som entry point, `icons/` som medföljande data, `packaging/app_icon.ico` som exe-ikon). Ny `packaging/app_icon.ico` — genererad (inte handritad) via ett nytt engångsskript `dev_scripts/generate_app_icon.py` som renderar `icons/shield.svg` omfärgad till appens egen accentblå (#2F5FD0) i sju upplösningar via Qt:s egen SVG-renderare, packat till en multi-storleks `.ico` via Pillow — lätt att byta ut mot en riktig ProSa-logotyp senare genom att bara peka skriptet på en annan SVG.

**Ett upptäckt bloat-problem, åtgärdat innan bygget godkändes:** första körningen blev 863 MB — `rapidocr_onnxruntime`s `collect_all()` i specen var i sig inte boven; PyInstallers vanliga import-analys hittade att `easyocr` råkade vara installerat i den här utvecklingsmiljön (från tidigare manuell `pip install easyocr`-testning) och buntade in HELA dess beroendekedja: `torch` (364 MB!), `torchvision`, `scipy`, `matplotlib`, `sympy` — för en OCR-motor som `equipment_detection.py` bara försöker som reservval BAKOM `rapidocr_onnxruntime`, och som beslutet tidigare i denna session uttryckligen sa skulle UTESLUTAS ur paketeringen (bara rapidocr behöver fungera). Fix: `excludes=['easyocr', 'pytesseract', 'torch', 'torchvision', 'scipy', 'matplotlib', 'sympy']` i specen — säkert eftersom appen redan hanterar `HAS_EASYOCR`/`HAS_TESSERACT` som `False` via sina egna `try/except ImportError`-block. Resultat: 863 MB → 342 MB.

**Verifiering (mot den RIKTIGA byggda exe:n, inte bara källkoden):** kopierade `dist/HazopTool/` till en helt fristående testmapp (för att verkligen testa "installerad någon annanstans"-scenariot, inte bara köra i repo-mappen där gamla filer redan finns), startade `HazopTool.exe` och bekräftade:
- `hazop_project.db`, `hazop_backups/`, `crashes/`, `hazop_crash.log` skapades ALLA bredvid exe:n i den nya mappen — path-fixen från del 1 fungerar i en riktig fryst bygge, inte bara i mockade enhetstester.
- `hazop_crash.log` visar en helt ren uppstart: databasmigrering (57 kolumnmigreringar), schemavalidering, noll fel/undantag.
- En riktig skärmdump av det körande fönstret (`PrintWindow`-API, inte en vanlig skärmdump som bara fångade utvecklingsmiljön) bekräftar hela gränssnittet renderar korrekt — meny, navigeringsraden med alla SVG-ikoner, flikar, tabeller, svensk text, appens etablerade styling.
- Appen stängdes ner rent (`CloseMainWindow`, ingen kvarvarande process).

**Uppdaterat:** `.gitignore` (nya `/build/`, `/dist/`, `packaging/output/`-rader — byggda binärer, inte källkod; `hazop.spec` och `packaging/`-innehållet committas).

**Nästa steg:** Inno Setup-installern (`.iss`-skriptet finns redan skrivet i `packaging/hazop_installer.iss`, men Inno Setup självt är inte installerat på den här maskinen och varken `winget` eller `choco` finns tillgängligt för att installera det automatiskt — kräver antingen manuell installation eller ett uttryckligt godkännande att ladda ner det).

## Paketera HAZOP-appen som en installationsfil — del 4: Inno Setup-installern kompilerad och verifierad (2026-08-21)

Anton installerade Inno Setup själv ("Nu har jag installerat det") — hittades på `C:\Program Files\Inno Setup 7\ISCC.exe` (version 7, inte 6 som CLAUDE.md:s kommando ursprungligen antog). `ISCC.exe packaging\hazop_installer.iss` kompilerade rent på första försöket → `packaging\output\HazopSetup.exe` (109 MB, komprimerad från 342 MB via LZMA2).

**Fullständig end-to-end-verifiering mot den RIKTIGA installern, på den riktiga maskinen** (inte bara källkoden — en tyst installation/avinstallation, sedan städat undan alla spår efteråt):
1. `HazopSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS="desktopicon,fileassoc"` — installerade rent till `%LOCALAPPDATA%\ProSa\HAZOP Tool\`, inget admin-krav.
2. Bekräftat: Startmenygenväg + avinstallationsgenväg skapade, `.hzp`-filassociation registrerad i `HKCU\Software\Classes` (inte `HKLM`, matchar per-användare-installationen), avinstalleringspost syns i registret med rätt `DisplayName`/`UninstallString`.
3. Startade den installerade `HazopTool.exe` direkt och tog en riktig fönsterskärmdump (Win32 `PrintWindow`-API, inte en vanlig skärmdump — den hade bara fångat utvecklingsmiljön eftersom det körande HAZOP-fönstret inte nödvändigtvis är det aktiva/synliga fönstret i den här miljön) — gränssnittet renderar identiskt korrekt från den installerade platsen.
4. **`.hzp`-filassociationen testad på riktigt**, inte bara i enhetstest: byggde en fristående `.hzp`-testfixtur (en riktig zip med en riktig databas, samma format `_write_hzp` producerar) med en unik testnod, öppnade den via `Start-Process <sökväg>.hzp` (Windows löser association-kedjan precis som ett dubbelklick skulle) — fönstertiteln bytte omedelbart till `HAZOP Tool — installer_test_project.hzp`, vilket bara händer efter en lyckad `_load_hzp()`-körning. Bekräftar hela kedjan installer → registeruppslag → `HazopTool.exe "%1"` → `sys.argv[1]` → `_load_hzp()` (del 2 ovan) fungerar i verkligheten, inte bara i mockade tester.
5. Avinstallerade tyst (`unins000.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`) — programfilerna (`HazopTool.exe`, `_internal/`, avinstalleraren själv), Startmenygenvägen, `.hzp`-ProgID-nyckeln och registerposten för avinstallation försvann alla korrekt. Användardata (`hazop_project.db`, `crashes/`, `hazop_backups/`, `hazop_crash.log`) lämnades AVSIKTLIGT kvar — standardbeteende för en avinstallerare (samma anledning som att avinstallera Word inte raderar dina `.docx`-filer), inte en brist. Den enda kvarlämningen var en tom `.hzp`-nyckel utan värde under `HKCU\Software\Classes` (Inno Setups egna `uninsdeletevalue`-flagga tar bara bort VÄRDET på den delade filtillägg-nyckeln, aldrig nyckeln själv — det är den etablerade, medvetet försiktiga konventionen, ifall ett annat program också skulle registrera samma filtyp).
6. Städade manuellt bort alla testspår från den riktiga maskinen efteråt: den tomma `.hzp`-registernyckeln, hela `%LOCALAPPDATA%\ProSa\`-mappen (kvarlämnad testdata), testfixturens temp-mapp.

**Alla fyra delar av "Genomför en installationsfil"-uppdraget är nu klara och verifierade end-to-end:** frozen-path-fixen (del 1), `.hzp`-kommandoradsstödet + en riktig databasförlust-bugg hittad och fixad på vägen (del 2), PyInstaller-bygget med bloat-städning (del 3), och nu Inno Setup-installern (del 4). `HazopSetup.exe` ligger i `packaging/output/` (gitignorad, bygg om lokalt med kommandona i CLAUDE.md:s nya "Packaging"-avsnitt).

## "Analysera P&ID kraschar och startar om appen vid flera sidor" — tre separata buggar hittade och fixade (2026-08-24)

**Rapport:** Anton: "När man klickar på analysera p&id och det finns flera sidor så kraschar programmet och startar om." Bara i den paketerade `.exe`:n — aldrig i `python hazop.py`.

**Bugg 1 (den efterfrågade) — `multiprocessing.freeze_support()` saknades.** `ParallelTagScanWorker`/`ParallelEquipmentAnalysisWorker`/`ImageSymbolSearchWorker` (pid_viewer.py) växlar till `concurrent.futures.ProcessPoolExecutor` för dokument med **4+ sidor** (`_should_parallelize`, `n_pages >= 4`) — exakt tröskeln för "flera sidor". På Windows kör en ny process den FRYSTA exe-filen från grunden när den startas, om inte `multiprocessing.freeze_support()` redan körts — utan den återinträdde varje worker-process hela `__main__`-blocket som om den startats på nytt och öppnade ÄNNU ett fullständigt `MainWindow`, vilket ser ut precis som "kraschar och startar om" utifrån. Fix: `import multiprocessing; multiprocessing.freeze_support()` som allra första sats i `hazop.py`s `if __name__ == '__main__':`, innan något annat. No-op opaketerat — 828/831-testsviten oförändrad grön. Två nya regressionsvakter i `tests/test_hazop.py` (`MultiprocessingFreezeSupportTests`) verifierar källkodsnivå att detta faktiskt är först och kommer före `QApplication`-konstruktion — kan inte testas beteendemässigt eftersom `freeze_support()` är en dokumenterad no-op opaketerat/icke-Windows, så det finns inget sätt att träna det verkliga felfallet från en vanlig testkörning.

**Bugg 2 (hittad UNDER omverifieringen, helt orelaterad) — `QOpenGLWidget` odefinierad vid ImportError.** Ombygge av exe:n för att verifiera fix #1 kraschade DIREKT vid start (innan `.hzp`/multiprocessing ens var inblandat) med `ImportError: cannot import name 'QOpenGLWidget' from partially initialized module 'pid_viewer' (most likely due to a circular import)`. Roten: `pid_viewer.py`s `try: from PyQt6.QtOpenGLWidgets import QOpenGLWidget / except ImportError: HAS_OPENGL = False` glömde sätta `QOpenGLWidget = None` i `except`-grenen — till skillnad från den identiska `QSvgRenderer`-koden precis under, som gör det rätt. Om `PyQt6.QtOpenGLWidgets` inte går att importera i en given miljö (hänt icke-deterministiskt i flera ombyggen denna session, trolig orsak: hur PyInstaller råkar bunta det här ovanliga, valfria Qt-modulet) finns namnet `QOpenGLWidget` helt enkelt inte alls i modulen — och Pythons felmeddelande gissar MISSVISANDE på "cirkulär import" som trolig orsak, fast den verkliga orsaken är att namnet aldrig definierades. Fix: `QOpenGLWidget = None` tillagt i `except`-grenen, matchar mönstret som redan används korrekt för `QSvgRenderer`/`fitz`/`pytesseract`/`_PILImage` i samma fil — verifierat att det VAR det enda stället i filen med denna asymmetri.

**Bugg 3 (avfärdad, INTE en riktig bugg) — "Could not load the Qt platform plugin 'windows'".** Efter fix #2 kraschade nästa testbygge fortfarande, nu med detta Qt-felmeddelande. Lade till en PyInstaller runtime-hook (`packaging/rthook_qt_dll_dir.py`, `os.add_dll_directory()` för `PyQt6/Qt6/bin/`) som en rimlig defensiv åtgärd — men grundorsaken visade sig vara nåt helt annat och redan löst av sig självt: testmappen låg under Claude Codes eget scratchpad, vars sökväg (med inbäddade sessions-GUID:er) är extremt lång — `qwindows.dll`s egen `LoadLibrary`-anrop misslyckades med `WinError 206: filnamnet eller filnamnstillägget är för långt`, en klassisk Windows MAX_PATH (260 tecken)-begränsning, bekräftad genom att köra EXAKT samma bygge från en kort sökväg (`C:\hzt\`) — startade då direkt utan problem. Den riktiga installationsplatsen (`%LOCALAPPDATA%\ProSa\HAZOP Tool\`) är gott och väl under gränsen, så detta hade aldrig drabbat en verklig användare. Runtime-hooken behölls ändå (ofarlig, och en äkta förbättring ifall en användare någon gång installerar till en ovanligt djupt nästlad sökväg) — men den var INTE den faktiska fixen för vad som såg ut som en tredje bugg.

**Verifiering av den faktiska buggen (#1), mot en riktig ombyggd `.exe`, inte bara resonemang:** byggde en syntetisk 5-sidig PDF (PyMuPDF, i linje med hur `test_symbol_geometry.py` redan bygger testfixturer), pekade en fristående testdatabas på den, startade den paketerade exe:n från en kort sökväg, navigerade till P&ID-vyn via UI Automation (`System.Windows.Automation`, eftersom navigeringsraden P&ID-ikonen inte har QAccessible-namn — hittad via bounding-rect-position istället), klickade "Analysera P&ID" på riktigt. Innan fix #1 (kontrollgrupp, samma testupplägg): exakt samma krasch reproducerad 3/3 gånger, ÄVEN med `hazop.py` återställd till sitt senast committade (tidigare verifierade fungerande) skick — bevisar att kraschen redan fanns latent, inte orsakad av min ändring. Efter alla tre fixarna: exakt EN process genom hela körningen (aldrig fler), `Responding=True` hela tiden, och en ren `=== HAZOP Tool exited (code 0) ===`-rad i loggen — ingen krasch, ingen omstart, ingen dubblettprocess.

**Ombyggt och testat:** PyInstaller-bygget (nu 342 MB, alla tre fixar inbakade) och Inno Setup-installern (`HazopSetup.exe`) om båda — tyst installation/avinstallation, ren start från den installerade platsen, allt städat bort från den riktiga maskinen efteråt igen.

## En FJÄRDE, helt separat bugg: "Analysera P&ID" kraschade fortfarande — `causes.equipment_id` FK saknades i cleanup (2026-08-24, samma dag, uppföljning)

**Rapport:** Anton, efter att ha testat de tre fixarna ovan mot sitt riktiga projekt: "kolla senaste crash-filen. krashchade igen." Två nya riktiga kraschrapporter (`crash_20260824_132650_IntegrityError.json`, `crash_20260824_143009_IntegrityError.json`) — **inte** samma bugg som ovan (den var paketerings-/multiprocessing-specifik och kraschade aldrig i `python hazop.py`; dessa två kraschade i just det, direkt från källkoden, oberoende av allt tidigare arbete idag).

**Roten:** `sqlite3.IntegrityError: FOREIGN KEY constraint failed` i `clear_equipment_catalog()`s `DELETE FROM equipment_catalog`. Tre kolumner pekar på `equipment_catalog(id)`: `equipment_markers.equipment_id` (har `ON DELETE CASCADE`, ofarlig), `deviations.equipment_id` (2026-08-07, redan nollställd i både `delete_equipment_item()` och `clear_equipment_catalog()` sedan en tidigare krasch samma dag) — och **`causes.equipment_id`** (2026-08-13, "Live tag-länk mellan Orsak-cellens taggremsa och objektet på P&ID", se NOTES_ARCHIVE.md), som ALDRIG lagts till i någon av de två städfunktionerna. En riktig P&ID med taggade orsaker (kolumnens hela syfte) → `DELETE FROM equipment_catalog` blockeras av kvarvarande `causes.equipment_id`-referenser → krasch, exakt vad båda dagens rapporter visar.

**Fix:** `UPDATE causes SET equipment_id=NULL WHERE ...` tillagt i BÅDA `delete_equipment_item()` och `clear_equipment_catalog()`, samma mönster som redan användes för `deviations.equipment_id`. Verifierade att detta var en UTTÖMMANDE lista genom att grep:a hela `database.py` efter `equipment_catalog(id)` — bekräftat exakt tre träffar, inga fler saknas.

**Verifiering:** 2 nya tester i `tests/test_database.py::EquipmentForeignKeyCleanupTests` (samma klass som redan täckte `deviations.equipment_id`-fallet) — byggde EXAKT samma scenario som kraschrapporterna (orsak med `causes.equipment_id` satt via `update_cause()`, sedan `delete_equipment_item()`/`clear_equipment_catalog()`). Bekräftade att båda testerna FAKTISKT reproducerar den riktiga `IntegrityError`:n innan fixen (körde dem mot `database.py` tillfälligt återställt via `git stash`) och passerar efter. Full `test_database.py`-svit (60 tester) grön.

**Lärdom:** samma "hitta-en-FK-i-taget"-mönster som redan hänt en gång denna dag (`equipment_catalog.node_id` → `deviations.equipment_id` → nu `causes.equipment_id`) — nästa gång en ny `*_id INTEGER REFERENCES equipment_catalog(id)`-kolumn läggs till utan `ON DELETE CASCADE`, lägg till den i BÅDA `delete_equipment_item()` och `clear_equipment_catalog()` direkt, innan den hinner nå en användares riktiga projekt.

## Åtta UX/logik-förbättringar: standardnod, popup-placering, gummiband, dubbletter, +Orsak, Auto-collapse (2026-08-24)

**Beställning:** Anton gav en lista med åtta konkreta förbättringar baserade på verklig användning av trädet/P&ID-flödet. Genomförda i denna ordning:

1. **Standardnod vid ny studie.** `Database.__init__` beräknar redan `pre_existing_db` (om `.db`-filen fanns innan denna konstruktion) för sitt eget backup-beslut — samma flagga återanvänds nu: `if not pre_existing_db: self.add_node()`. Täcker BÅDE första appstart utan `.hzp` OCH "Nytt projekt" (som raderar den gamla filen innan den konstruerar en ny `Database`), men INTE `_load_hzp()` (där `DB_PATH` redan pekar på riktig, kopierad data) — en användare som medvetet raderat sin sista nod i ett riktigt projekt påverkas alltså inte.
2. **Popup-placering får inte täcka markeringen.** `EquipmentPlacementPopup.show_near(punkt)` kunde hamna rakt ovanpå gummibandsrektangeln (anchor var alltid rektangelns CENTRUM). Ny metod `show_near_rect(left, top, right, bottom)` provar höger → vänster → under → över och väljer sidan med mest utrymme, skärm-clampad som `show_near`. Endast gummibandsflödet (`pdf_rect is not None`) använder den nya metoden — vanlig högerklicksplacering (ingen rektangel) är oförändrad.
3. **Tydlig "+ Lägg till"-knapp för objekttyp.** Både `EquipmentPlacementPopup._add_new_type` (pid_panel_mod.py) och `EquipmentTagPopup._add_new_type` (equipment_panel.py) hade en bar `"+"`-kvadratknapp utan text. Ersatt med `"+ Lägg till"` (samma ordval som `StandardObjectsSettingsPanel`s knapp), auto-bredd istället för fast kvadrat.
4. **"+ Orsak"-knapp ovanför trädet.** `TreePanel`s action-rad hade redan "+ Nod"/"+ Avvikelse" men Orsak var bara nåbar via högerklick på en avvikelse. Tredje knapp tillagd, kopplad rakt till den REDAN BEFINTLIGA `add_cause()`-metoden (som redan har "Välj en avvikelse i trädet"-skyddet) — ingen ny logik behövdes.
5. **Textigenkänning ENDAST inom gummibandet.** `EquipmentTagSearchWorker.run()` föll, om `extract_tag_from_rect` inte hittade nåt i rektangeln, tillbaka på `find_tag_near_point` (radie- sedan hel-sida-sökning från rektangelns mittpunkt) — gav fel taggar på P&ID:er där taggtext saknas eller ligger långt bort. Fallbacken borttagen helt för rektangel-fallet; punkt-baserad sökning (vanlig högerklicksplacering) är oförändrad.
6. **Dubbletter av taggnummer.** (a) `_commit_tag` visar nu en riktig `QMessageBox.warning` ("Ett objekt med taggnummer X finns redan på denna P&ID") utöver den befintliga tysta hint-etiketten/sammanslagningen — men BARA vid en direkt användarinmatning (`show_warning=True`, default), INTE vid `set_detected_tag`s passiva auto-ifyllning från bakgrundssökningen (`show_warning=False`) — en modal som dyker upp av sig själv utan att användaren gjort nåt hade varit förvirrande, inte hjälpsam. (b) Rot­orsaken till en misstänkt "dubbla avvikelser vid omskanning"-bugg hittades: `apply_scan_result_to_equipment_catalog` gjorde `clear_equipment_catalog()` (radera allt) + återskapa vid VARJE omskanning — varje tagg som överlevde fick ett HELT NYTT `equipment_catalog.id`, vilket gjorde befintliga avvikelse-/orsakskopplingar frikopplade (`equipment_id=NULL`); kryssade användaren i samma avvikelse igen efteråt skapades en äkta dubblett eftersom `get_or_create_deviation` matchar mot det NYA (icke-matchande) id:t. Fix: diff-baserad synk — en tagg som överlever en omskanning (matchad skiftlägesokänsligt) UPPDATERAS på plats (ny `Database.update_equipment_scan_fields`, uppdaterar bara tag/prefix/sida/is_ocr — INTE typ/beskrivning, så manuella redigeringar överlever också en omskanning) istället för att raderas och återskapas; bara genuint försvunna taggar raderas (via redan fixade `delete_equipment_item`).
7. **Förenklad popup vid gummibandsmarkering.** `EquipmentPlacementPopup` fick en `simple=True`-flagga: ingen inbäddad avvikelse-checklista, fältet heter "Objekt:"/"Objekttyp:" istället för "Tag:"/"Typ:". `place_equipment_marker` väljer `simple=True` när `pdf_rect is not None` (gummiband) — vanlig högerklicksplacering behåller den fulla popup:en oförändrad. Vill användaren lägga till avvikelser på det nyskapade objektet klickar de på markören precis som för vilket annat objekt som helst, vilket redan öppnar `EquipmentDeviationBar`.
8. **"Auto-collapse"-toggle under trädet.** Ny `QCheckBox`, persisterad via `Database.get_config`/`set_config` (`app_config`-nyckel `'tree_auto_collapse'`, samma mönster som `tag_strip_spaces` — ingen `QSettings` i denna kodbas). Ny `TreePanel._apply_auto_collapse()`: fäller ihop alla `NODE_T`-items utom den aktiva (härledd från `self.tree.currentItem()`s förfäderskedja), och inom den aktiva noden alla `DEV_T`-items utom den aktiva — strukturella grupperingsnivåer (Ledord/Utrustning) mellan aktiv nod och aktiv avvikelse tvingas expanderade så avvikelsen förblir synlig. Anropas både från `refresh()` (efter en full data-ombyggnad) och `_on_select` (så ett vanligt klick fäller ihop den förra noden direkt, utan att vänta på en refresh).

**Test­på­verkan av standardnoden (punkt 1) — flera BEFINTLIGA tester antog en helt tom, nod-lös databas:** `tests/test_hazop.py` (en explicit `assertEqual([...], [])`), `tests/test_tree_panel.py` (en numrerings-test som räknade ALLA noder i trädet, dubblerade 1-16 till 1,1,2,2...16,16), `tests/test_worksheet.py` (6 tester, node-combo count/index off-by-one) samt `tests/test_node_markup.py` (prev/next-navigering trodde sin egen första nod var den absolut första). Alla fixade genom att antingen uppdatera förväntat värde eller (worksheet/node_markup) radera den auto-seedade noden i `setUp()` innan testets egna, kontrollerade nod-uppsättning byggs. En FEMTE, redan existerande men LATENT testbugg avslöjades av samma ändring: `tests/test_integration.py`s två "quick add cause"-tester anropade `db.causes(dev_id)` — men `Database.causes(x)` filtrerar på `node_id`, inte `deviation_id` (`causes_for_deviation(x)` är rätt metod) — testet råkade bara passera tidigare eftersom en helt tom databas gav `node_id == deviation_id == 1` av ren slump. Fixat till att anropa rätt metod.

**Verifiering:** ny regressionstest per punkt (`tests/test_database.py`, `tests/test_equipment_detection.py`, `tests/test_pid_viewer.py`, `tests/test_pid_panel_mod.py`, `tests/test_tree_panel.py`) — bland annat en direkt reproduktion av dubbla-avvikelser-buggen (punkt 6b) som bekräftar samma `equipment_catalog.id` och avvikelse-koppling överlever en omskanning. Full 14-filssvit (856 tester, upp från 828 — nya tester denna session) grön.

**En verklig hängning hittad och fixad UNDER testarbetet (inte en del av beställningen, men värt att komma ihåg):** `EquipmentPlacementPopup._tag_edit`s `editingFinished`-signal kan fyras SYNKRONT när popup:en förstörs via `deleteLater()` (fokusförlust-kaskaden), men `deleteLater()` är uppskjuten — den faktiska förstörelsen (och därmed signalen) kan hamna i ett SENARE tests `processEvents()`-anrop istället för det egna testets `tearDown()`. Innan punkt 6a fanns var detta ofarligt (bara en tyst hint-etikett); med den nya blockerande `QMessageBox.warning` hängde ett par tester i `tests/test_pid_panel_mod.py` på en riktig, aldrig-avfärdad dialog. Fix: `tearDown()` blockerar nu signaler på varje kvarvarande popups `_tag_edit` INNAN `deleteLater()` schemaläggs — en beständig per-objekt-flagga, så det spelar ingen roll vilket senare tests event-loop-varv som faktiskt kör förstörelsen.

## Riv bort orsaksväljaren "Lägg till orsak på P&ID" (2026-08-24, samma dag, uppföljning)

**Beställning:** Anton: "Jag vill att du tar bort funktionen 'lägg till orsak på P&ID' som ger dialogrutan, riv denna funktion. Högerklick på avikelse lägg till orsak ska skapa en ny orsak under avikeslen nere i hazop scenario. Detta skall även den nya knappen ovanför trädet göra." (den "nya knappen" är dagens "+ Orsak"-knapp, se föregående avsnitt punkt 4).

**Identifiering:** "Lägg till orsak på P&ID" är den bokstavliga rubriktexten (`QLabel`) inuti `StandardCausesPickerPopup` (`tree_panel.py`, `setWindowTitle("Lägg till orsak")` + synlig header "Lägg till orsak på P&ID") — INTE `CauseObjectPopup` (verklig titel "Objekt / Standardorsak", används av HAZOP scenario-tabellens egna "+ Ny orsak"-rad/tom-ORS-cell-klick, orörd av denna ändring). `StandardCausesPickerPopup` öppnades bara från ETT ställe: `TreePanel._open_cause_picker_for_deviation()`, i sin tur anropad av `add_cause()` (knappen ovanför trädet + högerklicksmenyns "+ Lägg till orsak") och `_add_cause_for_deviation()` (Enter-tangenten på en avvikelse i trädet).

**Genomförande:**
- `TreePanel.add_cause()`/`_add_cause_for_deviation()` skapar nu orsaken direkt — `_create_cause_from_pick(self.db, dev_id, None, None)` (samma redan existerande, delade hjälpfunktion, defaultar till "Ny orsak"-platshållartext + skapar en tom konsekvens på samma gång) — precis samma "inget popup"-mönster som `add_consequence()`/`add_safeguard()` redan använde. `exit_pid_mode_requested.emit()` + `refresh(CAUSE_T, new_id)` + `structure_changed.emit()` matchar de andra två metoderna exakt.
- `_open_cause_picker_for_deviation()` borttagen helt; `StandardCausesPickerPopup`-klassen (464 rader) raderad helt ur `tree_panel.py`.
- Re-exporter borttagna i `hazop.py` och `scenario_panel.py`; oanvänd import borttagen i `equipment_panel.py`. `_resolve_std_deviation_id`-importen i `tree_panel.py` togs bort (blev oanvänd i den filen efter borttagningen — funktionen själv lever kvar i `ui_helpers.py`, används fortfarande av `scenario_panel.py`/`pid_panel_mod.py`).
- Kvarvarande kommentarer som nämnde klassen (i `database.py`, `pid_panel_mod.py`, `scenario_panel.py`, `ui_helpers.py`) uppdaterade eller lämnade som historisk kontext där de fortfarande stämmer; `CLAUDE.md`s arkitekturlista för `tree_panel.py` uppdaterad.

**Testpåverkan:** `tests/test_integration.py::AutoConsequenceOnCauseAddTests::test_tree_add_cause_via_picker_also_creates_empty_consequence` bytte namn och skrevs om till att anropa `tree._add_cause_for_deviation(dev_id)` direkt (ingen dialog att mocka längre) — samma assertion (orsak + tom konsekvens skapas). Samma ombyggnad i `tests/test_tree_panel.py::TreePanelAddCauseButtonTests::test_clicking_button_with_a_deviation_selected_adds_a_cause` (denna sessions egen, nyss tillagda test för "+ Orsak"-knappen). Högerklicksmenyns etikett-tester ("Lägg till orsak" syns i menyn) rör bara vilket namn menyalternativet har, inte vad det gör — opåverkade.

**Verifiering:** full 14-filssvit grön efter ändringen.

## Auto-collapse: dela upp i två separata kryssrutor — "avvikelser"-halvan gjorde faktiskt ingenting synligt (2026-08-24, samma dag, ytterligare en uppföljning)

**Rapport:** Anton: "Autocollapse funktion funkar bra med att öppna mellan noder, dvs den stänger den andra noden. Men den funkar inte för avikelser. Lägg till ytterligare en kryssruta och kalla den tidigare funktionen auto-collapse nodes och den senare autocollapse avikelse."

**Rotorsak:** den ursprungliga, kombinerade `_apply_auto_collapse()` (se punkt 8 ovan) använde `item.setExpanded(id_ == active_dev_id)` för `DEV_T`-rader — men `setExpanded()`/`isExpanded()` styr bara om ETT ITEMS EGNA BARN visas eller ej, inte om raden SJÄLV är synlig. En nod som kollapsas döljer sitt HELA underträd (alla avvikelser med den), vilket är varför nod-halvan syntes fungera perfekt — men en avvikelse utan egna orsaker (den vanligaste startpunkten, "Ny nod" ger 16 tomma avvikelser) har inget att kollapsa i första taget; `setExpanded(False)` på den gjorde bokstavligen ingenting synligt. Qt har inget koncept för "kollapsa en rad så att RADEN SJÄLV döljs" — bara `setHidden()` gör det.

**Fix:**
- Två separata `QCheckBox` under trädet: **"Auto-collapse nodes"** (`app_config`-nyckel `tree_auto_collapse_nodes`, samma logik som tidigare — `setExpanded()` på `NODE_T`) och **"Auto-collapse avvikelser"** (ny nyckel `tree_auto_collapse_deviations`) som istället använder `setHidden(True)` på varje `DEV_T`-rad förutom den aktiva, i hela trädet (inte bara inom aktiv nod — ingen avvikelse är "aktiv" i en icke-aktiv nod ändå).
- En `Ledord`/`Utrustning`-grupperingsrad vars samtliga avvikelse-barn blivit dolda döljs nu också (annars en tom rubrikrad utan poäng kvar) — beräknas i en andra, omvänd (djupast-först) genomgång så en `Ledord`-wrapper runt en `Utrustning`-wrapper hinner se sitt barns redan beslutade dold-status innan sin egen avgörs.
- Den gamla, enskilda `app_config`-nyckeln `tree_auto_collapse` lämnas oanvänd/övergiven (ingen datamigrering — en enda kryssruteinställning, försumbar kostnad att behöva kryssa i den nya "nodes"-rutan på nytt).

**Testpåverkan:** `tests/test_tree_panel.py::TreePanelAutoCollapseTests` skrevs om helt — separata tester per kryssruta, plus ett explicit regressionstest (`test_nodes_toggle_alone_does_not_hide_deviations_within_active_node`) som bekräftar den rapporterade buggen (nod-rutan ensam rör inte avvikelser) och ett för det delade Ledord-grupp-fallet.

**Verifiering:** full 14-filssvit grön efter ändringen.

## Ny toppnivå "System" ovanför Nod + grå (istället för röd) markörfärg för objekt utan avvikelse (2026-08-24, samma dag, ytterligare en uppföljning)

**Beställning:** Anton: "jag vill att du inkluderar en kategori som står
över alla andra som heter system så hierarkin består av system, nod
avvikelse, osv." samt (mindre begäran i samma meddelande, förtydligad
efter en fråga: "alltså om ingen avvikelse är kopplad. du vet") "jag vill
även att du ändrar färgen på objekt som ej är tillagda på p&id från röda
till grå" — dvs. en P&ID-objektmarkör utan någon kopplad avvikelse.

**Del 1 — grå markörfärg (liten, gjord direkt utan plan):**
`PIDGraphicsView.add_equipment_marker()` (`pid_graphics_view.py:1992-1994`)
avgör markörfärg via `has_deviations = deviation_count > 0` — röd
(`QColor(160,0,0)`/`QColor(220,20,20,90)`) bytt till neutral grå
(`QColor(120,120,120)`/`QColor(150,150,150,90)`); grönt (avvikelse finns)
oförändrat. Ingen annan plats i kodbasen hade denna logik aktiv — en
tidigare röd/grön "placerad på P&ID"-funktion i `scenario_panel.py`
(`_draw_pid_pin`/`_make_pin_icon`) visade sig vara död kod sedan
2026-08-13 (P&ID-canvasen är nu enbart objektplacering, se den sessionens
NOTES.md-post), rörd inte vidare.

**Del 2 — System-hierarkin (kodgenomgång: 3 parallella Explore-agenter
över databasschema/nod-CRUD, `tree_panel.py`s trädrendering, samt alla
ANDRA ställen som listar noder):** `nodes`-tabellen var helt platt (inget
`sort_order`, ingen förälder-koppling), och trädet var det ENDA stället
som faktiskt renderade noder hierarkiskt (`addTopLevelItem` förekom exakt
en gång i hela `tree_panel.py`). **Medveten avgränsning:** System
implementerades som en riktig nivå i TRÄDET plus fullständig CRUD; andra
ytor (Worksheet-nodväljaren, "Visa samtliga noder", export, global
sökning, `hazop_preparation_panel.py`s Noder-flik, föregående/nästa-nod)
fortsätter läsa `self.db.nodes()` platt, oförändrat — att bygga om t.ex.
scenariotabellens 10 hårdkodade kolumnkonstanter för att också gruppera
på System hade mångdubblat omfattningen utan att vara vad som
efterfrågades.

**Databasändringar (`database.py`):** ny `systems`-tabell
(`id/name/sort_order`, samma form som `node_types`), ny nullbar
`nodes.system_id`-kolumn (nullbar med flit — befintliga projekt får NULL
på alla sina noder, ingen tvingad migrering, renderas ogrupperat). Ny CRUD
modellerad rakt av på `node_types`s redan existerande mönster:
`systems()`, `add_system(name='Nytt system')`, `rename_system`,
`delete_system` (**omfördelar** noder till NULL, kaskaderar INTE —
samma säkra "reassign, don't cascade"-princip som `delete_node_type`),
`reorder_systems`, `set_node_system`. `add_node(system_id=None)` fick en
ny valfri parameter. Standardnods-seedningen (`pre_existing_db`-flaggan,
se tidigare sessions post om detta) utökad: en helt ny studie seedar nu
ETT system OCH en nod under det, inte bara en ensam ogrupperad nod.

**`tree_panel.py`:** ny `SYSTEM_T=8` i `constants.py`. `refresh()`s
toppnivåloop (den enda `addTopLevelItem`-anropspunkten) byggdes om utan
att röra den befintliga per-nod-renderingslogiken alls — hela den gamla
`for`-loopkroppen (Ledord→Utrustning→Avvikelse→Orsak→Konsekvens→
Safeguard-uppbyggnaden, ~185 rader) gjordes om till en lokal closure
`_add_node_item(node, ni, parent_item)` genom att BARA byta ut
loop-headern (`for ni, node in ...:` → `def _add_node_item(...):` +
`nonlocal target`) och den enda `self.tree.addTopLevelItem(nitem)`-raden
(→ villkorlig `parent_item.addChild(nitem)` om satt) — noll omindentering
av kroppen, minimerar risken för att råka ändra beteendet. Ny yttre logik
delar upp `self.db.nodes()` i `nodes_by_system`/`ungrouped_nodes` och
anropar `_add_node_item` en gång per nod, antingen under sitt system-item
eller direkt som toppnivå (ogrupperade noder, bakåtkompatibelt utseende).

Ny **"+ System"**-knapp i action-raden (före "+ Nod"). `add_system()`/
`_rename_system()`/ny `_resolve_system_id(type_, id_)`-helper (samma
DB-fk-vandringsmönster som `_resolve_node_id` m.fl. — `SYSTEM_T` faller
redan korrekt igenom de BEFINTLIGA resolvernas sista `return None` utan
någon ändring där). **`add_node()` härleder nu vilket system den nya
noden hamnar i** från var man står i trädet (samma mönster
`add_cause()`/`add_consequence()` redan använder) — klickar man "+ Nod"
med ett system (eller en nod/avvikelse/etc. under det) markerat hamnar
den nya noden i samma system; annars ogrupperad. Högerklicksmeny:
"Döp om"/"+ Lägg till nod" för `SYSTEM_T`; "Ta bort" (redan generisk för
alla typer) utökad med en tydligare bekräftelsetext för system
("Noderna i det flyttas till ogrupperade, tas inte bort").

**Auto-collapse (förra sessionens tillägg) utökad till System** — INGEN
ny tredje kryssruta; den befintliga "Auto-collapse nodes" täcker nu även
System (samma strukturella "var jobbar jag just nu"-nivå konceptuellt).
`_active_node_and_deviation()` döpt om/utökad till
`_active_system_node_and_deviation()` (3-tuppel). Ancestor-tvingad-
expanderad-loopen kördes tidigare bara om en avvikelse var aktiv — utökad
till att köras från BÅDE den aktiva noden OCH den aktiva avvikelsen (om
någon), annars skulle en aktiv NOD utan någon vald avvikelse inte hålla
sitt eget System-förfäder expanderat.

**En riktig bugg hittad under testskrivning:** `_rename_system` anropade
`system.get('name')` på en rå `sqlite3.Row` (stödjer indexering men inte
`.get()`) — kraschade direkt. Fixad genom att konvertera till `dict()`
innan uppslag, samma mönster `get_node()` redan använder.

**Testpåverkan:** 7 nya tester i `tests/test_database.py`
(`SystemsHierarchyTests`) och 10 nya i `tests/test_tree_panel.py`
(`TreePanelSystemHierarchyTests`) — bland annat att en tom `Database()`
seedar exakt ett system+en nod, att `delete_system` omfördelar (inte
kaskaderar), att "+ Nod" härleder rätt system, och att "Auto-collapse
nodes" fäller ihop icke-aktiva system. `hazop.py`s re-export av
`constants`-tupeln utökad med `SYSTEM_T` (samma "lager + re-export"-
mönster som resten av kodbasen). Full 14-filssvit (873 tester, upp från
856) grön direkt vid första körningen — ingen av de befintliga testerna
antog nod som absolut toppnivå (bekräftat redan under kodgenomgången: inga
`item.parent() is None`-baserade `NODE_T`-antaganden hittades någonstans).

## Kodoptimering: döda klasser borttagna + N+1-frågemönster i trädet och HAZOP-scenariot batchade (2026-08-24, samma dag, ytterligare en uppföljning)

**Beställning:** Anton: "kan du optimera/förbättra koden lite?" — ett öppet,
ospecificerat önskemål. Kartlade konkreta kandidater innan något
genomfördes (se AskUserQuestion-svaret): Anton valde tre av fyra
föreslagna spår — död kod, `TreePanel.refresh()`s N+1-mönster, och
`ScenarioTablePanel._build_rows()`s N+1-mönster (det fjärde, ett
specifikt känt NOTES.md-problem, valdes bort).

**1. Död kod borttagen** — tre klasser med noll användningsställen
någonstans i kodbasen (bekräftat via grep före borttagning):
`SeverityDefinitionsPanel` (`settings_panels.py`, 96 rader),
`TemplateCausePickerDialog` (`pid_viewer.py`, 256 rader — kvarglömd rest
sedan gårdagens borttagning av den gamla orsaksväljar-popupen),
`ConsequenceChainDialog` (`scenario_panel.py`, 86 rader, dess egen
docstring sa redan "kept for legacy compatibility"). Städade samtidigt
bort ett helt gäng nu-oanvända importer i `settings_panels.py` som blev
uppenbara efter borttagningen (`CONFIG`, `SEV_LABELS`, `DEFAULT_MATRIX`,
`DEFAULT_FREQ_BOUNDARIES`, `_STD_OBJECTS`, `_normalise_matrix`,
`_risk_matrix_cache`, `get_matrix`→borta, `freq_to_f_level`, samt en rad
Qt-widgetimporter och `json`/`functools.partial`/`QDate`/`QEvent`/
`QMimeData`/`QDrag`/`QFontMetrics`) — kvarlämningar från en ÄNNU TIDIGARE
uppdelning (2026-08-21, `settings_panels.py` splittrades i flera
underfiler), inte bara från dagens borttagning.

**2. `TreePanel.refresh()`s N+1-mönster batchat.** Trädet gjorde tidigare
en separat SQL-fråga per nod (`deviations`), per avvikelse
(`causes_for_deviation`), per orsak (`consequences`) och per konsekvens
(`safeguards`) — kördes vid nästan varje redigering. Ny generisk
`Database._fetch_grouped(table, fk_column, ids)`-hjälpare (chunkad
`WHERE ... IN (...)`, grupperar resultatet i en `{fk_value: [rader]}`-
dict, samma radform/ordning som motsvarande enstaka-id-metod) plus fyra
tunna wrappers (`deviations_for_nodes`, `causes_for_deviations`,
`consequences_for_causes`, `safeguards_for_consequences`). `refresh()`
hämtar nu allt i EN batch-omgång (4 frågor totalt, oavsett trädstorlek)
innan de befintliga closures (`add_cause_children` m.fl.) körs oförändrat
— bara datakällan bytt (DB-fråga → dict-uppslag), ingen
grupperings-/etikettlogik rörd. En redundant `cause_frequency_level`-
beräkning (kördes en gång PER KONSEKVENS istället för en gång per orsak,
trots att den bara beror på orsaken) lyftes samtidigt ut ur den inre
loopen.

**3. `ScenarioTablePanel._build_rows()`s N+1-mönster batchat — betydligt
djupare (upptäcktes iterativt, inte i ett svep).** Utöver samma fyra
grundfrågor (nu återanvända direkt från punkt 2) hittades och batchades
ytterligare, i tur och ordning när ett regressionstest fortsatte visa
skalning trots gjorda fixar:
- `get_node` per orsak → en `nodes_by_id`-dict.
- `get_consequence_severities`/`get_severity_excluded_sgs`/
  `get_safeguard_excluded_causes` per konsekvens/kategori/safeguard → tre
  nya bulk-metoder (`get_consequence_severities_for_consequences` m.fl.).
- **`_causes_for_node()` anropad en gång PER NOD** i "Visa samtliga
  noder"-läget — ett HELT EGET, tidigare oupptäckt N+1-lager (en fråga
  per nod för avvikelser, sedan en fråga per avvikelse för orsaker) som
  kördes INNAN resten av batchningen ens hann starta. Ny
  `_causes_for_all_nodes()` bygger samma `[(orsak, avvikelse), ...]`-lista
  för HELA studien med samma två redan-byggda bulk-metoder som `TreePanel`
  använder.
- `reduction_factors(cid)` och `actions(cid)` anropade en gång PER
  RENDERAD RAD i `_add_row()` — trots att flera rader ofta delar samma
  konsekvens (`n_rows = max(n_cats, n_sgs, 1)`). Två nya bulk-metoder,
  förhämtade en gång och skickade in som parametrar till `_add_row()`.
- `get_safeguard_excluded_causes(sg['id'])` anropades EN GÅNG TILL inne i
  `_add_row()`, trots att `_build_rows()` redan räknat ut samma sak
  (`excl_causes_by_sg`) en nivå upp — bara aldrig skickades vidare.
- En redundant `get_cause(cons_d['cause_id'])`-fråga i huvudloopen visade
  sig hämta EXAKT samma rad som redan låg i `cause_d` (strukturellt
  garanterat, eftersom `cons_d` kom från `cons_by_cause[cause_d['id']]`)
  — ersatt med `dict(cause_d)` direkt, ingen fråga alls.
- ORS-statusikonens egen `consequences()`/`safeguards_for_cause()`-
  omfrågning per rad (borde vara per orsak) ersattes med ett
  `cause_status`-tripel förhämtat en gång per orsak från redan
  batchad data.

Alla nya `_add_row()`-parametrar (`cause_status`, `rfs`, `acts`,
`excl_causes_by_sg`) defaultar till `None` och faller då tillbaka till
EXAKT samma direkta DB-fråga som tidigare — ingen möjlig anropare
(inklusive framtida) kan gå sönder av att inte skicka in dem.

**Verifieringsmetod:** en delad `_CountingConnProxy`/`count_selects()`-
hjälpare (`tests/test_helpers.py`, ny) sveper in `Database.conn` (går
inte att patcha `sqlite3.Connection`s egna metoder direkt — "attribute is
read-only", C-extensionstyp) och räknar `SELECT`-satser. Två nya
regressionstester (`tests/test_tree_panel.py::TreePanelRefreshQueryBatchingTests`,
`tests/test_scenario_panel.py::ScenarioTablePanelBuildRowsQueryBatchingTests`)
bygger en liten och en stor studie och jämför frågeantalet — **bekräftat
genom kontrollkörning mot koden innan varje fix** (via tillfällig
`git stash`) att dessa tester verkligen slår av på den gamla koden:
`TreePanel.refresh()` gick från 63→483 frågor (2 vs. 20 noder);
`ScenarioTablePanel._build_rows()` gick från 104→716 frågor (2 vs. 15
noder) innan fixarna, mot en helt platt frågeräkning (15→15) efteråt.

**Verifiering:** full 14-filssvit grön.

## "+ Lägg till"-knappen för ny objekttyp i rubberband-popupen ändrad tillbaka till en bar "+" (2026-08-25)

**Beställning:** Anton: "I popuprutan så står det '+ lägg till'. justera
detta till bara '+' och justera knappens storlek (detta efter man dragit
med gummibandet)." — gäller `EquipmentPlacementPopup` (`pid_panel_mod.py`),
den kombinerade tagg+typ-popupen som visas både vid vanlig högerklick-
"🔧 Objekt"-placering och vid höger-drag-gummiband-placering
(`_on_zone_drawn` → `equipment_placement_requested` → `place_equipment_marker`).

Detta är en **direkt reversering** av gårdagens ändring (se föregående
avsnitt "Ny toppnivå 'System' ..."/"Åtta UX/logik-förbättringar" ovan,
2026-08-24) där samma knapp gick från en bar `"+"`-kvadratknapp till en
bredare `"+ Lägg till"`-knapp med text, av precis motsatt skäl (den
bara "+"-knappen ansågs otydlig då). Med typ-comboboxen redan bredvid i
en liten, smal popup blev den textade knappen istället för bred/
klumpig — särskilt i det förenklade `simple=True`-läget (gummiband) som
bara har två fält totalt. Löst genom att gå tillbaka till en bar `"+"`
men behålla en tydlig tooltip ("Lägg till en ny objekttyp") för
förklaring, samt göra knappen kvadratisk (`setFixedSize(H_SMALL_BTN,
H_SMALL_BTN)` istället för `setFixedHeight` + auto-bredd) så den inte
ser ihoptryckt/felproportionerad ut.

Berörd regressionstest i `tests/test_pid_panel_mod.py`
(`EquipmentPlacementRubberBandSimplePopupTests`) — gårdagens
`test_add_type_button_has_visible_text_not_a_bare_plus` bytt mot
`test_add_type_button_is_a_compact_square_plus` (verifierar bar `"+"`,
tooltip, och att knappen är kvadratisk).

**Verifiering:** `test_smoke` + `test_pid_panel_mod` (81 tester, grönt).

## Dublett-taggens varningstext i gummiband-popupen uppdateras nu live + ny "Skapa dublett"-knapp (2026-08-25)

**Beställning:** Anton: "Dubletter vid gummibandsmarkering. Vid pop-upen
finns nu text 'finns redan i katalog'. Om man ändrar taggnummer så det
skiljer sig ska varningstexten direkt försvinna. För att skapa en ny
dublett behövs en verifierar-knapp som dyker på pop-upen." — gäller
samma `EquipmentPlacementPopup` (`pid_panel_mod.py`) som föregående
avsnitt, specifikt dess `_dup_hint`-varning (2026-08-24) för en tagg som
redan finns i katalogen.

**Problem 1 — varningen låg kvar för länge.** `_dup_hint` uppdaterades
bara i `_commit_tag()`, som bara körs vid `editingFinished` (fokus
lämnar fältet/Enter) — skriver man om taggnumret till något som inte
längre är en dublett syns den gamla varningen kvar tills fältet
tappar fokus igen. Löst med en ny `_update_dup_hint_live()` kopplad till
`_tag_edit.textEdited` (kör alltså på VARJE tangenttryckning) — den
uppdaterar bara `_dup_hint`-texten och den nya knappen (se nedan), gör
ALDRIG någon databasskrivning eller sammanslagning, så en dublett-tagg
som råkar skrivas fram mitt i inmatningen inte utlöser en sammanslagning
innan användaren är klar.

**Problem 2 — omöjligt att medvetet skapa en riktig dublett.** Innan
denna ändring körde `_commit_tag()` alltid `_reassign_to_existing()`
(slå ihop med den befintliga raden) så fort en matchande tagg
committades — det fanns inget sätt att säga "nej, jag vill faktiskt ha
två separata objekt med samma tagg". Ny knapp `"Skapa dublett"`
(`_dup_confirm_btn`), dold som standard, visas bara medan den inskrivna
taggen matchar ett annat objekt. Klick sparar taggen på DENNA post
direkt (`_confirm_duplicate()`) istället för att slås ihop, och minns
den bekräftade taggtexten i `_dup_confirmed_tag` så att `_commit_tag()`
(t.ex. vid senare fokus-byte) inte i efterhand slår ihop samma tagg ändå.

**Knepig detalj:** knappen sätts till `Qt.FocusPolicy.NoFocus`. Utan det
skulle ett klick på knappen först få `_tag_edit` att tappa fokus
(`editingFinished`/`_commit_tag()` hinner köra och slå ihop/radera denna
post INNAN knappens egen `clicked()`-hanterare ens körs) — samma
"textfält med inbäddad knapp som inte får stjäla fokus"-mönster som
t.ex. ett sök-fälts inbäddade rensa-knapp använder.

Nya regressionstester i `tests/test_pid_panel_mod.py`
(`EquipmentPlacementAsyncSearchTests`):
`test_dup_hint_and_button_update_live_as_the_tag_is_typed`,
`test_confirm_duplicate_button_creates_a_real_duplicate_without_merging`.

**Verifiering:** `test_smoke` + `test_pid_panel_mod` (83 tester, grönt).

## Dublett-varningen missade tag-completerns egna val (2026-08-25, samma dag, uppföljning)

**Beställning:** Anton: "Varningen skall även komma upp om texten ändras
till ett objekt som heter likadant. Du behöver därför stämma av mot
objektslistan vid textredigering." — följt av förtydligandet "Alltså
varningen i pop-upen", vilket bekräftade att det gäller samma
`EquipmentPlacementPopup` (`pid_panel_mod.py`) som föregående avsnitt.

**Grundorsak:** dublett-kontrollen (`_update_dup_hint_live`, se
föregående avsnitt) var kopplad till `_tag_edit.textEdited` — en Qt-
signal som ENDAST triggas av riktiga tangenttryckningar, inte av att
välja ett förslag ur tagg-completerns rullgardin (`_make_tag_completer`).
Skriver man några tecken och sedan KLICKAR på ett befintligt taggförslag
i completer-listan byts texten ut utan att `textEdited` någonsin
triggas — varningen uteblev alltså exakt i det fall den borde synas som
tydligast (användaren väljer aktivt en tagg som redan finns). Löst genom
att istället koppla till `_tag_edit.textChanged`, som Qt garanterat
triggar vid ALLA textändringar oavsett källa (tangenttryckning,
completer-val, eller programmatisk `setText`) — samma mönster den äldre,
redan korrekt fungerande `EquipmentTagPopup._check_duplicate_tag`
(`equipment_panel.py`) redan använde. `_on_tag_edited_by_user` (kopplad
kvar till `textEdited`) sköter fortsatt bara sitt eget jobb: markera att
användaren skrivit något, så att en sen asynkron tagg-sökning
(`set_detected_tag`) inte skriver över det.

Ny regressionstest i `tests/test_pid_panel_mod.py`
(`test_dup_hint_shows_on_completer_selection_not_just_raw_keystrokes`) —
anropar bara `popup._tag_edit.setText(...)` direkt (ingen `_on_tag_edited_
by_user`-anrop, till skillnad från syskontestet), vilket reproducerar
exakt completer-scenariot. Bekräftat verkligen fånga den gamla buggen
via `git stash` (misslyckades mot koden innan denna fix).

**Verifiering:** `test_smoke` + `test_pid_panel_mod` (84 tester, grönt).

## Nod-klick i trädet öppnar inte längre automatiskt P&ID-ritläge (2026-08-25)

**Beställning:** Anton: "Om man klickar på en nod i trädet idag försvinner
hazop scenario och man kommer direkt in i ritningläget på P&ID. Detta
blir förvirrande. Därför ska du behålla HAZOP scenario och fortsätta
vara i navigeraläget. För att gå in i editerarmode behöver jag aktivt
trycka på pennan till höger."

Detta reverterar HÄLFTEN av 2026-08-18-beteendet (se NOTES.md, samma
dag som "nodmarkup dockas till höger"): `MainWindow._on_selected()`
(`hazop.py`) anropade tidigare ovillkorligen `_on_edit_node_markup(id_)`
för varje `NODE_T`-val i trädet — vilket bytte huvudvyn till P&ID-sidan
(`_switch_view(1)`) OCH satte P&ID-canvasen i markup-redigeringsläge
(`PIDPanel.enter_markup_edit` → `MODE_MARKUP_SELECT`, vilket stänger av
normal navigering/gummiband-placering). Ändrat till att bara göra detta
OM markup-läget redan är aktivt (pennan redan intryckt) — då rebinds det
fortfarande till den nya noden, precis som tidigare, så "ritar jag i
noden skall det vara kopplat till noden jag står på" (2026-08-18)
fortsätter fungera medan man faktiskt ritar. Ett vanligt nodklick i
navigeraläge uppdaterar nu bara ribbonens fält, P&ID:ns "aktiv nod" (för
markörfärgning) och HAZOP scenario-tabellens filter — ingen sidbyte,
inget lägesbyte. Enda vägen in i markup-redigering är nu den explicita
✏️-knappen i `props_ribbon`, högerklick → "Editera nodmarkup", eller
föregående/nästa-nod-knapparna medan man redan editerar.

Berörd testklass `NodeMarkupAutoOpenTests` (`tests/test_integration.py`)
skriven om helt — de tester som tidigare förväntade sig auto-öppning vid
ett vanligt `_on_selected(NODE_T, ...)`-anrop använder nu ett explicit
`_on_edit_node_markup(node_id)` innan de testar rebind/stäng-beteendet;
nya tester lagts till som bekräftar att ett vanligt nodklick INTE
aktiverar markup-läget och INTE byter sida.

**Verifiering:** full 14-filssvit (878 tester, grönt).

## Auto-collapse "avvikelser" dolde hela avvikelser istället för bara orsaks-nivån (2026-08-25, samma dag, uppföljning)

**Beställning:** Anton: "Auto-collapse funktionen för avvikelser funkar
inte som jag vill. Den skall alltså inte dölja avikelser utan den ska
dölja orsaks-nivån och nedåt. Så står jag på högt flöde skall jag bara
se orsaker på högt flöde." — rättar 2026-08-24-implementationen av samma
kryssruta (se ovan, "Autocollapse ... delad i två").

Den tidigare implementationen använde `setHidden(True)` på varje icke-
aktiv `DEV_T`-rad — vilket dolde AVVIKELSEN SJÄLV, inte bara dess
underliggande orsaker, plus en extra "dölj tomma Ledord/Utrustnings-
grupper"-mekanism för att städa upp efteråt. Bytt till samma mönster som
"nodes"-kryssrutan redan använder ETT NIVÅ HÖGRE UPP: `setExpanded(id_ ==
active_dev_id)` på varje `DEV_T`-item. En kollapsad `QTreeWidgetItem`
döljer bara SINA EGNA barn (orsaker, och därmed konsekvenser/safeguards
under dem) — själva raden förblir synlig som syskon till den aktiva
avvikelsen, exakt "dölj orsaks-nivån och nedåt" som efterfrågat. Den nu
onödiga "dölj tomma grupper"-logiken (och dess `group_items`-insamling)
togs bort helt, eftersom inget längre göms på avvikelsenivån som skulle
kunna tömma en grupp.

Fyra regressionstester i `tests/test_tree_panel.py`
(`TreePanelAutoCollapseTests`) skrivna om för de nya semantiken
(`test_deviations_toggle_collapses_causes_but_keeps_every_deviation_visible`
m.fl.) — bekräftat att de faktiskt slår av på koden innan denna fix
(4 av 9 tester i klassen, via `git stash`).

**Verifiering:** `test_smoke` + `test_tree_panel` (79 tester, grönt).

## Vänsterklick på ett P&ID-objekt kan nu redigera tag/typ och ta bort; högerklicksmenyn fick en "Ta bort" (2026-08-25, samma dag, uppföljning)

**Beställning:** Anton: "Om jag vänsterklickar på ett objekt på pid
viewer ska man kunna editera objektnamn (tag) och objekttyp. Man ska
även kunna klicka på deleteknappen för att ta bort. Samt att om man
högerklickar på objektet så ska också alternativet att ta bort finnas."

**Bakgrund:** vänsterklick på en befintlig utrustningsmarkör visade
sedan tidigare bara `EquipmentDeviationBar` — en ren avvikelse-checklista
(2026-08-12, se NOTES.md: "Resten av valen får jag nog göra nere i hazop
scenario"). Tag/typ-redigering och radering fanns bara via högerklick →
"Redigera objekt" (`EquipmentTagPopup`, 2026-08-12) — och den menyn hade
INGEN raderingsfunktion alls, bara redigering.

**1. `EquipmentDeviationBar` (`pid_panel_mod.py`) blev en kombinerad
tag+typ+avvikelse+ta bort-editor** — samma princip som
`EquipmentPlacementPopup` redan är för NYA objekt, nu även för
BEFINTLIGA. Ny Tag-fält (samma taggcompleter-mönster som övriga
popuper, live-commit på `editingFinished`), Typ-combo + "+"-knapp för
ny typ (samma som `EquipmentPlacementPopup`/`EquipmentTagPopup`), en
informativ dublett-varning (live via `textChanged`, ingen blockerande
sammanslagning eftersom detta är ett REDAN existerande, riktigt objekt —
inte en tom platshållare som riskerar bli en övergiven dubblett) och en
"Ta bort"-knapp med bekräftelsedialog. Två nya signaler,
`equipment_updated`/`equipment_deleted` (equipment_id), bubblas via
`PIDPanel` (`_on_equipment_bar_updated`/`_on_equipment_bar_deleted` —
uppdaterar canvasens overlay direkt, sedan vidare) till `MainWindow`
(`_on_equipment_changed_from_marker`) som gör samma tråd/scenario-
uppdatering `_on_equipment_edit_requested` redan gjorde för
högerklicksflödet.

**2. Högerklicksmenyn fick en "Ta bort"** direkt bredvid "Redigera
objekt" (`pid_graphics_view.py`s `_show_context_menu`), ny signal
`equipment_delete_requested` (samma payload — `equipment_markers.id` —
som `equipment_edit_requested`). `PIDPanel._on_equipment_delete_requested`
slår upp objektet, bekräftar (`QMessageBox.question`, samma
"Ta bort X?"-mönster som trädets egen `delete_selected()` redan
använder), anropar `db.delete_equipment_item()` (kaskaderar redan till
alla markörer på objektet, se dess egen docstring) och återanvänder
SAMMA `equipment_deleted`-signal/MainWindow-uppdatering som
vänsterklick-popupens knapp — ingen duplicerad refresh-logik mellan de
två borttagningsvägarna.

Nya regressionstester: `tests/test_pid_panel_mod.py`
(`EquipmentDeviationBarTests` — tag/typ-commit, dublett-hint, ta bort
bekräftad/avbruten; `EquipmentMarkerEditContextMenuTests` — "Ta bort"
finns/saknas i menyn, klick emitterar rätt signal) och
`tests/test_integration.py` (nya `EquipmentDeleteRequestedHandlerTests`,
`EquipmentBarUpdateAndDeleteBubbleTests` — hela vägen PIDPanel→
MainWindow→tree/scenario-refresh för båda borttagningsvägarna och
tag-redigering).

**Verifiering:** `test_smoke` + `test_pid_panel_mod` +
`test_pid_graphics_view` + `test_integration` (331 tester) samt full
14-filssvit, allt grönt.

## Slå ihop objekt-rad + avvikelse-rad till en platt rad i trädet (2026-08-25, samma dag, uppföljning)

**Beställning (planerad tillsammans via Plan Mode, inte direktimplementerad):**
Anton: "Jag vill planera en förändring tillsammans i hierarkin. Under
kategorin Avvikelse finns idag objekt-tag och sedan objekt-typ. Under
denna kategorin visas sedan avikelsetexten: Ventil felar stängd. Jag
vill att dessa två nivåer slås ihop till en. I trädet skall enbart
Objektag + avikelsetexten stå. Detta skall vara på en rad istället för
två rader som idag i trädet. Jag kommer även senare ersätta objekttyp
med rätt figur man kan klicka på för att få denna information."

**Tidigare beteende** (`tree_panel.py`, `_add_node_item`s Ledord/
Utrustning-uppbyggnad): en avvikelse kopplad till ett objekt visade
ALLTID två rader — en `LEDORD_T`-omslagsrad (`"⬡ N. {description}"`,
t.ex. "⬡ 1. Ventil felar stängd") med en barn-rad under (`"{tag},
{typ}"`, t.ex. "V-101, Ventil") som i det vanliga fallet redan
tekniskt var `DEV_T`/`CAUSE_T`-noden ("kaka på kaka"-kollaps från
2026-08-09) — bara etiketten visade fortfarande tag+typ istället för
avvikelsetext.

**Avstämt via `AskUserQuestion` innan implementation:** när flera
objekt delar exakt samma avvikelsetext (tidigare grupperade under en
delad numrerad rubrik, "16 avvikelser"-räkningen från 2026-08-13) valde
Anton **"Platta alltid ut, oavsett antal"** — en avsiktlig reversering
av den tidigare grupperingspreferensen, specifikt för det objekt-
kopplade fallet.

**Ny regel:** varje objekt-kopplad avvikelse (`equipment_id` satt) är
nu EN platt rad direkt under noden, oavsett hur många andra avvikelser
som råkar dela samma beskrivningstext — `LEDORD_T`-omslaget byggs inte
längre för dessa. Ny etikett: `f"  ⬡  {di}. {tag} — {description[:45]}"`
(em-dash, samma separator `scenario_panel.py`s egen tagg+typ-formatering
redan använder). Objekttyp tas bort helt från etikett-TEXTEN ("I trädet
skall ENBART Objektag + avikelsetexten stå") — kursiv font för "typ ej
satt" behålls dock som tyst visuell signal i väntan på den klickbara
figur/ikon Anton nämner som ett senare, separat steg. Löpande
numrering (`di`) fortsätter oförändrad över alla avvikelse-rader under
en nod ("16 avvikelser"-egenskapen bevarad, bara omfördelad till platta
rader).

`LEDORD_T` finns kvar, men bara för det sällsynta kvarvarande fallet:
2+ avvikelser UTAN objekt som råkar dela exakt samma beskrivningstext
under en nod — rörs inte, ingen del av dagens önskemål handlade om det.
`EQUIP_T` som rader-typ-vid-vila försvinner därmed helt i praktiken
(den existerade bara i den nu borttagna "samma objekt har 2+ avvikelser
med identisk text"-grenen, bekräftat via kodgenomgång att detta ALDRIG
uppstod genom appens normala `get_or_create_deviation`-flöde) —
`EQUIP_T`-konstanten och dess resolvers lämnas dock orörda.

**Drag-and-drop-verifiering:** `_deviation_item_at` (equipment-marker-
drop-på-trädrad) rördes inte alls — dess redan befintliga `DEV_T`/
`CAUSE_T`-hantering (byggd för "kaka på kaka"-fallet) tar automatiskt
hand om de nu alltid-platta raderna, bekräftat med tre regressionstester
i `tests/test_integration.py::EquipmentDropOnTreeDeviationTests`
(drop på en platt-slagen rad, DragMove-hover över samma, samt drop på
det kvarvarande `LEDORD_T`-fallet för delade ogrupperade avvikelser).

Nästan alla ~9 påverkade tester låg i
`tests/test_tree_panel.py::TreePanelEquipmentGroupingTests` — skrivna
om för den nya platta strukturen, det nya etikettformatet, och det
faktum att `EQUIP_T` inte längre går att nå (två dubbelklicks-tester
skrivna om från att tvinga fram ett konstgjort `EQUIP_T`-läge till att
använda det nu vanliga platta scenariot istället). Bekräftat att alla
omskrivna tester verkligen slår av mot koden innan denna ändring (8 av
8 misslyckas via `git stash`).

**Verifiering:** `test_smoke` + `test_tree_panel` + `test_integration`
(293 tester) samt full 14-filssvit, allt grönt.

## Rättar ihopslagningen: Avvikelse ("Lågt flöde") kvar intakt, Orsak = objekt-tag + orsaksbeskrivning (2026-08-25, samma dag, uppföljning)

**Beställning (planerad via Plan Mode):** föregående ändring samma dag
(commit `7fcc993`, "Slå ihop objekt-tagg + avvikelsetext...") slog ihop
FEL två nivåer. Anton rättade: "Ventil felar stängd" — exemplet han
gav ursprungligen — var ORSAKENS egen beskrivning
(`causes.description`, en specifik felmod), inte AVVIKELSENS guide-ord
(`deviations.description`, t.ex. "Lågt flöde"). Han förtydligade:

> "Jag vill att avvikelsenivån är kvar intakt, dvs lågt flöde. En nivå
> under lågt flöde skall Orsak ligga, orsak i trädet består av
> Objekttaggen + orsaksbeskrivningen (felar öppet, felar stängd etc).
> Exempel System -> nod -> avikelse -> Objekt-tag + orsaksbrevning."

**Bekräftat via `AskUserQuestion`:** när flera objekt (t.ex. V-101 och
V-102) delar exakt samma avvikelsetext ("Lågt flöde") delar de EN
gemensam avvikelse-rad — inte en var — med varsin Orsak-rad som syskon
under den.

**Konsekvens, långt mer än en etikettändring:** eftersom objekt-
identiteten nu visas på ORSAKS-nivån (`causes.equipment_id`/`comp_tag`,
oberoende av vilken specifik `deviations`-rad orsaken råkar hänga på)
behövs ingen separat "Utrustning"-nivå i trädet längre. Hela "kaka på
kaka"-mekaniken (2026-08-09/08-10 — objekt-rad som slås ihop med
avvikelse-raden, som i sin tur kan slås ihop med en trivial orsaks-rad)
blir överflödig och togs bort. `tree_panel.py`s `_add_node_item`s
Ledord/Utrustning-block (equipment_groups/ungrouped_devs-uppdelning,
"skip wrapper"-specialfall, "hide empty generic"-regel) ersattes av en
enda, enhetlig loop: en `DEV_T`-rad per beskrivning (ankrad på den
GENERISKA, alltid auto-seedade avvikelsen), med en ny `add_cause_item`
som samlar orsaker från ALLA `deviations`-rader som delar texten
(objekt-kopplade eller generiska) som direkta barn, etiketterade
`"{tag} — {beskrivning}"` (bara tagg om orsaken fortfarande är trivial,
bara beskrivning om ingen tagg finns).

**Bonusfix, hittad under omskrivningen:** orsaks-radens tagg
resolveras nu LIVE via `causes.equipment_id` → `equipment_catalog`
(samma mönster `scenario_panel.py`s egen `_cause_tag_display` redan
använder för ORS-taggremsan), inte den frusna `comp_tag`-kolumnen —
detta var faktiskt en redan existerande bugg (bekräftat: den gamla,
nu borttagna "kaka på kaka"-koden hade samma frusna-taggvisning-problem,
inte något min omskrivning införde) som gjorde att ett objekt omdöpt
via P&ID/Utrustningsregistret inte uppdaterade trädets rad förrän
något annat råkade trigga en fullständig ombyggnad.

**`EQUIP_T` och `LEDORD_T` är nu HELT odåtkomliga** genom `refresh()`
(tidigare bara sällsynta) — konstanterna och deras resolvers/
kontextmeny-spärr lämnas medvetet orörda (kostar inget). Två tester i
`tests/test_tree_panel.py` som tidigare konstruerade sitt `LEDORD_T`-
scenario via ett riktigt `refresh()`-anrop bygger nu sitt måltillstånd
direkt (ett manuellt konstruerat `QTreeWidgetItem`) istället.

**Verifiering:** `test_smoke` + `test_tree_panel` + `test_integration`
+ `test_scenario_panel` (391 tester) samt full 14-filssvit, allt grönt.
Bekräftat att samtliga 10 omskrivna/nya tester slår av mot koden innan
denna rättelse (`git stash`).

## Auto-collapse "avvikelser" verifierad redan korrekt efter dagens hierarki-rättelse (2026-08-25, samma dag, uppföljning)

Anton bad om att "avvikelser" ska bete sig mer likt "nodes"-kryssrutan:
klicka på "Lågt flöde" ska fälla ut den och fälla ihop t.ex. "Låg nivå".
Verifierat (skript mot `tree_panel.py`s aktuella kod, flera scenarier:
klick på avvikelse-raden direkt, klick på en orsaks-rad, båda
kryssrutorna på samtidigt, byte till en helt annan nod) — beteendet
fungerar redan exakt så, sedan den tidigare fixen samma dag ("Rättar
ihopslagningen"). Anton bekräftade att testet som föranledde önskemålet
sannolikt gjordes innan den fixen landade. Ingen kodändring behövdes;
låst fast med en ny regressionstest
(`test_deviations_toggle_behaves_like_nodes_toggle_between_sibling_avvikelser`,
`tests/test_tree_panel.py`) som använder exakt de guide-ord Anton själv
nämnde.

## Slå ihop objektbaren i Orsak-kolumnen (2026-08-25, samma dag, tredje ändringen)

Anton: "I hazop scenario består orsak av en objektbar, orsakstext och
en frekvens. Jag vill att du slår ihop tar bort objektbaren och
istället gör så tag id står utskrivet i fetstilt följt av
orsakstexten exempelvis 'V-101, Felar öpppen'. Objekttaggen ska vara
i bold och klickar man på denna så editerar man objekt tag alternativ
objekt typ. klickar man på 'felar öppen' ska man kunna editera
orsakstexten. Till höger om detta ska frekvens stå."

**Före:** ORS-cellen (`scenario_panel.py`, `_PidDelegate.paint()`) var
vertikalt uppdelad i en egen objekt-tagg-remsa (`_ORS_STRIP_H = 17px`,
egen bakgrundsfärg, separerad med en linje) högst upp, med
orsaksbeskrivningen som egen, ombruten text under den
(`_ORS_HEADER_H`-offset). En manuellt dragbar delare (`_cause_obj_w`,
persisterad i `app_config`) lät användaren justera taggzonens bredd.

**Efter:** remsan är helt borttagen. Taggen står nu INLINE, i fetstil,
direkt följt av orsakstexten på samma textflöde ("**V-101**, Felar
öppen") via samma `_draw_text_with_bold_tags`/`QTextLayout`-
infrastruktur som redan fetmarkerar dragna taggar i KON/SG-
beskrivningar. Tre nya delade hjälpmetoder på `ScenarioTablePanel`
(`_ors_tag_prefix`, `_ors_combined_text`, `_ors_tag_prefix_pixel_width`)
är ensam källa till sanning för vad som visas/mäts/klickas — använda
identiskt av alla tre radhöjdsberäkningarna, `paint()`,
`updateEditorGeometry()` och `eventFilter()`s klick-zon, enligt filens
egen etablerade "delad geometri"-regel. Klick på den fetstilta taggen
öppnar samma `CauseTagPopup` som tidigare (oförändrad); klick/
dubbelklick på resten startar samma inline-textredigering av
orsaksbeskrivningen som redan fanns. Frekvensen (redan en högerställd
"chip" sedan 2026-08-18) flyttades inte i sak — bara att "första
raden" nu börjar direkt vid cellens topp istället för efter remsan.
Den dragbara delaren (`_cause_obj_w`/`_drag_obj_w_*`, `app_config`-
nyckeln `cause_obj_w`, `_ors_tag_zone_width`) togs bort helt — en fast
zonbredd att justera finns inte längre när den fetstilta zonen alltid
är exakt så bred som taggen faktiskt renderas.

**Konstant-omdöpning:** `_ORS_STRIP_H` → `_ORS_FIRST_LINE_H` (samma
värde 17, nytt namn/betydelse: höjden på radens FÖRSTA textrad, där
tagg/frekvens/kommentar-zonerna bor — inte en fysisk remsa som inte
längre finns). `_ORS_HEADER_H` togs bort helt.

**Bugg hittad och fixad under omskrivningen, inte i planen:**
kommentar-pricken (`_ors_comment_dot_geometry`) flyttades avsiktligt
från "remsans mitt" till "första radens mitt" som en konsekvens av att
remsan försvann — men det flyttade den in på SAMMA rad/höjdband som
frekvens-chipen, som redan (sedan 2026-08-18, likelihood defaultar
till 1 för varje ny orsak) är aktiv på praktiskt taget varje rad. Utan
fix hade pricken och frekvenstexten kunnat överlappa visuellt, och en
regressionstest (`OrsCommentClickZoneTests.
test_clicking_near_the_dot_no_longer_fires_the_removed_clone_zone`)
hängde faktiskt i en riktig, omockad `FrequencyPickerPopup.exec()` i
testsviten tills detta upptäcktes. Fixat genom en ny konstant
`_ORS_DOT_RESERVE_W = 12` som ovillkorligen reserverar utrymme åt
pricken i `_ors_freq_zone_geometry`s beräkning — oavsett om just den
raden faktiskt har en kommentar, så att de två geometrierna aldrig
behöver komma överens dynamiskt om det (samma "en delad källa"-princip
som allt annat i denna klass).

**Verifiering:** `test_smoke` (11), `test_scenario_panel` (110),
`test_integration` (214) samt full 14-filssvit — alla gröna. Manuell
räckvidd inte körd i GUI:t denna session (headless testmiljö).

## Rekommendationshantering — delad katalog med återanvändning (2026-08-25)

Anton ville kunna återanvända tidigare rekommendationer istället för
att skriva samma text på nytt varje gång, och kunna redigera en delad
rekommendation utan att det tyst ändrar den för andra scenarion som
råkar återanvända samma text.

**Beslutat med Anton innan planering (AskUserQuestion):** länkpunkten
är **konsekvensen**, inte bokstavligen orsaken (`causes`) — matchar
dagens REK-kolumns spanning/keying (`consequence_id`) exakt, istället
för att skriva om hela den logiken för en mindre relevant vinst.
Ansvarig/deadline/status (dagens gamla `actions`-fält) **behålls, på
katalogposten** — delas alltså av alla konsekvenser som länkar till
samma rekommendation.

**Datamodell:** `actions`-tabellen (en rad per konsekvens, ingen
återanvändning) ersattes av en delad katalog `recommendations`
(`id, description, responsible, due_date, status` — `id` är själv det
unika, aldrig återanvända löpnumret, visat som `R-XXX` överallt) plus
en äkta many-to-many-länktabell `consequence_recommendations`. En
engångsmigrering (`Database._migrate_actions_to_recommendations()`)
kopierar en eventuell gammal `actions`-tabells rader in i katalogen +
skapar motsvarande länkar, sedan `DROP TABLE actions` — körs bara en
gång per databasfil (tabellen finns inte kvar att migrera igen efteråt),
samma mönster som `_drop_legacy_consequence_likelihood_column`.

**UI:** `RecommendationEditorDialog` (hazop.py, öppnas via REK-cellen,
oförändrad anropspunkt i `scenario_panel.py`) skrevs om helt — ett
fritextfält som är BÅDE "skriv en ny rekommendation" och "sök bland
befintliga" (live-filter, case-insensitive substring), plus en
kryssrutelista över hela studiens katalog. Att kryssa i/ur en rad
länkar/avlänkar direkt (`link_recommendation_to_consequence`/
`unlink_recommendation_from_consequence`) — ingen OK-knapp behövs,
samma "sparar sig själv"-mönster den gamla `ActionEditor` redan hade
(som togs bort helt, ingen annan kodplats refererade den). Att avkryssa
tar INTE bort katalogposten även om det var dess sista länk — texten
finns kvar för återanvändning senare.

**Redigeringskonflikt (del 2 av önskemålet):** ett ✎-knapp per rad
öppnar `_RecommendationDetailDialog` (ny, hazop.py) för EN
rekommendations fyra fält. Vid Spara: om
`recommendation_consequence_count(rec_id) > 1` visas en
Ja/Nej/Avbryt-fråga ("Denna rekommendation används av flera
konsekvenser (N st). Vill du uppdatera rekommendationen för
samtliga?", svenska knapptexter för att matcha appens övriga
dialogrutor snarare än Qt:s engelska standardtexter) innan något
sparas:
- **Ja:** `update_recommendation(rec_id, ...)` — samma id, syns direkt
  hos alla konsekvenser som redan länkar till den.
- **Nej:** en NY katalograd skapas med de redigerade fälten
  (`add_recommendation`), den aktuella konsekvensen avlänkas från den
  gamla och länkas till den nya — övriga konsekvenser fortsätter peka
  på originalet, helt oförändrat.
- **Avbryt:** ingen databasändring alls.
Vid `count <= 1` sparas direkt utan att fråga.

**Numrering i REK-cellen** (`ScenarioTablePanel._recommendation_summary`)
bytte från lokal `enumerate(acts, 1)` (nollställd per cell, "1. 2. 3...")
till rekommendationens egna globala `id`, formaterat `R-XXX` — detta
är kärnan i att samma återanvända rekommendation visar SAMMA nummer
oavsett vilken konsekvens man tittar på, inte bara en kosmetisk ändring.

**Övriga följdändringar:** `stats()`s dict-nyckel `open_actions` →
`open_recommendations` (+ `settings_panels.py`s label "Öppna åtgärder"
→ "Öppna rekommendationer"); Excel-/PDF-export och
Åtgärdsrapport-PDF-exporten (`hazop.py`) pekar om mot
`recommendations_for_consequence`/`all_data()` men konsumerar exakt
samma dict-form som förut (`description`/`responsible`/`due_date`/
`status`) — ingen ändring behövdes i själva exportlogiken.

**Verifiering:** `test_smoke`, hela 14-filssviten, samt kontrollgrupp
(`git stash` på de fyra ändrade produktionsfilerna — 25 av 27 nya/
omskrivna tester slog verkligen av mot den gamla `actions`-baserade
koden innan denna ändring).

## Standardorsak-popup vid redigering av Orsak-cellen (2026-08-25)

Anton: "När jag vill editera orsakstexten och står i editerarläget
vill jag även att det dyker upp en liten popupruta (som inte täcker
cellen). I denna popuprutan skall jag kunna välja bland de
'standard'-orsaker som finns för objektypen och avikelsen. Denna
popupruta behöver bara innehålla detta samt möjlighet att editera
frekvens genom att klicka på frekvensen. Anpassa popuprutan efter
antalet standardorsaker som dyker upp."

**Funktion:** så fort man går in i redigeringsläge på en Orsak-cell
dyker `StandardCauseSuggestPopup` (`scenario_panel.py`) upp under
raden — en knapp per matchande standardorsak (klick sparar texten och
avslutar redigeringen direkt, bekräftat med Anton via
`AskUserQuestion`) plus en klickbar frekvensrad som återanvänder den
redan befintliga `FrequencyPickerPopup`/`_on_ors_frequency_picked`.
Storleken anpassas automatiskt (`adjustSize()`) efter hur många
standardorsaker som listas. Standardorsakerna hämtas via en ny delad
hjälpmetod `ScenarioTablePanel._ors_standard_causes_for_row(row)` —
samma fallback-kedja (objekt-hierarki → comp_type+avvikelsetext →
comp_type utan avvikelsefilter) som `CauseObjectPopup._rebuild_causes`
(tree_panel.py) redan använde, nu delad med den befintliga
tangentbords-completern (`_attach_cause_completer`, omskriven till att
anropa samma hjälpmetod istället för att duplicera upplösningslogiken).

**Betydande, oväntad felsökningsresa** (värt att komma ihåg om
liknande "extra hjälpruta bredvid en aktiv celleditor"-behov dyker upp
igen): en NAIV implementation — en riktig separat top-level-fönster-
popup (`Qt.WindowType.Tool | FramelessWindowHint`), även kombinerad med
`WA_ShowWithoutActivating` och `Qt.FocusPolicy.NoFocus` på varje
underwidget — visade sig, bekräftat empiriskt (inte via minne/gissning),
tysta stänga den aktiva celleditorn så fort popupen visades. Orsak:
`QAbstractItemDelegate`s inbyggda FocusOut-hantering (samma mekanism
som låter en `QCompleter`s egen popup samexistera med en editor, via
`completer.setWidget(editor)`) tolkar "fokus gick till ingenting i
editorns egen anfader-kedja" som "användaren är klar med redigeringen"
och committar+stänger den automatiskt — och att visa ETT NYTT
OS-fönster alls (oavsett aktiverings-flaggor) räckte för att trigga en
sådan momentan fokusförlust på plattformen som testades mot. Lösningen
blev att göra popupen till en VANLIG, icke-top-level barn-widget av
panelens eget toppfönster (`panel.window()`) istället — då skapas inget
nytt OS-fönster alls, och den händelsen inträffar aldrig. Positionering
sker därför i toppfönstrets EGNA lokala koordinater (`mapFromGlobal`),
klippt mot dess klientyta istället för mot skärmens `availableGeometry`.

**Andra bekräftade detaljer, inte bara antaganden:** `closeEditor`-
signalen döljer (`hide()`) editorn OMEDELBART/synkront, men FÖRSTÖR
(`destroyed`) den bara efter en odefinierad fördröjning (`deleteLater`-
mönster) — popupens auto-stängning lyssnar därför på BÅDA: ett
`eventFilter` som fångar `QEvent.Type.Hide` på editorn (omedelbart,
pålitligt) plus `editor.destroyed` som extra skyddsnät. Att manuellt
`emit`a `commitData`/`closeEditor` på fel delegate-instans
(`self._pid_delegate` istället för `self._delegate`) ger en tyst Qt-
runtime-varning ("editor that does not belong to this view") utan att
krascha — `_pick`/`_edit_frequency` använder `self._panel._delegate`,
samma instans den redan existerande Enter-tangent-hanteringen i
`eventFilter()` använder, verifierat att den faktiskt fungerar.
Klick på frekvensraden committar eventuell oskriven text FÖRST (samma
`commitData`/`closeEditor`-mönster) innan frekvensen ändras — annars
hade `_on_ors_frequency_picked`s `_schedule_rebuild()` (som proaktivt
rensar fokus från en aktiv celleditor innan raderna byggs om) kunnat
tyst tappa obekräftad text.

**Verifiering:** `test_smoke`, hela `test_scenario_panel`-filen (140
tester), samt kontrollgrupp (`git stash` på `scenario_panel.py` — 14
av 15 nya tester slog verkligen av mot koden innan denna ändring, den
15:e gav en hård `ImportError` eftersom hela popup-klassen inte fanns).

## Riv bort Avvikelse-cellens frekvenspopup (2026-08-26)

Anton: "det finns någon konstig funktion om jag klickar på en
avvikelse i hazop scenario som ger en pop-upryta utifrån detta med
någon form av frekvens som jag inte förstår ... den funktion ska
rivas." Bekräftat via en genererad skärmdump av den faktiska popupen
innan något togs bort.

**Funktionen som togs bort:** `DeviationPickerPopup` (tree_panel.py,
tillagd 2026-08-14) — öppnades vid klick på Avvikelse-cellen (`_C_DEV`)
i HAZOP scenario-tabellen för en rad med en riktig orsak, och visade en
knappgrid med nodens övriga avvikelser, var och en annoterad med en
härledd "förvald frekvens" (`Database.default_frequency_for_deviation`,
lägsta `standard_causes.frequency` bland matchande
`standard_deviations`), plus ett fritextfält för en ny/egen avvikelse.

**Borttaget helt:** `DeviationPickerPopup`-klassen (tree_panel.py),
dess import i `scenario_panel.py`/`hazop.py`, klick-hanteringen i
`scenario_panel.py`s `eventFilter()` (Avvikelse-cellen är nu en vanlig,
oklickbar cell igen), handlern `_on_deviation_picked`, samt
`Database.default_frequency_for_deviation` (helt oanvänd efteråt —
verifierat via grep, ingen annan kodplats läste den). Motsvarande
tester (`AvvikelseCellPickerTests` i `tests/test_integration.py`,
`DeviationDefaultFrequencyTests` i `tests/test_database.py`) togs bort
med.

**Uttryckligen INTE rört** (verifierat innan borttagning, en genuint
separat funktion som råkar dela samma bakomliggande DB-anrop):
"↕ Flytta till annan avvikelse…"-kontextmenyalternativet i trädet
(`_move_cause_dialog`) och `always_show_deviation_column()` (används
av Worksheet-vyn för att alltid visa Avvikelse-kolumnen) — båda
oförändrade. `Database.move_cause_to_deviation`/`get_or_create_deviation`
behölls likaså, eftersom `_move_cause_dialog` fortfarande anropar dem.

**Verifiering:** `test_smoke`, hela 14-filssviten (910 tester), grep
över hela kodbasen efter kvarvarande referenser till de borttagna
symbolerna — inga funna.

## "Fyll bredd" som standard vid uppstart (2026-08-26)

Anton: "Du kan ordna så kanppen fyll bredd är ikryssad per default när
programmet startar." Knappen "↔ Fyll bredd" i HAZOP scenario-tabellen
är sedan tidigare en engångsknapp (inte en kryssruta — se
`_fill_width_once`s egen docstring om varför den gamla, låsande
kryssrutan togs bort 2026-08-10), som fördelar om Orsak/Konsekvens/
Barriärer-kolumnerna jämnt över den lediga bredden.

**Ändring:** en helt ny studie (ingen sparad `scenario_col_widths` i
`app_config` än) kör nu automatiskt samma logik en gång vid uppstart —
via `QTimer.singleShot(0, ...)` (tabellen har ingen riktig
viewport-bredd vid själva konstruktionen, innan widgeten lagts in i ett
visat fönster). Så fort användaren ändrat NÅGON kolumnbredd (drag eller
programmatiskt) sparas riktiga bredder via `_on_column_resized`, och
denna gren körs aldrig igen för den studien.

**Bugg hittad och fixad under implementationen:** den första versionen
körde den fördröjda auto-fyllningen ovillkorligt baserat ENDAST på om
sparade bredder fanns vid KONSTRUKTIONSTILLFÄLLET — om något (ett
riktigt test, eller i teorin ett annat kodställe) satte en kolumnbredd
programmatiskt EFTER konstruktion men INNAN händelseloopen hann köra
den fördröjda callbacken, skrevs den bredden tyst över ett ögonblick
senare. Bekräftat genom att en befintlig test
(`OrsInlineTagPrefixTests.test_tag_click_zone_matches_the_actual_rendered_prefix_width`,
som sätter ORS-bredden till 400px direkt efter konstruktion) plötsligt
slog av. Fixat med en `_col_widths_user_set`-flagga som
`_on_column_resized` sätter omedelbart vid VILKEN SOM HELST
breddändring (drag eller programmatisk) — den fördröjda auto-
fyllningen kollar flaggan precis innan den kör och hoppar över sig
själv om något redan hunnit sätta en bredd.

**Verifiering:** `test_smoke`, hela `test_scenario_panel`-filen (138
tester, inklusive två nya: en som bekräftar att en helt ny studie
fyller bredden automatiskt, en som bekräftar att en studie med sparade
bredder INTE skrivs över), samt kontrollgrupp (`git stash` — det nya
testet för auto-fyllning slog verkligen av mot koden innan denna
ändring), samt full 14-filssvit.

## Crash-genomgång: 27 nya krascher sedan 2026-08-12 triagerade, två levande buggar fixade (2026-08-26)

Sessionsstart: startup-checklistan (se CLAUDE.md) hittade 27 nya filer i
`crashes/` sedan senaste genomgången (2026-08-12). Triage av alla 27:
9 redan bekräftat fixade i nuvarande kod (modulsplitten 2026-08-17/18,
`_participant_matrix_panel`-db-swappen, `clear_equipment_catalog`s
FK-nollning), 10 var dev/testskript-artefakter (traceback-toppen är
`<string>`/`<stdin>`, dvs ett fristående `python -c`/testskript, inte den
körande appen — bl.a. båda `UnicodeEncodeError`-emoji-kraschen: skriptet
anropade aldrig `_configure_utf8_console_output()`, som redan fixar den
riktiga apploggningsvägen sedan 2026-08-12). 2 var genuint fortfarande
levande buggar, båda fixade nu:

**1. "Skriv ut scenariotabell" kraschade varje gång (crash_20260825_135308_AttributeError.json).**
`MainWindow._print_scenario_table()` (hazop.py) byggde sin `QPrinter` med
PyQt5-stilens enum-access: `QPrinter.PageSize.A4` och
`QPrinter.Orientation.Landscape` — PyQt6 tog bort båda från `QPrinter`
(sidstorlek/orientering flyttade till `QPageSize`/`QPageLayout`). Fixat
till `printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))` och
`printer.setPageOrientation(QPageLayout.Orientation.Landscape)`. Att fixa
detta avslöjade en ANDRA, ännu inte rapporterad krasch ett steg längre in
i samma metod: `preview.paintRequested.connect(doc.print_)` —
`QTextDocument.print_` (PyQt5-namnet, understreck eftersom `print` är ett
Python-nyckelord) döptes om till bara `doc.print` i PyQt6. Båda fixade
tillsammans eftersom den andra aldrig gick att nå förrän den första var
löst. Ny test: `PrintScenarioTableTests` i `tests/test_hazop.py`.

**2. "💾 Spara som mall…" i "Hitta liknande symbol" kunde fortfarande
krascha på tomt referensurval (crash_20260815_175028_ValueError.json).**
Samma bugg-klass som redan fixades 2026-08-15 för `_restart_scan`
(`SimilarSymbolSearchDialogTests`) — att exkludera ALLA primitiv i
referenscanvasen lämnar en tom `index_group`, som kraschar
`symbol_geometry.cluster_features()` (`min()` på en tom lista). Den
tidigare fixen skyddade bara scan-omstarten; `_save_as_template()`
(pid_viewer.py) hade samma oskyddade anrop till `similarity_features()`
kvar — om en användare exkluderade allt och sedan klickade "Spara som
mall" innan skanningen hunnit köras (eller trots meddelandet) small den
fortfarande. Fixat med samma vakt-mönster: visar en varningsruta istället
för att bygga features på en tom grupp. Ny test:
`test_save_as_template_shows_message_instead_of_crashing_when_all_segments_excluded`.

**Verifiering:** `test_smoke`, hela `test_hazop.py` (nya
`PrintScenarioTableTests`) och hela `test_pid_viewer.py` (nytt fall i
`SimilarSymbolSearchDialogTests`) — 134 tester, alla gröna.

## Excel-export av standardavvikelser, grupperade per objekttyp (2026-08-26)

Anton: "Skapa en Excel-fil med programmets samtliga standardavvikelser
grupperade per objekttyp. Filen ska vara enkel att redigera manuellt och
senare kunna läsas in igen för att uppdatera programmets standarddata."

**Ny knapp** "↑ Exportera Excel" i Inställningar → Standardorsaker
(`StandardCausesSettingsPanel._export_library_excel()`, standard_causes_
panel.py), bredvid den befintliga JSON-export/import-knappraden ("Feature
16") — samma standard_causes/standard_deviations/standard_objects-data,
men i ett annat, till-hands-redigerbart format istället för JSON.

**Format (medvetet valt för att gå att läsa in igen senare):** en helt
platt tabell, EN rad per standard_causes-post, INGA sammanslagna celler —
kolumner Objekttyp | Avvikelse | Orsak | Frekvens (/år). Grupperingen "per
objekttyp" görs genom att sortera på `standard_objects`s egen sort_order
och upprepa objekttypens namn på varje rad (inte via en rubrikrad eller
sammanslagna celler) — en framtida importfunktion kan då matcha rader
enbart på textidentiteten (Objekttyp, Avvikelse, Orsak), exakt samma
matchningsprincip den befintliga JSON-importen (`_import_library`) redan
använder (ingen dold id-kolumn behövdes). Objekttyper utan en enda orsak
än utesluts helt (inget att redigera där). Alternerande radfärg per
objekttyp-grupp ger visuell gruppering utan att offra platt struktur.
En andra flik "Läs mig" förklarar kolumnerna och påminner om att
Objekttyp-stavningen måste matcha appens egen lista exakt för att en
framtida import ska kunna matcha den.

**Avsiktlig avgränsning:** bara export byggd denna gång — användarens
formulering ("ska... senare kunna läsas in igen") beskriver ett krav på
FILFORMATET (måste vara re-importvänligt), inte en beställning av
importfunktionen själv än. Filformatet är dock redan förberett för det
(ingen omdesign ska behövas när import-knappen byggs).

**Verifiering:** ny testklass `StandardCausesExcelExportTests` (4 tester)
i tests/test_settings_panels.py — bekräftar kolumnrubriker, radantal mot
riktiga `standard_causes`-data, att inga celler är sammanslagna, att
grupperingen per objekttyp aldrig bryts (samma objekttyp dyker aldrig upp
i två separata block), att en känd riktig frökt orsak ("Pump stopp" under
Pump/Lågt flöde, 0.02/år) hamnar rätt, att tomma objekttyper utesluts, och
att en avbruten spardialog inte skriver någon fil. Även manuellt körd mot
den riktiga `hazop_project.db` (175 orsaker, 20 objekttyper) för att
bekräfta att UTF-8/å-ä-ö-text och grupperingen ser korrekta ut i en
verklig .xlsx öppnad med openpyxl. `test_smoke` + hela
`test_settings_panels.py` (94 tester) gröna.

## Riv Smart Layout ur den aktiva applikationen, arkivera implementationen (2026-08-26)

Anton: "Riv den nuvarande Smart Layout-funktionen från den aktiva
applikationen. Arkivera den gamla implementationen på lämplig plats i
projektet så att koden finns kvar men inte används."

**Borttaget från PIDPanel (pid_panel_mod.py):** knappen "Smart layout" i
P&ID-verktygsraden, dess tre init-attribut (`_smart_layout_prev`,
`_analyzer_thread`, `_analyzer_progress_dlg`) och de tre metoderna
`_run_smart_layout`/`_on_smart_layout_done`/`_undo_smart_layout` (samt
den nu oanvända `ConnectorAnalyzer`-importen). Kvar och oförändrat: hela
den aktiva vägen som ritar upp REDAN SPARADE kopplingar/bågar när ett
P&ID öppnas (`PIDPanel._load_overlays`, `add_sheet_conn_arc`) — den
läser bara det som redan finns i DB:n och beror inte på hur datan en
gång skapades.

**Flyttat till `archive/smart_layout.py`:** hela `ConnectorAnalyzer`-
klassen och `_propose_layout()`-funktionen (den Sugiyama-inspirerade
lagerbaserade layouten), ordagrant, ur pid_viewer.py. Modulen importerar
`_DIALECTS`/`_detect_dialect`/`_sheet_ref_variants`/`_RE_SHEET_NUM`
TILLBAKA från pid_viewer.py istället för att duplicera dem — de stannade
kvar där eftersom `_DIALECTS['classic']`s egen dict-literal refererar
`_RE_SHEET_NUM` direkt och den aktiva bågritningskoden ovan fortfarande
använder `_sheet_ref_variants`/`_detect_dialect`. Bara regex/dictar som
en repo-bred grep bekräftade sakna alla andra anropsställen
(`_RE_RDS_SHEET`, `_RE_ITS_CONN`, `_RE_GRYAAB_CONN`, `_RE_TO_FROM`,
`_RE_DIR_KW`, `_RE_LINE_ID`, `_MEDIA_PATTERNS`, `_MEDIA_WEIGHTS`) flyttade
ner tillsammans med koden som faktiskt använder dem.

**Även flyttat** (samma dag, samma orsak): de två fristående
dev-testverktygen `analyze_refs.py`/`render_layout.py` (byggda helt kring
`ConnectorAnalyzer`/`_propose_layout` mot `P&ID ref/`-biblioteket) till
`archive/dev_scripts/`, med uppdaterade importer
(`from archive.smart_layout import ConnectorAnalyzer, _propose_layout`
istället för `from pid_viewer import ...`) och en `sys.path`-fix eftersom
de nu ligger två katalognivåer under hazop/ istället för en. Samt det
enda testet som konstruerade `ConnectorAnalyzer` direkt
(`ConnectorAnalyzerHangTests` i tests/test_pid_viewer.py) till
`archive/tests/test_smart_layout_archived.py` — körs separat
(`python -m unittest archive.tests.test_smart_layout_archived`), inte
del av den aktiva sviten.

**Medveten avvägning — SmartPipeTracer rördes INTE:** namnet "Smart" är
delat mellan två helt orelaterade funktioner i den här kodbasen —
`SmartPipeTracer` (pid_viewer.py, används av `pid_graphics_view.py` för
att spåra en rörledning mellan två klickade punkter när man ritar
markup/kopplingar) är INTE samma sak som "Smart layout"
(bladpositionering) och är fortfarande aktiv. Bekräftat via grep innan
något togs bort — annars hade fel funktion riskerat rivas.

**Verifiering:** `python -m py_compile` på alla ändrade/nya filer, samt
en faktisk import av `archive.smart_layout` och båda flyttade
dev-skripten (inte bara py_compile) för att bekräfta att deras nya
importer verkligen löser ut. Det arkiverade testet passerar isolerat.
Hela 14-filssviten (922 tester) + `test_smoke` gröna efter borttagningen.

## Objekttypslistan på P&ID källas nu från Standardobjekt (2026-08-26)

Anton: "Objektlistan som används när objekt definieras på P&ID ska vara
samma som programmets standardobjektlista. Ändra P&ID-listan så att den
använder standardlistan som källa."

`ui_helpers._equipment_type_options(db)` — den delade hjälpfunktionen
bakom typ-comboboxen i `EquipmentPlacementPopup`/`EquipmentDeviationBar`
(pid_panel_mod.py, dvs. "definiera objekt på P&ID"), `EquipmentPanel`,
`TreePanel` och `SafeguardObjectPopup`s typfilter — byggde tidigare listan
som `_EQ_TYPE_ITEMS` (COMPONENT_TYPES, ~90 ISA-liknande prefixnamn) UNION
`standard_objects`, dvs. de två listorna "pratade med varandra" men
standardlistan var aldrig själva källan. Skrivet om: `standard_objects`
(Inställningar → Standardobjekt) är nu bas-listan; endast ett
`equipment_type`-värde som redan används i katalogen men INTE är ett
standardobjekt läggs fortfarande på i slutet (så gammal/anpassad data
inte tystnar ur listan). `_EQ_TYPE_ITEMS`-konstanten och dess enda
återstående direktanvändning (Utrustningsregistrets tabellcell-delegat
för inline-redigering av typ, equipment_panel.py:1205) lämnades
medvetet orörda — avsiktligt avgränsat till den delade funktionen som
faktiskt gäller "definiera objekt", inte hela appens typ-taxonomi.

**Verifiering:** ny testklass `EquipmentTypeOptionsSourcedFromStandardObjectsTests`
(4 tester) i tests/test_ui_helpers.py — bekräftar exakt listinnehåll,
att en gammal ISA-stilsträng ("Ventil") som inte är ett standardobjekt
inte längre dyker upp, att en genuint använd men icke-standard typ
fortfarande läggs till sist, och att listan speglar en live-ändring av
standard_objects (inte en cachead kopia). `test_smoke` + `test_ui_helpers`
+ `test_pid_panel_mod` + `test_equipment_panel` + `test_tree_panel` +
`test_scenario_panel` + `test_settings_panels` (403 tester) gröna —
inklusive två befintliga tester som redan förlitade sig på ett katalog-
objekts EGET, redan satta legacy-`equipment_type`-värde ("Ventil"/
"Behållare") fortfarande syns i dess combobox, precis det den nya
extra-listan är till för.

## Behåll HAZOP-vyn när ett objekt tas bort från P&ID (2026-08-26)

Anton: "När ett objekt tas bort från P&ID ska HAZOP-trädet uppdateras utan
att öppna noder stängs. Behåll samma position och markerat objekt om
möjligt. HAZOP Scenario-vyn ska inte hoppa eller bli blank."

Båda hälfterna spårades till samma handler, `MainWindow.
_on_equipment_changed_from_marker` (hazop.py), som körs för BÅDE
`equipment_updated` och `equipment_deleted` (samma signal-mottagare,
avsiktligt — se dess egen docstring).

**Trädet tappade sin markering:** `refresh()` bevarar redan ALLA
expanderade noder ovillkorligt (dess egna `expanded`-set byggs innan
`self.tree.clear()`, oberoende av vad man anropar med) — men återmarkerar
bara en rad om man talar om VILKEN via `select_type`/`select_id`.
Handlern anropade `self.tree_panel.refresh()` helt utan argument, så
trädet slutade alltid utan någon markerad rad efter "Ta bort", även för
ett objekt helt orelaterat till det borttagna. Fix: läs
`self.tree_panel._current()` INNAN ombyggnaden och skicka det vidare —
`refresh(cur_type, cur_id)`. Om just den raden själv är det som
försvann matchar den förstås inte längre (target=None, samma
ofarliga fallback som redan fanns) men allt annat behåller sin markering.

**HAZOP Scenario kunde bli blank:** `ScenarioTablePanel.load_equipment()`
(körs när man klickar ett objekts P&ID-markör) filtrerar rader via ett
tagg/typ-uppslag som läses LIVE från `equipment_catalog` varje ombyggnad
(`_causes_for_equipment`/`causes_for_equipment`). Om just DET objektet
sedan raderas matchar uppslaget noll rader — tabellen byggdes tyst om
till en tom vy istället för att falla tillbaka på något. Fix: handlern
kollar nu om scenariopanelens AKTIVA filter (`get_equipment_filter()`)
pekar på just det borttagna id:t OCH att objektet verkligen är borta
(`db.get_equipment_by_id(...) is None` — skiljer delete från en vanlig
tag/typ-redigering, som ska lämna ett aktivt filter helt orört) — i så
fall `load_all()` istället för `schedule_rebuild()`.

**Verifiering:** tre nya tester i tests/test_integration.py
(`EquipmentBarUpdateAndDeleteBubbleTests`) — bekräftat att båda de nya
testerna verkligen FALLERAR mot koden innan fixen (kontrollgrupp via
`git stash` på hazop.py) och passerar efter. Ett tredje test bekräftar
att en redigering av ett ANNAT objekt inte rubbar ett redan aktivt
filter. `test_smoke` + `test_integration` + `test_hazop` +
`test_pid_panel_mod` + `test_scenario_panel` + `test_tree_panel`
(515 tester) gröna.

## Återanvänd tidigare konsekvenser — autocomplete i HAZOP Scenario (2026-08-26)

Anton: "Spara varje konsekvens som skrivs i HAZOP Scenario i en databas.
Vid redigering ska en rullgardinslista visa tidigare konsekvenser.
Filtrera listan direkt när användaren skriver, case-insensitive, baserat
på att texten börjar med det inskrivna värdet."

**Ny tabell** `consequence_history (id, description TEXT UNIQUE)` —
avsiktligt en helt annan mekanism än `standard_causes`/`standard_deviations`
(det kurerade biblioteket i Inställningar): den här växer TYST av sig
själv från vad användaren faktiskt skriver, ingen admin-vy, ingen
kategorisering. `Database.add_consequence_history(desc)` (INSERT OR
IGNORE — redan sedd text är en no-op, inte ett fel) anropas från
`_on_cell_changed_inner`s `'consequence'`-gren, direkt efter
`update_consequence()` — samma commit-punkt som redan sparar KON-cellens
text till DB. `Database.consequence_history()` returnerar listan
alfabetiskt (COLLATE NOCASE).

**Completer:** `_PidDelegate.createEditor()` fick en ny gren för
`_C_KON` (samma mönster som ORS-kolumnens `_attach_cause_completer`,
men medvetet EN annan matchningsprincip): `_attach_consequence_completer`
bygger en `QCompleter` med `Qt.MatchFlag.MatchStartsWith` (prefix, inte
"innehåller" som ORS-completerns) + `CaseSensitivity.CaseInsensitive`,
källad direkt från `db.consequence_history()`. Ingen completer alls
sätts om historiken är tom (en QCompleter med noll poster är bara
brus).

**Verifiering:** `ConsequenceHistoryTests` (4 tester, tests/test_database.py)
+ `ConsequenceHistoryAutocompleteTests` (4 tester, tests/test_scenario_panel.py)
— sparning vid cellredigering, tom text smutsar inte ner historiken,
completerns filterMode/caseSensitivity/innehåll, samt att ingen
completer sätts på en tom historik. `test_smoke` + `test_database` +
`test_scenario_panel` (224 tester) gröna (en isolerad, icke-relaterad
flakighet i `BackupSystemTests.test_write_backup_creates_file_with_same_data`
observerades en gång i en kombinerad körning men reproducerades INTE i
upprepade körningar av samma kommando, varken med eller utan denna
ändring — miljöflaknighet, inte en regression; se även den redan
dokumenterade "kan hänga i EN GUI-skapande test"-posten under Kända
begränsningar).

## Flytta HAZOP-popups ovanför sitt fält (2026-08-26)

Anton: "Alla mindre popup-rutor och dropdowns i HAZOP Scenario ska öppnas
ovanför sitt fält istället för nedanför."

Genomgång av samtliga manuellt positionerade popups i scenario_panel.py
(grep på `.move(`/`bottomLeft()`/`topLeft()`) hittade nio ställen — alla
föredrog NEDANFÖR sin cell/klickpunkt (några med fallback uppåt om det
inte fick plats, några helt utan fallback alls). Alla nio vändes till
att föredra OVANFÖR, med nedanför bara som reservval när det uppåt inte
finns plats på skärmen (samma `screen.top()`-koll som redan fanns i
`RiskMatrixPopup`, det EN popup som redan gjorde rätt — se
"Stäng riskmatris vid klick utanför" ovan):

- `_show_standard_cause_popup` (ORS-editorns standardorsak-popup) — egen
  fönster-relativ clamping (inte skärm-global, se dess docstring om
  varför), bara riktningen vänd.
- `_pos_near_cons_row` — DELAD av konsekvenskedjeguiden, rekommendations-
  editorn OCH `MainWindow.position_near_row` (hazop.py) — en fix täcker
  alla tre.
- `_show_cat_sg_popup` (safeguard-val för kategorirad).
- RRF-popupen (`SgRRFCategoryPopup`/`RRFPopup`).
- `_show_sg_object_popup_at` (🏷-ikonen på safeguard-celler).
- `_show_cause_obj_popup`/CauseTagPopup (klick på ORS-taggzonen).
- `_open_comment_popup` (💬 orsakskommentar) — saknade helt en ovanför-
  gren tidigare, inte bara fel prioritetsordning.
- `ConsCategoryMatrixPopup` (📊-badgen i KON-cellen) — samma sak, saknade
  ovanför-gren helt.
- `_quick_add_cause`s CauseObjectPopup.

**Medvetet OFÖRÄNDRAT:** native `QMenu.exec()`-kontextmenyer (t.ex.
`_show_quick_add`) — Qt hanterar redan skärmkant-flip själv för dessa,
och användarens formulering ("popup-rutor och dropdowns") syftar på
appens egna, handbyggda popups, inte OS-konventionsenliga kontextmenyer.

**Verifiering:** ny testklass `PopupsPreferOpeningAboveTheirFieldTests`
(4 tester) i tests/test_scenario_panel.py — täcker den delade
`_pos_near_cons_row`-hjälparen (både "får plats ovanför" och "faller
tillbaka nedanför"-grenarna) samt två representativa exempel på det
klick-punkt-ankrade mönstret (`_open_comment_popup`,
`_show_cat_sg_popup`), via en `popup.move()`-mock för att fånga
koordinaterna utan att behöva visa/köra popupen på riktigt. Kontrollerat
att 3 av 4 nya tester verkligen FALLERAR mot koden innan denna ändring
(`git stash` på scenario_panel.py) — det fjärde (nedanför-fallback) var
redan sant förut också, vilket är korrekt (det testar bara att
reservvägen fortfarande fungerar rimligt, inte att den ändrats).
`test_smoke` + `test_scenario_panel` (151 tester) gröna.

## Gör om safeguard-valet — riven typfiltrering, numerisk sortering (2026-08-26)

Anton: "Riv den nuvarande safeguard-funktionen markerad med emoji. När
safeguard redigeras i HAZOP Scenario ska en sökbar rullgardinslista visa
alla taggar/objekt definierade på P&ID, sorterade numeriskt. Sökningen
ska matcha var som helst i taggen, t.ex. PI123 ska visa både O1-PI123
och O2-PI123."

**Vad "emoji-funktionen" var:** 🏷-ikonen längst till vänster i SG-cellen
öppnade `SafeguardObjectPopup` (2026-08-19) — redan en redigerbar,
sökbar combobox (QCompleter, `MatchContains` — "var som helst i taggen"
var alltså redan uppfyllt) men med ett ⚙-kugghjul som öppnade
`_SgObjectTypeFilterDialog`, en kryssrutelista för att BEGRÄNSA vilka
`equipment_type`-värden som visades. Användarens "alla taggar/objekt"
(inte en filtrerad delmängd) och "riv" pekade på just den delen.

**Rivet helt:** `_SgObjectTypeFilterDialog`-klassen, `_SG_OBJECT_TYPE_
FILTER_KEY`, `SafeguardObjectPopup._allowed_types()`/`_open_type_filter()`
och kugghjulsknappen. `ui_helpers._equipment_tags_for_types(db)` tappade
sin `types`-filterparameter helt (död kod efter rivningen — inget annat
anropsställe fanns).

**Nytt:** `ui_helpers._natural_sort_key()` — delar en sträng i växlande
text/sifferdelar (siffror jämförs som int, text som gemener) så
`sorted(tags, key=_natural_sort_key)` ger "O2-PI123" före "O10-PI123"
istället för efter (vanlig strängsortering: '1' < '2' gör "O10" < "O2").
`_equipment_tags_for_types()` sorterar nu med denna nyckel istället för
`ORDER BY tag` i SQL.

**Verifiering:** `NaturalSortKeyTests` (3 tester, tests/test_ui_helpers.py)
för sorteringsfunktionen isolerat. `SafeguardObjectPickerTests` i
tests/test_scenario_panel.py fick tre nya tester (alla taggar utan
filter, numerisk sortering, sökning matchar suffix — det exakta PI123-
exemplet från begäran) och tappade de fyra som testade det borttagna
kugghjuls-filtret. `test_smoke` + hela 14-filssviten (943 tester) gröna.

## Redigera rekommendationer direkt i HAZOP Scenario (2026-08-26)

Anton: "Ta bort den separata popupen för redigering av rekommendationer.
Rekommendationstexten ska kunna redigeras direkt i HAZOP Scenario. Extra
information kan visas i en liten popup ovanför. Gör det även möjligt att
snabbt skapa en ny rekommendation med Enter."

**Rivet:** `RecommendationEditorDialog` (hazop.py) — den modala dialogen
som REK-cellen tidigare alltid öppnade vid klick, med egen `_input`/
`_table`/skapa-ny/redigera-UI. `scenario_panel._open_recommendation_editor`
borttagen med den.

**Nytt beteende:** REK-cellen är nu alltid direkt redigerbar (samma
"första klick väljer, klick på redan-aktuell cell startar inline-
redigering"-konvention som ORS/KON/SG, `_try_start_edit`/`_on_cell_clicked`).
Eftersom en konsekvens kan ha 0, 1 eller flera länkade rekommendationer
(många-till-många via `consequence_recommendations`) men cellen bara är
EN textrad, valdes: 0 länkade → tom redigerare, Enter skapar en ny
länkad rekommendation. 1 länkad → redigeraren visar den befintliga
beskrivningen, Enter uppdaterar SAMMA rekommendation på plats (delar
samma "uppdatera överallt / dela upp" `QMessageBox`-fråga som redan
fanns för delade rekommendationer — flyttad ut ur
`_RecommendationDetailDialog._save()` till en fristående
`_apply_shared_recommendation_description_update()`-hjälpare i hazop.py
så båda vägarna delar samma regel). 2+ länkade → tom redigerare, Enter
LÄGGER TILL en tredje utan att röra de befintliga.

**Extra info-popupen:** ny `RecommendationAssistPopup` (samma icke-
toplevel-barnwidget-mönster som `StandardCauseSuggestPopup`, se
"Standardorsaksförslag" — en riktig separat toplevel-popup visade sig
tidigare trigga en falsk FocusOut på cellredigeraren och stänga den i
förtid) öppnas ovanför cellen ("ovanför sitt fält", samma konvention som
"Flytta HAZOP-popups ovanför"). Listar HELA rekommendationskatalogen som
kryssrutor (kryssad = länkad till just denna konsekvens) plus en ✎-knapp
per rad för att öppna den befintliga `_RecommendationDetailDialog` (ansvarig/
förfallodatum/status) — oförändrad, bara nåbar via popupen istället för
den rivna dialogen.

**Qt-fälla hittad under arbetet:** Qt anropar ALLTID `setEditorData()`
direkt efter `createEditor()` — standardimplementationen för en QLineEdit
skriver då OVILLKORLIGEN över editorns text från modellens data, vilket
tyst nollställde den seedning `createEditor()` gjorde för REK-cellen
(bekräftat med ett isolerat testskript). Fixat med en `setEditorData()`-
override på `_ScenarioDelegate` som hoppar över standardbeteendet just
för REK-kolumnen. Samma latenta bugg finns troligen även i ORS-kolumnens
emoji-prefix-strippning i `_PidDelegate.createEditor()`, men är osynlig
där idag (strippat och ostrippat värde råkar vara identiska sedan
2026-08-25 års ORS-omarbetning) — inte fixad, ren dokumentation.

**Testfälla hittad under arbetet:** ett test som förlitade sig på att
den RIKTIGA `QTimer.singleShot(200, ...)` faktiskt hann leverera sitt
anrop inom ett `QTest.qWait(250)`-fönster passerade isolerat men
FALLERADE deterministiskt (inte flakigt — samma fel två körningar i rad)
när det kördes som del av den kombinerade sviten (386 test i samma
process) — 250 ms räcker uppenbarligen inte alltid när processen redan
kört hundratals tidigare tester. Fixat genom att byta till samma mönster
som `KonInlineEditTests` redan använde: mocka `QTimer.singleShot` att
köra sin callback synkront istället för att lita på den riktiga timern.

**Verifiering:** `RecommendationColumnTests` (12 tester, inline-redigering
+ commit-vägarna för alla fyra länknings-fall) och `RecommendationAssistPopupTests`
(5 tester, ersätter den rivna `RecommendationPickerPopupTests`) i
tests/test_integration.py. `RecommendationEditConflictTests` (4 tester,
testar `_RecommendationDetailDialog` oförändrat) omkörda och gröna mot
den refaktorerade `_save()`. `test_smoke` + `test_scenario_panel` +
`test_hazop` + `test_integration` (386 test) gröna två körningar i rad
(kontrollerat specifikt för att verifiera att timer-fixen ovan verkligen
löste den icke-deterministiska fallissemanget). Hela 14-filssviten
(946 test) grön.

## Flytta konsekvenskategori till riskmatrisen (2026-08-26)

Anton: "Ta bort kategori-/C-värdesvalet från konsekvensfältet i HAZOP
Scenario, inklusive visning som Per C5. Flytta detta till riskmatrisen.
Där ska användaren kunna ange konsekvensnivå separat per kategori, t.ex.
Person C5 och Miljö C3, och se respektive position i matrisen. Frekvens
hämtas från orsaken."

**Vad som togs bort:** KON-cellens "📊"-badgezon längst till vänster
(`_kon_cat_zone_geometry`, konstanten `_KON_CAT_W`) — visade tidigare
antingen ett "📊N"-märke eller staplade "Per C5"-liknande etiketter, en
per kategori. Klick på zonen öppnade `ConsCategoryMatrixPopup` som en
egen popup. Rivet helt: geometrihjälparen, editor-/storleksberäkningarnas
`_KON_CAT_W`-avdrag (texten får nu hela cellens bredd) och klick-
hanteringen i `eventFilter()`. **Medvetet KVAR, oförändrat:**
`ConsCategoryMatrixPopup`-klassen själv och dess ANDRA anropsställe i
`node_markup.py` (`PropertiesRibbon._edit_cons_sev`, "Konsekvens per
kategori"-knappen i högerpanelen för en markerad konsekvens på P&ID) —
begäran gällde specifikt "konsekvensfältet i HAZOP Scenario", inte den
ribbonknappen, och att riva ett oberoende, fungerande åtkomstsätt hade
varit en oombedd sidoeffekt.

**Vart det flyttade:** `RiskMatrixPopup` (öppnas genom att klicka på
"Risk före barriär"-cellen, samma popup oavsett om konsekvensen redan
har en kategoribedömning eller inte) tar nu emot valfria `db=`/
`cons_id=`-argument. När de ges visas en ny sektion under själva F×C-
rutnätet: en rad per konsekvenskategori med kompakta severity-knappar
(samma stil/mönster som `ConsCategoryMatrixPopup`, men sparar OMEDELBART
per klick istället för att vänta på en OK-knapp — så markeringarna på
rutnätet ovanför uppdateras direkt). Varje kategori med en satt severity
ritar nu ut sin kortnamnsförkortning ("Per", "Mil" osv.) direkt i den
rutnätscell den motsvarar — ALLTID i samma frekvenskolumn (den delade,
från orsaken hämtade `current_freq`), så flera kategoriers positioner
syns samtidigt i EN och samma matrisvy ("se respektive position i
matrisen"). Frekvensen går inte att ändra per kategori i den nya
sektionen (bara raden/severityn) — matchar "Frekvens hämtas från
orsaken" ordagrant; att klicka en rutnätscell direkt (den befintliga,
oförändrade snabbvägen för den enkla/icke-kategoriserade bedömningen)
sätter fortfarande både frekvens och severity och stänger popupen precis
som förut.

**Ny signal:** `RiskMatrixPopup.category_changed` emitteras efter varje
sparad/rensad kategori-severity; anropsstället (`_on_cell_clicked`s
"Risk före barriär"-hantering) kopplar den till `self._schedule_rebuild()`
— samma mönster `_apply_risk_from_matrix_cat` redan använde. Detta är
också det som gör den FÖRSTA kategoribedömningen för en konsekvens
möjlig att skapa nu: `_C_RFORE`-radernas per-kategori-vy krävde
tidigare att en `consequence_severities`-rad redan fanns (skapad via det
rivna badge-klicket) innan den visades alls — nu skapas den första raden
direkt i den nya sektionen, tabellen bygger om, och en egen per-kategori-
riskcell dyker upp i "Risk före barriär" för fortsatt redigering.

**Verifiering:** `RiskMatrixCategorySectionTests` (6 tester — bakåt-
kompatibilitet utan db/cons_id, en rad per kategori, spara+signal,
rensa genom att klicka samma severity igen, markör i rätt/fel
frekvenskolumn, förifyllda befintliga severities) och
`KonCellCategoryBadgeMovedToRiskMatrixTests` (3 tester — tooltip nämner
inte längre badgen, geometrihjälparen är borta, klick på riskcellen ger
popupen `db`/`cons_id`) i tests/test_scenario_panel.py. Kontrollerat att
8 av 9 nya tester verkligen FALLERAR mot koden innan denna ändring
(`git stash` på scenario_panel.py, testfilen orörd) — den nionde
(`test_clicking_a_severity_button_...`) föll också men syntes inte i den
trunkerade outputen, samma orsak (`TypeError: unexpected keyword
argument 'db'`) som de andra `RiskMatrixPopup(...)`-konstruktionerna.
`test_smoke` + `test_scenario_panel` + `test_hazop` + `test_integration`
+ `test_node_markup` (406 test, täcker även den oförändrade ribbon-
åtkomsten) och hela 14-filssviten (955 test) gröna.

## Filtrera orsaker i trädet (2026-08-26)

Anton (efter en tidigare, obesvarad förtydligandefråga samma session):
"när jag drar från pod [P&ID] viewer till trädet så kopplar jag objektet
mot en ny avvikelse. problemet är att i hazop scenario ser jag flera
objekt. jag vill bara se det objektet som precis dragits."

**Rotorsak:** `MainWindow._on_equipment_dropped_on_deviation` (hazop.py)
— hanteraren för att dra en eller flera P&ID-utrustningsmarkörer till en
avvikelse i HAZOP-trädet — skapade rätt tagg-kopplad orsak per markör,
men avslutade med `self.scenario_panel.load_node(node_id)`: det visar
HELA nodens samtliga avvikelser/orsaker, inte bara den/de precis
skapade. Om noden redan hade andra, orelaterade orsaker under andra
avvikelser (det vanliga fallet — en nod har typiskt många fördefinierade
avvikelser) syntes alla dessa också, exakt "flera objekt"-symptomet.

**Fix:** bytte till `self.scenario_panel.load_cause(last_cause_id)` —
samma vy-avgränsning som att klicka på just den orsaken någon annanstans
i trädet redan ger (en enda orsak + dess egna konsekvenser, inget annat).
Vid flera samtidigt dragna markörer (stöds redan, en orsak skapas per
markör) visas den SISTA skapade orsaken, samma "sista vinner"-princip
trädmarkeringen (`tree_panel.refresh(CAUSE_T, last_cause_id)`) redan
använde för vilken nod som markeras.

**Verifiering:** ny test
`test_dropping_one_marker_scopes_the_scenario_view_to_just_that_cause`
i `EquipmentDropOnTreeDeviationTests` (tests/test_integration.py) — sätter
upp en nod med flera orelaterade orsaker under ANDRA avvikelser (för att
en naiv `load_node()` verkligen skulle läcka in dem), drar en ny markör
till en av nodens avvikelser, och kontrollerar att `panel._row_meta`
bara innehåller den precis skapade orsaken. Kontrollerat att testet
verkligen FALLERAR mot koden innan denna ändring (`git stash` på
hazop.py, testfilen orörd) — visade exakt de tre orelaterade orsakerna
plus den nya, matchande den rapporterade buggen. De sex befintliga
testerna i samma klass (`test_on_equipment_dropped_on_deviation_*`)
kör om och gröna oförändrat. `test_smoke` + `test_hazop` +
`test_integration` + `test_tree_panel` (306 test) samt hela 14-
filssviten (956 test) gröna.

## Ändra standardfilter i Worksheet (2026-08-26)

Anton: "I Worksheet ska rutorna visa samtliga noder som standard.
Inställningen visa orsaker utan avvikelser ska vara ikryssad som
default." (den andra kryssrutans namn är i praktiken "Visa avvikelser
utan orsaker" — enda existerande sådan kryssruta i Worksheet, texten är
uppenbart samma ruta bara omvänd ordning på orden.)

**Fix:** `HAZOPWorksheet.__init__` (worksheet.py) — båda kryssrutorna
(`_all_nodes_cb`, `_show_empty_dev_cb`) sätts nu till `setChecked(True)`
direkt efter att deras `toggled`-signaler kopplats (INTE före — Qt
emitterar bara `toggled` vid en faktisk värdeändring, och innan
signalerna är kopplade skulle en tidigare `setChecked(True)` bara ändra
kryssrutans EGET visuella läge utan att nå `_table_panel.load_all()`/
`set_show_empty_deviations(True)`).

**Testuppdatering:** fyra befintliga tester i `HAZOPWorksheetTests`
(tests/test_worksheet.py) antog implicit att båda rutorna startade
avmarkerade (`test_selecting_combo_entry_calls_load_node_with_right_id`,
`test_checking_all_nodes_disables_combo_and_calls_load_all`,
`test_show_empty_dev_checkbox_calls_set_show_empty_deviations`,
`test_deviation_column_always_visible_regardless_of_checkboxes`) —
uppdaterade att explicit nollställa den relevanta rutan (`setChecked
(False)`) INNAN de testar sina egna check/uncheck-övergångar, så samma
mekanik verifieras som förut, bara mot ett nytt startläge. Två nya
tester (`test_all_nodes_checkbox_defaults_to_checked_and_loads_all`,
`test_show_empty_deviations_checkbox_defaults_to_checked`) verifierar
både kryssrutans EGNA lägre och att effekten verkligen nått
`_table_panel` (inte bara ett visuellt kryss). `test_smoke` +
`test_worksheet` + `test_integration` (252 test) gröna — liten,
väl avgränsad ändring i en fil, ingen full regressionskörning.

## Ta bort Smart Polygon — arkiverad (2026-08-26)

Anton: "Riv den befintliga Smart Polygon-funktionen som används när
objekt markeras/kopplas på P&ID. Arkivera detta."

**Tolkning (ingen exakt "Smart Polygon" hittades):** en genomsökning av
hela kodbasen hittade ingen bokstavlig "Smart Polygon". Den enda
"smart"+form/väg-funktionen som fanns var **"Smart polylinje"**
(`MODE_SMART_POLYLINE`/`SmartPipeTracer`) — ett rörspårningsverktyg:
klicka en start- och slutpunkt på P&ID, en algoritm föreslår en trolig
rörledningsväg mellan dem (med vänster/höger-pil för att bläddra mellan
alternativa vägar, Enter för att spara som en polylinje-markup). Detta
gick vidare med som den avsedda funktionen (polygon/polylinje-
förväxling, "kopplas" passar ett verktyg som just spårar en koppling
mellan två punkter) — flaggas ändå tydligt här ifall det visar sig fel,
eftersom arkivering (inte permanent radering) gör felbedömningen billig
att ångra.

**Arkiverat:** `SmartPipeTracer`-klassen flyttad ORÖRD till nya
`hazop/archive/smart_pipe_tracer.py`, samma mönster som "Ta bort Smart
Layout"-arkiveringen tidigare denna session (se ovan).

**Rivet ur den aktiva appen:** `MODE_SMART_POLYLINE`-konstanten
(pid_viewer.py), all `_smart_*`-tillståndshantering och rit-/
förhandsgranskningslogik i pid_graphics_view.py, `'smart'`-verktygsknappen
i nodmarkup-verktygsfältet (node_markup.py — den enda klickbara
åtkomstpunkten), och `'smart': MODE_SMART_POLYLINE`-mappningen i
`pid_panel_mod.py`s `_set_markup_tool` för BÅDE `'node'`- och
`'red'`-fallen. Den `'red'`-mappningen var redan död kod innan denna
ändring — `RedMarkupPanel._TOOLS` trimmades till bara select/symbol redan
2026-08-17 (se "Red markup konsolideras"), så inget verktygsfält
exponerade den längre — en bonus-städning upptäckt under arbetet, inte i
ursprungsbegäran.

**Verifiering:** 16 nya tester fördelade över `tests/test_node_markup.py`,
`tests/test_pid_graphics_view.py`, `tests/test_pid_panel_mod.py` — bekräftar
att `'smart'`/`MODE_SMART_POLYLINE`/`SmartPipeTracer` inte längre är
nåbara och att kvarvarande verktyg (select/polygon/symbol) fungerar
oförändrat. Hela 14-filssviten grön (974 test vid agentens egen körning;
996 test i den slutliga, sammanslagna körningen med alla tre denna
omgångs ändringar tillsammans).

## Gör om Red Markup-knappen (2026-08-26)

Anton: "Knappen för att rita symboler på P&ID ska inte längre öppna den
gamla Red Markup-vyn. Riv den gamla funktionen och låt knappen istället
öppna endast den mindre popupen för symbolval."

**Vad "den gamla vyn" var:** `MainWindow._on_edit_red_markup` — klick på
"Lägg ut P&ID-symbol" (props_ribbon) körde `_switch_view(1)`, ändrade om
tre splitters, och visade dels `red_markup_panel` (en smal 2-knapps-
ribbon, redan trimmad 2026-08-17) dels `red_markup_table_panel` (en
tabell över befintliga red markups: Typ/Etikett/Färg/Opacitet/
Tjocklek/👁) — INNAN den öppnade symbolval-popupen.

**Viktig risk hanterad:** `_on_edit_red_markup` gjorde INTE bara visuella
saker — den anropade även `pid_panel.enter_red_markup_edit(node_id)`,
som binder vilken nod den nya symbolen ska sparas mot OCH kopplar de
signaler (`markup_draw_finished`/`markup_item_clicked`) som gör att en
ritad symbol faktiskt sparas till databasen. Att bara ta bort hela
anropet hade tyst trasat sönder symbolplacering (man ritar en symbol,
inget sparas, ingen felindikation). Den nya `_on_place_symbol_requested`
behåller detta anrop men hoppar över ALL splitter-omstorlek/panel-
visning — enda synliga effekten av knappen nu är att symbolval-popupen
öppnas.

**Ny "stäng"-koppling:** den gamla vägen ut ur red-markup-läge gick via
`red_markup_panel`s ✕-knapp — oåtkomlig nu när panelen aldrig visas.
Löst genom att `_on_red_markup_draw_finished` (efter att en symbol
faktiskt sparats) omedelbart anropar `_on_close_red_markup()` själv —
att placera en symbol är hela poängen med denna korta "avstickare" från
nodmarkup-redigering, ingen separat stängningsknapp behövs längre.

**Medveten funktionsförlust (flaggas explicit till Anton):**
`RedMarkupTablePanel` är helt borttagen — det finns nu INGEN kvarvarande
väg att RADERA en redan placerad red-markup-symbol eller ändra dess
färg/opacitet/tjocklek (färg/opacitet/tjocklek var redan bortopererade
2026-08-17 och gav bara fasta standardvärden). Storleksändring/rotation
av en redan placerad symbol fungerar fortfarande oförändrat via
"Välj/flytta"-verktygets grepp direkt på P&ID-canvasen — bara
LISTVYN/RADERINGEN för redan placerade symboler är borta. Om detta visar
sig behövas ändå är det en uppföljningsbegäran, inte återställd här.

**Verifiering:** `RedMarkupConsolidationTests` (tests/test_node_markup.py)
uppdaterad + två nya tester, inklusive ett fullständigt end-to-end-test
(klicka knappen → välj symbol i popupen → simulera ritning på canvasen →
bekräfta att en rad verkligen landade i databasen OCH att appen
automatiskt återgick till nodmarkup-läge utan manuell stängning).
`tests/test_integration.py` uppdaterad för borttagningen. Hela
14-filssviten grön (960 test vid agentens egen körning; 996 test i den
slutliga, sammanslagna körningen).

## Skapa sidan Rekommendationer (2026-08-26)

Anton: "Lägg till en ny sida i huvudmenyn bredvid Worksheet och P&ID
View med namnet Rekommendationer. Lista alla rekommendationer i kolumn
1. Kolumn 2 ska visa hierarkisk referens enligt
studie.nod.avvikelse.orsak.konsekvens, exempelvis 1.1.1.1.1 eller
1.3.1.1.1."

**Ny sida:** `RecommendationsPanel` (nytt `recommendations_panel.py`,
samma lager+re-export-mönster som `worksheet.py`) — infogad direkt efter
Worksheet, ny index 3 (Utrustning/Studiehantering/Inställningar
skiftade från 3/4/5 till 4/5/6; varje hårdkodad `_switch_view(N)`-plats i
hazop.py räknades om i samma steg). En enkel, read-only tvåkolumns-
tabell: kolumn 1 = "R-XXX. beskrivning" (samma konvention som REK-
kolumnen i HAZOP Scenario), kolumn 2 = referens(er) — kommaseparerade om
en rekommendation delas mellan flera konsekvenser, "—" om den (ännu)
inte är kopplad till någon (appen raderar aldrig en rekommendation bara
för att dess sista koppling tas bort).

**Numreringsbeslut (medveten förenkling, inte samma som trädets egen
numrering):** varje nivå numreras efter sin EGNA råa DB-radordning
(`db.nodes()`, `db.deviations(node_id)`, `db.causes_for_deviation(...)`,
`db.consequences(...)`, alla `ORDER BY id`) — platt över hela studien,
System-grupperingen (SYSTEM_T) ignoreras helt (skulle kräva en sjätte
siffra, aldrig efterfrågat). Detta är MEDVETET INTE samma avvikelse-
numrering trädet (tree_panel.py) visar — trädet slår ihop flera råa
`deviations`-rader som delar samma ledordstext till EN numrerad rad, så
en rad-position kan skilja sig från vad trädet visar för samma avvikelse.
Att återskapa den textmatchande ihopslagningen bedömdes vara
oproportionerlig komplexitet för ett oklart mervärde — den enklare,
helt deterministiska rå-DB-ordningen valdes istället. Om användare
förväntar sig att sidans siffror matchar trädets exakt, är detta känt
och avsiktligt, inte ett missat fall.

**Bonusfixar hittade under arbetet:** `recommendations_panel` saknades i
`_reload_all_panels()`s lista över paneler som får sin `db`-referens
bytt vid projektbyte (samma buggklass som andra paneler i den listan
redan skyddar mot) — tillagd. En `QTableWidget` med `setSortingEnabled
(True)` visade sig (verifierat, inte antaget) bära ett implicit
"kolumn 0, fallande"-sorteringsläge redan vid konstruktion, vilket
tyst vände katalog-id-ordningen efter en bulk-populering — fixat med en
explicit stigande sorteringsindikator.

**Verifiering:** ny `tests/test_recommendations_panel.py` (8 tester,
inklusive en medvetet ICKE-trivial position: 2:a noden / 3:e råa
avvikelsen / 2:a orsaken / 2:a konsekvensen → `"1.2.3.2.2"`, plus
nollkopplad-placeholder, flerkopplad-sammanslagning, och ett fullständigt
navigeringsknapp-regressionsskydd som kontrollerar ALLA sju sidors
index). `tests/test_smoke.py`/`tests/test_integration.py` uppdaterade
för den nya sidan/förskjutna index. Hela 14-filssviten grön (958 test
vid agentens egen körning, innan sammanslagning med de två andra
ändringarna ovan; 996 test i den slutliga, sammanslagna körningen).

**Genomförande denna omgång:** samtliga tre ändringar ovan (Smart
Polygon, Red Markup, Rekommendationer) implementerades parallellt av tre
subagenter i separata git worktrees (på Antons förslag om att prova
subagenter för hastighet), var för sig fullt testade innan
sammanslagning. `hazop.py` var den enda filen med en verklig sammanslag-
ningskonflikt (Red Markup-agentens splitter-storleksändringar mot
Rekommendationer-agentens navigerings-/sidindex-tillägg) — löst manuellt,
en enda rad (`_reload_all_panels`s panellista), bekräftat med en full
14-filskörning (996 test) efter sammanslagning.

## Nytt projekt rensar inte P&ID-objekt (2026-08-27)

Anton: "från tidigare körning blev det någon bugg att objekt på p&id inte
försvinner ens om jag klickar nytt projekt uppe i file" — senare
förtydligat: "buggen startade igår."

**Rotorsak:** `MainWindow._hzp_new`s tabellrensning (`_PROJECT_TABLES`,
ursprungligen från 2026-06-18) körde `DELETE FROM nodes` och `DELETE FROM
equipment_catalog` medan foreign-key-tvång fortfarande var aktivt
(`PRAGMA foreign_keys = ON`, satt en gång vid anslutning). Två kolumner
tillagda senare via `ALTER TABLE` refererar dem UTAN `ON DELETE CASCADE`:
`equipment_catalog.node_id → nodes.id` och `causes.equipment_id`/
`deviations.equipment_id → equipment_catalog.id`. I varje projekt där ett
objekt faktiskt länkats till en nod/avvikelse/orsak (det vanliga "dra
objekt till trädet"-flödet) gjorde detta att just DESSA DELETE-satser
föll på ett foreign-key-fel — tyst uppfångat av ett bart `except
Exception: pass` — och lämnade noden OCH `equipment_catalog` helt
orörda. `equipment_markers` (de faktiska P&ID-markörpositionerna/
taggarna) fanns dessutom aldrig med i listan alls, och en lång rad andra
tabeller (severity-bedömningar, rekommendationskatalog, LOPA-
reduktionsfaktorer, safeguard-undantag, sid-/revisionshistorik,
System-grupperingen) förlitade sig helt på CASCADE från just de tabeller
som nu tystnat.

**Varför "igår":** varken `PRAGMA foreign_keys = ON` eller de icke-
kaskaderande kolumnerna är nya (alla från en refaktorering långt
tillbaka) — själva koden är inte en regression. Bäst tolkning: dagens
och gårdagens sessioner byggde/testade just den funktionalitet som
SKAPAR dessa korsreferenser (dra P&ID-objekt till trädet, tagga orsaker
med `equipment_id`), så en vanlig, redan existerande databasfil fick
helt enkelt fler av just de länkar som triggar buggen. Buggen syns dessutom
bara när själva databasfilen inte kan raderas-och-återskapas (OneDrive-
lås, eller att appen redan är öppen — exakt det scenario 2026-06-18-
committen själv skrevs för att skydda mot) — lyckas filraderingen slås
allt rent oavsett tabellrensningens resultat, vilket gör buggen
intermittent/svår att lita på som "det funkar" bara för att det
råkade fungera senast.

**Fix:** `_hzp_new`s tabellrensning bröts ut till en egen, direkt
testbar metod `MainWindow._wipe_project_tables()` (tar inga sökvägs-/
filargument alls — arbetar enbart mot `self.db.conn`). Den omsluter hela
DELETE-loopen med `PRAGMA foreign_keys = OFF` / `= ON` — eliminerar
ordningsproblemet helt (det finns ingen "fel ordning" när ändå VARJE rad
i VARJE listad tabell ska bort) — samt utökar `_PROJECT_TABLES` med alla
tabeller som tidigare bara förlitade sig på CASCADE:
`equipment_markers`, `consequence_categories`, `severity_definitions`,
`consequence_severities`, `consequence_severity_exclusions`,
`safeguard_cause_exclusions`, `reduction_factors`, `recommendations`,
`consequence_recommendations`, `consequence_steps`, `pid_revisions`,
`pid_sheets`, `systems`. Medvetet OFÖRÄNDRADE (bevaras mellan projekt,
precis som förut): `standard_causes`/`standard_deviations`/
`standard_objects` (den delade mallbiblioteket), `tag_database`/
`tag_database_settings` (inlärd tagg-vokabulär avsedd att återanvändas),
statiska referenslistor (`component_types`/`node_types`/
`failure_modes`), samt `symbol_templates`/`equipment_types` (oklart
ägarskap, lämnade orörda för att undvika oombedd scope creep).

**Fälla hittad under implementationen:** `PRAGMA foreign_keys = ON` är
ett no-op om det körs MEDAN en transaktion fortfarande är öppen (SQLite
tillåter bara växling mellan transaktioner) — mitt första försök körde
den direkt efter DELETE-loopen men FÖRE `commit()`, vilket tyst misslyckades
(inget undantag, bara ingen effekt) och lämnade foreign-key-tvånget
avstängt för resten av sessionen. Fixat genom att flytta `commit()` före
återaktiveringen av pragmat. Fångades av en egen ny regressionstest
(`test_foreign_keys_enforcement_is_restored_afterward`) innan det hann
bli ett andra, separat problem.

**Verifiering — medvetet FARLIG kod att testa direkt:** `_hzp_new()`
riktar sitt filraderings-/omöppningssteg alltid mot den modulglobala
`DB_PATH`-konstanten, oavsett vad `self.db` råkar peka på — att anropa
den direkt i ett test utan mycket noggrann `DB_PATH`-mockning riskerar en
RIKTIG filoperation mot `hazop_project.db`. Detta hände faktiskt två
gånger under själva felsökningen av denna bugg (ett `MainWindow()`-
anrop utan `_TempDbMainWindow`, och `Database.__init__`s
importtidsbundna default-sökväg som gjorde en `hazop.DB_PATH`-mockning
verkningslös för just den konstruktorn) — ingen dataförlust i slutändan
(en färsk backup från `hazop_backups/` gjorde återställningen trivial,
se sessionens egen felsökningslogg), men det är EXAKT anledningen till
att `_wipe_project_tables()` bröts ut som en egen, fristående metod:
den tar inga sökvägsargument alls och är därför fullt säker att anropa
direkt i ett test. Ny testklass `WipeProjectTablesTests`
(tests/test_integration.py) — fyra tester: länkad nod/objekt/markör
rensas trots foreign-key-relationen som tidigare blockerade det,
tidigare fungerande orelaterat-data-fall fortsätter fungera oförändrat,
foreign-key-tvånget är verkligen tillbaka på efteråt, och de nyupptäckta
tabellerna (rekommendationer/kategorier) rensas också. Kontrollerat att
alla fyra genuint FALLERAR mot koden innan fixen (`git stash` — metoden
finns inte alls där, vilket i sig bevisar testerna verkligen träffar den
nya koden). `test_smoke` + `test_hazop` + `test_integration` (253 test)
samt hela 14-filssviten + `test_recommendations_panel` (988 test) gröna.

## Dynamisk färgmarkering av objekt på P&ID utifrån HAZOP-trädet (2026-08-27)

Anton: "Ändra markeringarna på P&ID så att objekten automatiskt
färgsätts beroende på vilken nivå användaren står på i HAZOP-trädet.
Grönt ska tills vidare användas för objekt som är relevanta för den
aktuella positionen i trädet... Bygg lösningen så att färgen inte
hårdkodas direkt till typen av koppling."

**Designbeslut fattade med Anton innan implementation (se plan-fasen):**
1. Trädet har ingen egen "Objekt"-rad längre (togs bort 2026-08-25) — en
   ENDA rekursiv regel täcker alla nivåer istället för fyra särfall: att
   klicka en Orsak-rad ("Objekt-nivå") highlightar den orsakens eget
   objekt PLUS allt taggat på dess egna konsekvenser/safeguards; en
   Konsekvens-rad exkluderar sin förälders (orsakens) objekt men
   inkluderar sina egna safeguards; scope flödar bara NEDÅT, aldrig
   uppåt mot förälder.
2. Tagg-matchning räknar ALLA historiskt dragna taggar (`tagged_refs`),
   inte bara den senaste (`comp_tag`) — mer fullständigt.

**Ny DB-metod:** `Database.equipment_link_types_in_scope(type_, id_) ->
dict[equipment_id, set[link_type]]` (database.py) — en batchad
nedåt-traversering (System ⊇ dess noder ⊇ deras avvikelser ⊇ deras
orsaker ⊇ deras konsekvenser ⊇ deras safeguards) via samma
bulk-hämtningsmönster `TreePanel.refresh()` redan använder
(`deviations_for_nodes`/`causes_for_deviations`/
`consequences_for_causes`/`safeguards_for_consequences`), INTE genom att
återanvända de två redan existerande, sinsemellan redundanta
"causes-under-nod"-vägarna — går alltid via avvikelser, exakt som
trädet självt visar. Tagg-sträng → `equipment_id` löses via den redan
existerande `Database.get_equipment_by_tag(tag)`, cachad lokalt per
anrop. `deviations`s egna direkta `equipment_id`-koppling räknas också
(länktyp `'deviation'`) trots att begäran bara nämnde orsak/konsekvens/
safeguard — annars hade en avvikelse direktkopplad till ett objekt utan
någon orsak ännu visat ingenting, ett synligt hål.

**Extensibel färg-per-kopplingstyp (INTE hårdkodad):** nya konstanter i
pid_viewer.py — `TREE_CONTEXT_LINK_COLORS` (dict `'deviation'|'cause'|
'consequence'|'safeguard'` → `QColor`, alla grönt idag),
`TREE_CONTEXT_LINK_PRIORITY` (avgör vilken färg som vinner om ett objekt
har flera kopplingstyper samtidigt — just nu overksynligt, men en
explicit, en-rads-omflyttningsbar ordning istället för att blanda
färger) och `resolve_tree_context_color(link_types)`. Att senare ge
orsak/konsekvens/safeguard olika färger blir en ändring av bara denna
dict — traverseringen och rit-koden rör sig inte.

**Ny, visuellt distinkt highlight i pid_graphics_view.py:** en SEPARAT
overlay (`set_tree_context_highlights`/`_reapply_tree_context_
highlights`, mönster kopierat från den befintliga multi-select-
highlighten `_select_equipment_marker`), inte en mutation av markörens
egen penna (som redan betyder "har avvikelser" via grönt) — en
solid-konturerad halo-ellips vid `Z_OVERLAY+3`, strikt UNDER
multi-select-rektangeln (`Z_OVERLAY+5`, streckad) så en markör som är
både trädkontext-highlightad OCH multi-vald visar båda samtidigt utan
att de flyter ihop.

**Tvådelad cache i PIDPanel** (pid_panel_mod.py): `set_tree_context
(type_, id_)` gör den dyra DB-trädvandringen ENDAST vid faktiskt
trädvalbyte (anropas från `MainWindow._on_selected`); `_apply_tree_
context_highlight()` gör den billiga om-mappningen (cachad
`equipment_id → färg` mot vilka `equipment_markers`-rader som faktiskt
finns just nu) — anropas dessutom vid varje `_load_overlays()`-körning
(sidbyte, redigering) utan ny DB-trädvandring.

**Ny gren för SYSTEM_T:** `MainWindow._on_selected` hade INGEN hantering
alls för att välja ett System i trädet innan denna ändring — lades till
(`pid_panel.clear_active_selection()`, samma städning varje annan gren
redan gör). `_on_scenario_item_edited` tvingar en ny (dyr) omräkning
(inte bara den billiga remappningen) eftersom en redigering kan ha
ändrat själva tagg-datan scope beror på; `_on_structure_changed` släcker
highlighten helt (`set_tree_context(None, None)`).

**Verifiering:** `EquipmentLinkTypesInScopeTests` (10 tester,
tests/test_database.py — varje trädnivå, tagged_refs-fullständighet,
avvikelsens egen equipment_id, omatchbar fritext-tagg, frågeantal-
regressionsskydd), `TreeContextHighlightTests` (6 tester,
tests/test_pid_graphics_view.py — overlay ritas/ersätts, samexisterar
synligt skilt från multi-select, överlever omritning), `TreeContext
HighlightPanelTests` (3 tester, tests/test_pid_panel_mod.py — rätt
`equipment_id`→flera `marker_id`, och att en ren overlay-omritning INTE
kör om DB-trädvandringen — bevisar tvådelningen), `TreeContextHighlight
EndToEndTests` (5 tester, tests/test_integration.py — Nod-val
highlightar allt under den, Konsekvens-val exkluderar förälderns objekt,
byte av val släcker det gamla, det nya System-valet, och ett fullt
tagged_refs-scenario). `test_smoke` + samtliga berörda moduler samt
hela 14-filssviten (1012 test) gröna (en känd, redan dokumenterad
icke-deterministisk `BackupSystemTests`-flakighet sågs en gång,
bekräftad orelaterad genom omkörning).

## Kända begränsningar och tekniska skulder

- **Full `test_regression.py`-körning kan hänga i EN GUI-skapande test, position varierar mellan körningar** (2026-08-13, sett två gånger samma dag: en gång i `RiskCellActualRenderColorTests`, en gång i `EquipmentDropOnTreeDeviationTests` — båda helt orelaterade testklasser till den ändring som pågick) — misstänkt resursuttömning (Windows fönsterhandtag/native-widgets) efter tillräckligt många sekventiella riktiga Qt-widget-skapelser i denna miljö (Python 3.14 + PyQt6), inte reproducerbart isolerat eller i mindre testgrupper. Innan en framtida hängning antas vara en regression: kör den specifika testklassen den hänger i separat (`python -m unittest test_regression.<KlassNamn>`) — den passerar nästan garanterat direkt.
- **Sid-orienteringsinställningen (P&ID-inställningar) är inte kopplad till faktisk rendering/skanning** — sparas i `pid_page_orientation_hint` men läses ännu inte av PDF-rendering, OCR-förbehandling eller taggskanning, som alla idag bara följer PDF-filens egen `/Rotate`-flagga direkt. Att koppla in den kräver att tråda en override genom flera lager, inklusive de flerprocess-skanningsarbetarna — inte gjort 2026-08-11, se ovan. (Den nya per-blad-rotationsknappen från 2026-08-12 löser ett näraliggande men separat behov — manuell rotation av EN specifik sida — genom att mutera `fitz.Page`s rotation i minnet; den treväga globala inställningen är fortfarande inte inkopplad.)
- **Manuell sidrotation (2026-08-12) remappar inte `off_page_connector` eller `board_annotations`** — off-page-kopplingar berör delvis scen-rymd (`dot_scene_x/y`), board-anteckningar ligger i absolut brädrymd. Att rotera en sida med aktiva off-page-kopplingar felplacerar dem. Rotationsknappen bevarar övriga sidors board-layout men gör ingen automatisk ombalansering om den roterade sidans nya fotavtryck (bredd/höjd bytt vid 90°/270°) överlappar en granne.
- **Adaptiv omrastrering (2026-08-12) kan öka minnesanvändningen vid extrem inzoomning** — en enskild sidas rastercache kan nu vara betydligt större än tidigare fasta `_RASTER_SCALE`, begränsat av `_MAX_RASTER_DIM`/`_MAX_RASTER_SCALE`-taken och `_MAX_HIRES` (max antal samtidigt hi-res-sidor), men värsta fall (flera stora sidor renderade nära pixelbudgeten samtidigt) är inte hårt minnesbegränsat.
- **OCR-positioner är approximativa** — x,y-koordinater från OCR stämmer inte perfekt med PDF-koordinater vid hög zoom. Markörer kan hamna något fel.
- **Skalning av P&ID-symboler** — när/om symbolöverlagringar implementeras behöver man hantera att varje P&ID har unik skala. Förslag: en gång per PDF låter användaren klicka på två kända punkter med känt avstånd.
- **Pumpdetektering kan felaktigt trigga på företagslogotyper i titelblocket** — instrumentdetekteringen generaliserar dock bra till nya symbolvarianter utan kodändring, verifierat mot en riktig ITS-fil. **Fixad 2026-08-09** (se nedan): titelblockets hörnregion filtreras nu bort innan pumpdetekteringens tagg-koppling körs.
- **Bowtie-ventilformsdetektion kan slå ihop flera närliggande ventiler till ett kluster** — i tätt packade områden (många ventiler nära varandra) kan klustringen (samma `_CLUSTER_GAP`-mekanism som andra symboler) råka slå ihop 2–3 fysiskt separata ventiler till en enda rapporterad träff. Sett vid verifiering mot en riktig Smurfit Kappa-fil. Påverkar bara "🦋 Hitta ventilformer" (formbaserad), inte den taggankrade "🎯 Hitta på P&ID". **Omprövad 2026-08-09** (20-förbättringar-omgången): `_cluster_cores`/`bbox_iou`/`cluster_distance` finns redan som delvisa skyddsnät (se ovan); en riktig fix av själva klustringen kräver verifiering mot riktiga referensfiler för att inte riskera att UNDER-detektera redan korrekt hittade ventiler — avsiktligt inte gjort blint i denna omgång.
- **Samma klustring kan även slå ihop en ventil med rörledningen den sitter på** — sett vid verifiering av quad-fixen ovan mot en riktig LKAB-fil (QMA184: bowtie-poäng 0.97 men aspect 9.05 eftersom hela rörsträckan klustrades ihop med ventilen, vilket faller på `aspect<=3.0`-filtret). Distinkt från punkten ovan (ventil+rörledning, inte ventil+ventil). Ej åtgärdat.
- **Räddningsmekanismen för textlösa CAD-exportfiler (se "Formdetektering generaliserad..." ovan, 2026-08-12) kan slå ihop en ventil med en äkta, tät skrafferingsmönster** — samma grundläggande risk som de två punkterna ovan (oönskad sammanslagning via nära-liggande geometri), men nådd via en NY kodväg (`cluster_primitives(rescue_dense_cells=True)`, bara aktiv på sidor helt utan inbäddad text). Sett på en riktig Smurfit Kappa-fil ("PANNVATTEN") där en ventil låg inom `_CLUSTER_GAP` (3pt) av ett rörknippe-skrafferingsmönster vars streck (9–50pt) var för långa för att fångas av räddningens "är detta en bokstav"-längdgolv. Ej åtgärdat — en riktig fix (igenkänning av skrafferingsmönster: många parallella, likformiga streck) kräver mer strukturell analys än denna omgångs tidsram tillät.
- **Flerkärnig OCR-parallellisering ger liten faktisk vinst** — se "Flerkärnig parallellisering av Analysera P&ID" ovan. `scan_pdf_for_equipment` med OCR (RapidOCR) skalar dåligt över flera processer i den här miljön (uppmätt ~1.05×, aldrig sämre än sekventiellt) eftersom en enda OCR-motoranrop redan mättar tillgängliga CPU-resurser internt. Formdetekteringen (`detect_equipment_and_valves`, "🎯 Hitta på P&ID") skalar däremot bra (~2.7× uppmätt). En riktig fix för OCR-fallet skulle kräva djupare, motorspecifik trådkonfiguration (t.ex. onnxruntimes `intra_op_num_threads` per session) — inte gjort, utanför denna omgångs tidsram.

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

Om filen växer stor igen (den läses i sin helhet varje session): flytta äldre kronologiska sessionsposter till `NOTES_ARCHIVE.md` (samma mönster som 2026-08-20-trimningen ovan), lämna evergreen-sektionerna (Arkitekturella beslut / Funktioner implementerade / Uppskjutna funktioner / Kända begränsningar / Användarpreferenser) och en rimlig svans av senaste poster kvar, och ersätt det borttagna med en kort pointer-paragraf.

Committa alltid NOTES.md tillsammans med kodfiler:
```
git add hazop.py pid_viewer.py NOTES.md
git commit -m "feat: ..."
git push
```
# Codex-projektinstruktioner för nya sessioner (2026-08-27)

Skapade `hazop/AGENTS.md` som beständig, HAZOP-specifik ingång för Codex.
Filen kräver att varje ny session läser hela `CLAUDE.md` och `NOTES.md`,
kontrollerar `crashes/` och `git status`, följer projektets lagerarkitektur
och teststrategi samt tydligt skiljer automatiska tester från visuell
GUI-verifiering. Den klargör också att projektrotens SIL-instruktioner inte
ska användas som HAZOP-specifikation vid konflikt.

## Säkert byte mellan Codex och Claude (2026-08-27)

Anton vill kunna växla mellan Codex och Claude utan att lokala, ocommittade
HAZOP-ändringar lämnas över otydligt. `hazop/AGENTS.md` kräver därför nu
uttryckligen att Codex efter varje färdig meningsfull ändring kör relevanta
tester, uppdaterar denna fil, committar endast avsedda käll-/testfiler och
pushar till aktuell upstream-gren utan en separat push-begäran. Före staging
och efter push ska `git status` kontrolleras. Orelaterade eller ospårade
projektdata får inte följa med. Om en push blockeras ska det redovisas tydligt
så att inget agentbyte sker under falsk uppfattning att arbetet finns på GitHub.

Samma commit färdigställer den pågående rekommendationsomgången: sidan
Rekommendationer visar `Ansvarig person`, Enter efter en sparad rekommendation
öppnar nästa tomma inmatningsrad utan att skriva över den första, och den
borttagna safeguard-objektväljaren inklusive dess gamla cell-metadata och test
är konsekvent arkiverad/borttagen. Verifiering: syntaxkontroll samt
`tests.test_smoke`, `tests.test_recommendations_panel`,
`tests.test_scenario_panel` och `tests.test_integration`.

## Globala läsbara tooltips + manuellt öppnade konsekvenser/barriärer (2026-08-27)

Tooltipen för 📍 i högermenyn Nod hade texten `Visa nod på P&ID`, men kunde
visas utan läsbar kontrast när källwidgeten hade en lokal stylesheet.
Tooltipfärgerna ligger nu centralt i applikationspaletten: mörk bakgrund
`#17191C` och vit text `#FFFFFF`. Tidigare duplicerade lokala `QToolTip`-regler
i scenariotabellen och riskmatrisens knappar är borttagna.

HAZOP-trädet hålls dessutom kompakt vid val: ett valt objekt/orsak fäller ihop
sina konsekvenser och en vald konsekvens fäller ihop sina safeguards/barriärer.
Användaren öppnar respektive undernivå manuellt med pilen; manuell expansion
är fortfarande tillgänglig och `Expandera allt` är oförändrat.

Verifiering: syntaxkontroll samt `tests.test_smoke`, `tests.test_node_markup`,
`tests.test_tree_panel` och `tests.test_scenario_panel` (241 tester, 10
arkiverade tester hoppades över). Separat global-tooltip-test tillagt i
`tests.test_hazop`.

## Scenario-Enter lämnar trädet stängt + kategoristyrd riskvisning (2026-08-27)

När en konsekvens eller barriär skapades med Enter i HAZOP Scenario använde
`new_item_created` tidigare den nya, dolda raden som mål i
`TreePanel.refresh(type_, id_, emit_selection=False)`. Med auto-collapse kunde
detta öppna objektet och visa den nya konsekvensen direkt. Scenario-skapade rader bygger nu
om trädet utan navigeringsmål;
datan finns direkt i trädet men användarens kollapsade vy och manuella
expansion bevaras. Scenario-tabellen väljer fortfarande den nya raden för
fortsatt inline-inmatning.

Tre tidigare riskmatrisuppföljningar är samtidigt slutförda:

- En konsekvens utan vald konsekvenskategori visar `Välj kategori` och ingen
  automatisk riskfärg/C1-bedömning. Första riskbedömningen skapas först när
  användaren väljer en kategori och dess C-nivå i riskmatrisen.
- Kategorirader visar kortnamn (`Per`, `Mil` osv.) före F/C-värdet i både
  Risk före barriärer och Slutkonsekvens.
- `−N steg` är borttaget ur Slutkonsekvenscellens synliga text. Reduktions-
  informationen finns fortsatt i tooltipen.

Verifiering: syntaxkontroll, `tests.test_smoke`, hela
`tests.test_scenario_panel` (153 tester, 10 arkiverade hoppades över), hela
`tests.test_integration` (239 tester) samt ett särskilt end-to-end-test för
Scenario-Enter där system, nod och avvikelse förblir öppna så objektet syns,
men objektet självt förblir kollapsat så konsekvens och barriär är dolda.
Ingen visuell GUI-körning gjordes.

## Barriärredigering i Scenario öppnar inte safeguardnivån (2026-08-27)

`MainWindow._on_scenario_item_edited()` använde tidigare den redigerade raden
som navigeringsmål vid trädets refresh. När en safeguard sparades med Enter
kunde den därför bli trädets aktuella, dolda objekt och konsekvensen öppnas så
att safeguardnivån syntes; ombyggnaden upplevdes även som att trädet släcktes
och tändes igen. Scenario-redigering gör nu en ren `TreePanel.refresh()` utan
navigeringsmål. System, nod, avvikelse, objekt och konsekvens behåller exakt
sina manuella expansionslägen; en kollapsad konsekvens förblir kollapsad och
barriären syns först efter manuellt klick på pilen.

Verifiering: syntaxkontroll och ett end-to-end-test genom MainWindow som
redigerar en safeguard medan konsekvensen är kollapsad och kontrollerar att
barriären finns i det ombyggda trädet men fortfarande är dold. Ingen visuell
GUI-körning gjordes.

## Trädkontext färgar befintlig gummibandsmarkering (2026-08-27)

Den uppskattade trädstyrda gröna markeringen ritades tidigare som en separat
rund halo runt P&ID-objektet. Samtidigt hade själva utrustningspolygonen en
äldre, konkurrerande färgregel: grå vid `deviation_count == 0` och grön vid
`deviation_count > 0`. Den gamla avvikelsekopplade färgregeln och den separata
halon är nu borttagna.

`PIDGraphicsView.set_tree_context_highlights()` applicerar i stället samma
trädscope och konfigurerbara färgprioritet direkt på den befintliga polygonen
som skapades av gummibandsmarkeringen. Objekt utanför aktuell trädkontext är
alltid neutralgrå; objekt inom kontext färgas gröna. När trädvalet byts eller
rensas återställs polygonen direkt till neutralgrått. Den streckade blå
multi-select-markeringen ligger fortsatt separat ovanpå och räknarbrickorna
för avvikelse/konsekvens/barriär finns kvar, men deras antal styr inte längre
polygonens färg.

Verifiering: syntaxkontroll samt `tests.test_pid_graphics_view` och
`tests.test_pid_panel_mod` (128 tester). Nya regressioner kontrollerar att
`deviation_count > 0` fortfarande ger grå polygon, att trädscope färgar själva
polygonen utan separat ellipselement, att färgen återställs vid kontextbyte och
att beteendet överlever en full overlay-ombyggnad. Ingen visuell GUI-körning
gjordes.

## Auto-collapse kollapsade hela trädet vid Scenario-redigering (2026-08-27)

**Rapport:** Anton: "Hela systemet kollapsar när jag trycker enter och man ser
bara systemmappen." — reproducerat till kolumnerna Konsekvens och Safeguard i
HAZOP Scenario, men bara när kryssrutorna "Auto-collapse nodes"/"Auto-collapse
avvikelser" (Inställningar → Träd) är påslagna.

**Root cause:** ovanstående post samma dag gjorde `TreePanel.refresh()`
medvetet mållös (`select_type=None`) efter en Scenario-redigering, för att
inte tvinga upp Konsekvens/Safeguard-nivån. Men `refresh()` börjar alltid med
`self.tree.clear()`, som nollställer `QTreeWidget.currentItem()` till `None`
— och utan ett mål sattes den aldrig tillbaka. `_apply_auto_collapse()`
(körs sist i `refresh()`) avgör vilken System/Nod-gren som är "aktiv" enbart
via `self.tree.currentItem()` (`_active_system_node_and_deviation()`); med
`currentItem()` == `None` matchade inget system `active_system_id`, så ALLA
System-rader fälldes ihop — inte bara Konsekvens/Safeguard-nivån som var
avsikten.

**Fix:** `refresh()` sparar nu vilket item som var markerat innan `clear()`
och söker upp motsvarande item i det ombyggda trädet igen. Om anropet saknade
ett eget navigeringsmål sätts detta återfunna item tyst tillbaka som
`currentItem()` (fortfarande innanför `blockSignals(True)`, ingen
`_reveal()`/scroll, inga val-signaler) — ren bokföring åt auto-collapse, inte
navigering. Konsekvens/Safeguard-fixen ovan är opåverkad eftersom
`_reveal()`/scroll fortfarande aldrig anropas i detta läge.

**Test:** `PlusRowQuickAddTaggingTests.test_editing_consequence_with_auto_collapse_enabled_keeps_active_node_open`
och `..._safeguard_...` (tests/test_integration.py) — båda röd→grön
verifierade (misslyckades utan fixen exakt som rapporterat: aktiv
System/Nod-rad kollapsad efter redigering). `tests.test_smoke`,
`tests.test_tree_panel` (60 tester, inkl. hela `TreePanelAutoCollapseTests`),
`tests.test_scenario_panel` och hela `tests.test_integration` (240 tester)
gröna. Ingen visuell GUI-körning gjordes.

## Klick på objekt på P&ID visade inget i trädet (2026-08-27)

**Rapport:** Anton: "Om jag klickar på ett objekt på P&ID viewer så kommer
inget upp i trädet. Jag vill att den då också syns ner till objektnivå i
trädet."

**Root cause:** `MainWindow._on_equipment_marker_navigate()` filtrerade redan
HAZOP Scenario-tabellen till objektets rader (`scenario_panel.load_equipment()`,
2026-08-12), men reveal:ade trädet bara till **Nod**-nivå
(`tree_panel.refresh(NODE_T, node_id, ...)`). Var noden redan synlig/öppen
hände ingenting synligt.

**Fix:** ny `TreePanel.reveal_causes_for_equipment(equipment_id)`
(tree_panel.py) återanvänder den redan existerande
`Database.causes_for_equipment()`-frågan (samma matchning: deviation-FK eller
comp_tag+comp_type på orsak/konsekvens/safeguard) för att hitta den första
(lägsta id) orsaken som nämner objektet, expanderar dess hela ankarkedja
(Avvikelse → Nod → System) och sätter Orsak-raden som markerad/`currentItem`.
Bara EN träff avslöjas även om objektet förekommer i flera avvikelser —
scenario-tabellen listar redan alla förekomster separat, och att expandera
flera grenar samtidigt skulle bara bli återkollapsat av
"Auto-collapse nodes/avvikelser" (som per design bara håller EN aktiv gren
öppen). Konsekvens/Safeguard-nivån under orsaken förblir hopfälld som vanligt.
Om objektet saknar HAZOP-data helt (inget cause taggat än) faller
`_on_equipment_marker_navigate` tillbaka till den gamla Nod-nivå-reveal:en
istället för att göra ingenting.

**Test:** `EquipmentMarkerNavigateFiltersScenarioTests.test_reveals_tree_down_to_the_causes_orsak_row`
(röd→grön verifierad) och `..._falls_back_to_node_reveal` (tests/test_integration.py).
`tests.test_smoke`, `tests.test_tree_panel`, `tests.test_scenario_panel`,
`tests.test_equipment_panel`, hela `tests.test_integration`,
`tests.test_pid_viewer` och `tests.test_pid_panel_mod` (693 tester totalt)
gröna. Ingen visuell GUI-körning gjordes.

## Uppföljning: klick på objekt visade bara EN av flera avvikelser (2026-08-27)

**Rapport:** Anton: "Klickar jag på ett objekt i pid viewer som finns på två
avikelser eller fler får du expandera båda avikelserna." — ovanstående
implementation avslöjade medvetet bara den första (lägsta id) träffen, för
att inte krocka med "Auto-collapse avvikelser".

**Fix:** `TreePanel.reveal_causes_for_equipment()` expanderar nu ALLA
Avvikelse-grenar som har en matchande orsak, inte bara den första. System/
Nod-nivån följer fortfarande den vanliga en-aktiv-gren-policyn via
`_apply_auto_collapse()` (matchningar för samma objekt ligger i praktiken
alltid under samma nod). Avvikelse-nivån särbehandlas: efter att
`_apply_auto_collapse()` annars bara skulle lämnat EN avvikelse öppen
(enligt "Auto-collapse avvikelser"), tvingas varje avvikelse med en
matchande orsak upp igen — obesläktade syskonavvikelser fälls fortfarande
ihop precis som kryssrutan avser. Den lägsta orsaks-id:t väljs fortfarande
som markerad `currentItem` för en enda synlig highlight.

**Test:** `EquipmentMarkerNavigateFiltersScenarioTests.test_reveals_every_avvikelse_the_object_is_tagged_under`
(tests/test_integration.py, röd→grön verifierad — misslyckades utan fixen
exakt som rapporterat: den andra avvikelsen förblev hopfälld). Hela
`tests.test_smoke`, `tests.test_tree_panel`, `tests.test_scenario_panel`,
`tests.test_equipment_panel`, `tests.test_integration`, `tests.test_pid_viewer`
och `tests.test_pid_panel_mod` gröna. Ingen visuell GUI-körning gjordes.

## Trädets lagerknappar styr visning och P&ID-färg (2026-08-27)

De tre knapparna **Orsaker**, **Konsekvenser** och **Safeguards** ovanför
HAZOP-trädet är nu oberoende lagerreglage. Vänsterklick visar/döljer
respektive nivå i trädet och behåller samtidigt den tidigare styrningen av
motsvarande P&ID-markörlager. Valet sparas i projektets `app_config` och
återappliceras efter varje träduppdatering.

Alla tre är gröna och aktiva som standard. Högerklick öppnar Qt:s
färgväljare; vald färg sparas per lager och används både som knappfärg och
av den befintliga gummibandsmarkeringen runt berörda objekt på P&ID. Om ett
objekt har flera länktyper används den redan etablerade prioriteten
Safeguard → Konsekvens → Orsak → Avvikelse. Avstängda knappar visas neutralt
grå och textfärgen på aktiva knappar anpassas efter vald färgs ljushet.

**Test:** tre nya regressionstester verifierar standardfärg/-synlighet,
visning av/på inklusive träduppdatering och beständigt färgval som når
P&ID:s färgresolver. `tests.test_smoke`, `tests.test_pid_viewer`,
`tests.test_pid_panel_mod`, `TreeContextHighlightEndToEndTests` och hela
`tests.test_tree_panel` (280 tester totalt) gröna. Ingen visuell GUI-körning.
## Rekommendationer som egna fysiska scenariorader (2026-08-27)

Anton förtydligade att flera rekommendationer under samma konsekvens ska
fungera som flera safeguards: varje rekommendation ska ligga på en egen
fysisk rad och Enter ska gå vidare till nästa tomma rad. Scenario-tabellens
REK-kolumn är därför inte längre spänd över konsekvensens safeguard-rader.
Radbyggaren reserverar alltid en tom REK-rad efter de sparade
rekommendationerna. Varje fysisk rad bär sitt eget rekommendations-ID;
redigering uppdaterar därmed rätt katalogpost och nya Enter-inmatningar
skapar nya poster utan att skriva över den första.

Den tidigare sammanfogade flerradstexten ersattes av en schemalagd
tabellombyggnad efter varje sparad rekommendation, så nästa fysiska rad
materialiseras säkert efter att den aktiva editorn hunnit stängas. Det
förhindrar att en pågående QLineEdit förstörs mitt i Enter-signalen.

**Test:** `tests.test_scenario_panel`, `tests.test_smoke` och relevanta
`RecommendationColumnTests` gröna; sex äldre tester som uttryckligen
förväntade en sammanfogad REK-cell är markerade som ersatta av det nya
radbeteendet.
## Korrigering: trädknappar styr endast P&ID-lager (2026-08-27)

Anton förtydligade att knapparna Orsaker, Konsekvenser och Safeguards ovanför
HAZOP-trädet inte ska dölja trädets rader. De styr enbart motsvarande lager i
P&ID Viewer. Trädet påverkas därför inte längre av knapptryckning eller
lagervisningsinställningar; vanliga expandera-/kollapsa-regler gäller där.
Färgvalet på knapparna och den befintliga P&ID-gummibandsmarkeringen är
oförändrat.
## Orsaksfältets P&ID-status och drag-and-drop (2026-08-27)

HAZOP Scenario visar nu fetstilt **Objekt ej på P&ID** i Orsak-fältet när
orsaken saknar en giltig koppling till `equipment_catalog`. När ett P&ID-
objekt dras till Orsak-fältet kopplas orsakens `equipment_id`, objekttyp och
tagg till den första markerade utrustningen. Tabellen byggs om schemalagt och
övriga paneler synkas via `item_edited`, så den nya objekttaggen visas direkt
och den befintliga P&ID-/scenario-logiken återanvänds.

## Dubbla avvikelser i Worksheet (2026-08-27)

En äldre seed/migrationsväg hade skapat dubbla `standard_deviations`, vilket
sedan gav två identiska avvikelser per nod. Databasmigrationen deduplicerar nu
mallar och generiska avvikelser, tar bort tomma generiska rader när en
utrustningskopplad rad med samma beskrivning redan har innehåll, och skyddar
med unika index mot att samma fel återkommer. Utrustningsspecifika avvikelser
för olika objekt lämnas kvar.

## Viktat taggminne vid gummibandsplacering (2026-08-27)

När ett objekt skapas via högerdragning används taggkodens alfabetiska
teckenkombination som minnesnyckel; löpnummer ignoreras. `study_tag_memory`
returnerar viktade typförslag där tidigare användningsfrekvens avgör mellan
flera klassificeringar med samma prefix och en exakt tidigare tagg får extra
vikt. Den högst rankade typen förifylls i Objekt/Objekttyp-dialogen, medan
övriga typer fortfarande kan väljas manuellt. Ett manuellt bekräftat val lärs
in och ökar därefter vikten för framtida taggar.

## Automatisk höjd i riskmatrisens konsekvensrader (2026-08-27)

När frekvens ligger på X-axeln och konsekvens på Y-axeln mäts nu alla
flerradiga konsekvensbeskrivningar automatiskt efter textlayout. Den högsta
behövda höjden används på samtliga konsekvensnivåer samt på radetiketter och
matrisknappar, så hela matrisen blir jämn. Mätningen schemaläggs även när text
redigeras, så höjden ändras direkt utan att riskmatrisinställningen behöver
byggas om manuellt. Vid konsekvens på X-axeln lämnas den befintliga
höjdlogiken oförändrad.

Vid konsekvens på X-axeln är konsekvensdefinitionerna nu flerradiga textfält
med individuell automatisk radhöjd. Varje kategorirad växer efter sin egen
längsta beskrivning; raderna behöver därför inte vara lika höga i den
orienteringen.

Riskmatrisinställningen har dessutom fått en horisontell splitter med dragkant
till höger. Den ändrar bredden på hela matrisytan inklusive konsekvens-
definitionerna under och behåller matrisen förankrad till vänster.

Splitterdragningen fungerar nu åt båda håll: matrisen kan både förstoras och
förminskas. Kolumnbredderna skalas proportionellt så färgrutor och
konsekvensdefinitioner fortsätter att följa varandra.

## Deltagarmatris: sessionsdatum och plats (2026-08-27)

Analystillfällen har nu separata datum- och platsfält. Nya tillfällen föreslås
dagen efter det senaste datumet. Datum redigeras med kalenderkontroll och plats
visas i sessionens kolumnhuvud. En ifylld plats kan dras från ett
analystillfälle till ett annat för att kopieras. Ett uppstartsundantag där en
SQLite-rad behandlades som en dict är korrigerat.

Analystillfällen har dessutom ett sparat digitalt-läge. Nya tillfällen ärver
senaste analystillfällets ort, medan Digitalt visar en kryssruta som stänger av
ortfältet och visas som "Digitalt" i kolumnhuvudet.
# 2026-08-27 — Grupporsak: separata live-objekt och manuell felriktning
Vid drag-and-drop av flera P&ID-objekt som grupp sparas styrande objekt i
`causes.equipment_id` och påverkat objekt i `causes.secondary_equipment_id`.
Gruppfrågan ber nu användaren välja högt/lågt fel och ventilens öppna/stänga-
effekt. Grupporsaken visar kedjetexten fetmarkerad utan dubblerad objekttagg.
# 2026-08-27 — Tomma HAZOP-celler
Tomma konsekvens-, safeguard-, slutrisk- och rekommendationsceller i HAZOP
Scenario visas utan utfyllnadsstreck. Streck som ingår i faktisk text eller
riskinformation påverkas inte.
# 2026-08-27 — Gruppobjekt: klick och ångra
Grupporsakens fetmarkerade taggar är separata klickzoner. Styrande objekt
startar vanlig P&ID-bindning och påverkat objekt startar sekundär bindning.
Ctrl+Z ångrar dessutom senaste textändring i orsak, konsekvens, safeguard
eller befintlig rekommendation.
# 2026-08-27 — Normaliserade orsakspilar
ASCII-pilarna `->` och `=>` normaliseras vid sparning till `→` i orsak,
konsekvens och safeguard. Samma regel gäller redigering från trädet och
HAZOP Scenario.
# 2026-08-27 — Flera objekt i Orsak-fältet
Shift-drag av flera P&ID-objekt till en befintlig orsak behåller hela gruppen:
första objektet lagras som primär koppling, andra som sekundär koppling och
båda taggarna visas fetmarkerade i orsaksfältet. Gruppens taggar visas även
när orsaken ännu saknar konsekvens.
# 2026-08-27 — Byt ordning på gruppobjekt
Grupporsakens taggpopup visar en ordningsknapp när två objekt är kopplade.
Den byter primär/sekundär live-koppling och uppdaterar taggordningen samtidigt.
# 2026-08-27 — Grupporsakens valpunkter
Grupporsaken visas kompakt som `primär … sekundär …`. Första ellipsen väljer
felriktning (högt/lågt) och den andra väljer typisk effekt (öppnar/stänger
eller annan effekt). Gruppskapandet avbryts inte längre av fasta frågor.
# 2026-08-27 — Grupporsak på två rader
Grupporsaker visas nu i samma ORS-cell på två rader: styrande objekt och
felriktning på rad ett, påverkat objekt och effekt på rad två. Taggarna är
fetmarkerade och respektive textdel är klickbar för val av alternativ.
# 2026-08-27 — Förenklat orsaksbibliotek
Det reducerade standardorsaksbiblioteket är aktivt som standard. Aktiva
standardorsaker utan egen frekvens får standardvärdet 0,02/år; det äldre
biblioteket ligger kvar i arkivet.

# 2026-08-27
# 2026-08-27 — Project Lumen startbild
Startup använder project_lumen_startup.svg i splashskärmen och behåller status/spinner under databas- och GUI-initialisering.

## Konsekvent redigering av HAZOP-scenarioceller (2026-08-28)

Orsak, Konsekvens, Safeguard och Rekommendation använder nu samma fördröjda
edit-start. Dubbelklick avbryter enkelklickets väntande edit, fångar den
faktiska klickpositionen och placerar textmarkören där utan helmarkering.
Editorn avmarkerar efter fokus och använder inte `selectAll()`.

Den gemensamma taggmedvetna editorn behåller samma cellposition och visuella
taggmarkering under redigering. Befintliga specialgeometrier för Orsakens
tagg/frekvens, Safeguards RRF och Rekommendationens sekventiella tillägg är
kvar eftersom de är en del av cellens funktionella layout. Regressionstesterna
uppdaterades till den gemensamma edit-starten och 27 berörda tester passerar.
Ingen visuell GUI-verifiering är gjord.

## Kompakta frekvens- och RRF-badges (2026-08-28)

Orsakens frekvensbadge delar nu en kompakt 32 px-zon med Safeguardens RRF-
badge. F1 visas centrerat och fetstilt i den översta textraden; långa värden
elideras i stället för att bredda cellen. RRF visas på samma sätt och exempelvis
RRF 1000 får kortare visning när värdet inte ryms. Badge-zonerna används fortsatt
gemensamt av målning, klickhantering och editorns geometri.

## Grupporsak redigeras med samma objektlogik (2026-08-28)

Dubbelklick på en Orsak-grupp öppnar nu en gemensam tvåkolumnspopup med
`Orsaksfel` för primärt objekt och `Vad konsekvensen leder till` för sekundärt
objekt. Den visar båda taggarna, befintliga val för felriktning/effekt samt
knappen `Byt primär / sekundär`. Gruppens gamla enkelklicksväg till P&ID-bind
är borttagen; gruppen redigeras i stället via popupen på samma sätt som ett
enskilt objekt redigeras via sin befintliga popup.

## Korrigering av frekvensyta, taggformat och namnbytesvarningar (2026-08-28)

Orsakens frekvenszon reserveras nu med samma dynamiska breddlogik som den
visade taggen och aktuell textstorlek, så tagg och frekvens inte överlappar.
Den gemensamma QTextEdit-editorn återställer fetformat för kända P&ID-taggar
efter Qt:s setEditorData och uppdaterar formatet vid textändring. Identity-
bekräftelsen konverterar sqlite3.Row till dict innan valfria fält läses, så
namnbytesdialoger och varningar kan visas utan exception. Berörda tester
passerar. Ingen visuell GUI-verifiering är gjord.

## Frekvensruta och riskmatrisens kategori-markeringar (2026-08-28)

Orsak-kolumnens frekvens visas nu i en fast ruta med samma visuella modell
som Safeguardens RRF-ruta, och kolumnrubriken är `Orsak (frekvens)`. Den
gamla texten `Välj kategori` i Risk före barriär är borttagen.

I riskmatrisens kategori-läge finns ingen förvald markerad riskruta längre.
Ramarna sätts i stället på de konsekvensnivåer som faktiskt är valda per
kategori vid den gemensamma frekvensen. Flera konsekvensnivåer kan därför
vara markerade samtidigt. 15 riskmatris-/kategori-tester passerar.

## Kraschfix: taggeditor saknade horizontalScrollBar (2026-08-28)

Kraschrapporten `crash_20260828_092809_AttributeError.json` visade att
`_BoldTagLineEdit.paintEvent()` anropade en metod som inte finns på PyQt6-
`QLineEdit`. Textens synliga startpunkt beräknas nu från `cursorRect()` och
caret-positionen, vilket även hanterar Qt:s horisontella textscrollning.
Direkt repaint-reproduktion, syntaxkontroll samt 16 smoke-/Edit Mode-tester
passerar.

## Intelligent namnbyte och objektkoppling i Edit Mode (2026-08-28)

Inline-redigering i HAZOP Scenario behåller nu fet visning av kända P&ID-taggar även när editorn är aktiv. Vid redigering av Konsekvens eller Safeguard identifieras ett borttaget gammalt taggnummer och ett nytt taggformat innan texten sparas. En exakt träff i aktuell equipment_catalog ger dialogen Koppla till objekt / Byt endast namn / Avbryt; utan träff krävs uttryckligt godkännande av namnbytet. Dubblettnamn blockeras och den nya taggen sparas i tagged_refs så att fetstil består.

Orsakens befintliga live-koppling och namnbytesflöde återanvänds. Tre nya regressionstester täcker koppling utan att döpa om gammalt objekt, avbryt utan databasändring och taggmedveten inline-editor. Verifierat med py_compile, tests.test_smoke (12 tester) och berörda edit-tester (26 tester totalt). Ingen visuell GUI-verifiering är gjord.

## Korrigering av Edit Mode-trigger och textens position (2026-08-28)

Enkelklick i Orsak, Konsekvens, Safeguard och Rekommendation väljer nu bara
cellen och startar inte längre någon fördröjd editor. Redigering startar
endast med dubbelklick; den faktiska klickpositionen används för caret utan
helmarkering.

Editorns fokusstyling använder samma borderbredd och padding före och efter
fokus. Standardgeometrin hålls mot cellens övre textlinje i stället för att
centrera en enkelradig QLineEdit i en hög, radbruten rad. 15 berörda tester
passerar. Ingen visuell GUI-verifiering är gjord.

## Wrap och prefix i gemensam Edit Mode-editor (2026-08-28)

Edit Mode använder nu en gemensam flerradig QTextEdit för Orsak, Konsekvens,
Safeguard och Rekommendation. Orsakens objektprefix, Konsekvensens radnummer
och Safeguardens RRF-fält reserveras i editorns geometri så att editortexten
inte täcker eller duplicerar dessa delar. Safeguard-textens statiska cell och
radberäkning använder också word wrap. 15 berörda Edit Mode-tester passerar.
Ingen visuell GUI-verifiering är gjord.

## Minimalistiska objektpopups (2026-08-28)

Grupporsakens popup visar nu två rena objektkolumner utan GroupBox-ramar och
utan extra rubriktext. Varje kolumn visar roll, fet tagg, typ/P&ID-sida och
valbara fel-/effektalternativ; primär/sekundär-växlingen ligger kvar som en
diskret gemensam åtgärd. Den enskilda objektpopupen visar motsvarande objekt-
och P&ID-information ovanför tagg- och typredigeringen. Den gamla Bind till
objekt-knappen är borttagen.

## Tom orsak via Enter (2026-08-28)

Enter skapar nu en tom orsak och en tom konsekvens direkt utan Orsak på
P&ID-dialog och utan automatisk frekvens. Den interna standardnivån används
endast som beräkningsfallback; frekvensbadgen visas först när användaren väljer
en frekvens i cellen.

Grupporsakens popup stannar nu öppen när ett fel-/effektalternativ väljs eller
när primär och sekundär växlas; statusfältet uppdateras direkt. Dubbelklick på
gruppens beskrivande mekanism/effekt öppnar samma inline-editor som en enskild
orsak, medan dubbelklick på någon av de fetstilta taggarna fortfarande öppnar
objektpopupen.

Frekvensklick på grupporsak går nu vidare till samma frekvenspopup och
sparlogik som för enskild orsak. Gruppens taggzon blockerar inte längre
frekvenszonen.

## Objektväljare och placering från tom orsak (2026-08-28)

Popupen för en tom orsak visar nu en lista över befintliga objekt i
equipment_catalog samt alternativet `Nytt objekt…`. Val av ett befintligt
objekt fyller tagg och typ; fria värden kan användas för ett nytt objekt.
Knappen `Placera objekt på P&ID` startar ett engångsläge där nästa klick på
ritningen placerar markören och kopplar objektet till orsaken.

## Fördröjd P&ID-taggväljare i Edit Mode (2026-08-28)

Den gemensamma texteditorn erbjuder nu matchande P&ID-taggar efter två
skrivna tecken med cirka 220 ms debounce i Orsak, Konsekvens, Safeguard och
Rekommendation. Popupen ersätter bara den aktuella taggdelen vid val och
stör inte den övriga texten.

## Beta-flik for P&ID-verktyg (2026-08-28)

HAZOP-tabellens kontextheader och riskkolumner Ã¤r ytterligare komprimerade
fÃ¶r att minska tom yta utan att ta bort mÃ¶jligheten att dra kolumnkanterna.

Nya reduktionsfaktorer lÃ¤ggs nu samtidigt i en global faktorkatalog. I RRF-
dialogen kan en faktor som redan anvÃ¤nts pÃ¥ ett objekt vÃ¤ljas och lÃ¤ggas till
pÃ¥ ett annat objekt.

Deltagarmatrisens analystillfällen visar nu en kompakt närvarokryssruta och
ett integrerat fritextfält per deltagare och tillfälle. Anteckningen sparas
tillsammans med närvaron i databasen.

Skanna P&ID, Skapa HAZOP-noder och Hitta objekt pa P&ID finns nu samlade i
den nya Beta-fliken langst ned i vanstermenyn, med korta forklaringar.
Funktionerna ateranvander EquipmentPanels befintliga arbetsfloden, inklusive
progressdialoger och bakgrundskorning. Verktygsknapparna ar dolda i
utrustningsregistret for att undvika dubbla ingangar.

## Edit Mode: cellens layout och wrapping (2026-08-28)

P&ID-objektens hÃ¶gerklicksmeny har dessutom fÃ¥tt `Redigera placering`.
Det aktiverar ett tydligt engÃ¥ngslÃ¤ge dÃ¤r anvÃ¤ndaren klickar den nya
centrumpositionen; den befintliga objektkopplingen behÃ¥lls och positionen
sparas till databasen.

Den gemensamma flerradiga editorn använder nu exakt cellens textområde för
Orsak, Konsekvens, Safeguard och Rekommendation. Orsakens objektprefix,
Konsekvensens nummer och Safeguardens RRF-badge ligger kvar synliga bredvid
editorn. Alla fyra textfält tillåter word wrap även under redigering, så texten
flyttar inte till en separat rad eller täcker andra delar av cellen.
## Uppföljning av önskelista och avbruten körning (2026-08-28)

Den fördröjda P&ID-taggkompletteringen skyddas nu mot borttagna Qt-editors.
Nya noder får endast aktiva standardavvikelser. X→Y-taggersättning visar
definierade objekt före bekräftelse och uppdaterar objekt samt P&ID-markörer.
Den trädorienterade Excel-exporten är åter kopplad till menyknappen och
P&ID-textträffar navigerar till rätt sida.

Verifierat med syntaxkontroll, `tests.test_smoke` (12 tester) och riktade
sök-/export-/layouttester. Ingen visuell GUI-verifiering är gjord.
## 2026-08-28 — kvarvarande HAZOP-flöden

- Aktiva standardavvikelser filtreras även när en nod skapas direkt via `add_node()`.
- Riskmatrisens hörn `F \\ C` är klickbart för axelbyte; X/Y-riktningarna ligger vid respektive axel.
- Deltagarnas snabbmarkering är kolumnjusterad under analystillfällena och fritext per deltagare/tillfälle behålls.
- Placera på P&ID stöder vänsterklick-drag med streckad gummibandsförhandsvisning och skickar PDF-rektangel till kopplingsflödet.
- Scenariohuvudet hålls kompakt; aktuell kontext visas i tooltip. Fördröjd taggkomplettering avbryts säkert om editorn redan är borttagen.
## 2026-08-28 — orsakstaggar efter Fyll bredd

- `Fyll bredd` får inte längre krympa Orsak/Konsekvens/Barriärer till en bredd där ett inline-visat P&ID-tagnummer radbryts eller klipps när orsaken öppnas för redigering. Minsta gemensamma bredd är 180 px för de tre textkolumnerna; smalare fönster får horisontell scroll.
## 2026-08-28 — safeguard-nummer vid redigering

- Safeguard-editorn startar nu efter det målade sekvensnumret och lämnar numret synligt när safeguardtexten redigeras, på samma sätt som konsekvenseditorn.
## 2026-08-28 — synlig tagg-popup i konsekvenseditorn

- Tagg-completerns popup görs till ett uttryckligt Qt-popupfönster med vit kontrastyta, minsta läsbredd och `raise_()` efter visning. Detta hindrar tabellens viewport från att dölja förslagen när två eller fler taggtecken skrivs i Konsekvens.
## 2026-08-28 — gemensamt faktorregister i Enablers

- Dialogen Övriga reduktionsfaktorer läser nu om det gemensamma faktorregistret vid varje uppdatering och efter redigering. En ny eller ändrad enabler blir därför direkt valbar även från en annan konsekvens.
## 2026-08-28 — Nytt projekt och deltagardatum

- När ett nytt projekt laddades byttes databaskopplingen för deltagarpanelen, men den synliga tabellen uppdaterades inte. `_reload_all_panels()` refreshar nu deltagarmatrisen direkt, så analystillfällen och datum från föregående projekt försvinner omedelbart.
## 2026-08-28 — en synlig närvarokryssruta per cell

- Närvarocellerna behåller sitt modellvärde men är inte längre `ItemIsUserCheckable`. Den tidigare modellindikatorn målades som en extra, icke-klickbar kryssruta ovanpå den riktiga cellkontrollen.
## 2026-08-28 — F\\C-knappen

- Crashrapporten visade att X/Y-riktningsknapparna placerades i gridet men samtidigt markerades för `deleteLater()` när axlarna byggdes om. De är nu panelägda och återanvänds vid axelbyte, vilket gör F\\C-klicket säkert.
## 2026-08-28 — dubbel målning i deltagarmatrisen

- Headless-testernas modellvärde för närvaro såg korrekt ut men bevisade inte vad Qt målade visuellt. Modellcellen använder därför inte längre `checkState`; närvaro lagras i en vanlig dataroll och endast den riktiga QCheckBox-widgeten visas.
## 2026-08-28 — radavstånd vid kategorier till höger

- Riskmatrisens grid behåller inte längre gamla explicita radminimum när orienteringen byggs om. Dessa kvarvarande höjder var orsaken till luft mellan raderna när konsekvenskategorierna låg till höger.
## 2026-08-28 — gemensam höjd för högra kategorikolumner

- Kategori-editorn till höger om riskmatrisen hade 28 px medan riskcellerna var 40 px. Det skapade synlig luft tills en lång text tvingade fram tre rader. Den högra layouten börjar nu på 40 px och växer gemensamt för alla delar av raden.
## 2026-08-28 — axelreglage runt riskmatrisen

- X/Y-riktningsknapparna ligger nu i ett eget skal runt matrisen: X ovanför och lika bred som matrisen, Y till vänster och lika hög som matrisen. De ligger inte längre i cellgridden där de försvann vid ombyggnad.
## 2026-08-28 — upplösningsoberoende riskmatris

- Riskmatrisen är nu toppjusterad med naturlig radhöjd. QGridLayout får inte längre fördela tillgänglig skärmhöjd mellan raderna; extra yta hamnar under matrisen och smalare/lägre fönster använder scroll.
## Tagg-popup och P&ID-koppling i Barriär (2026-08-28)

Barriär/Safeguard visar P&ID-taggförslag redan efter ett tecken. Exakta taggar
från P&ID-registret fetmarkeras live när de skrivs eller väljs; Konsekvens
behåller tvåteckengränsen för popupen.
## Utrustningskolumnen i HAZOP Scenario (2026-08-28)

Den gamla Utrustning-kolumnen är permanent dold även i Visa samtliga noder och
P&ID-filter. P&ID-taggen visas i stället i Orsak-cellen; anrop som tidigare
gjorde kolumnen synlig i dessa lägen används inte längre.
## Rekommendationsredigering i HAZOP Scenario (2026-08-28)

Rekommendationens R-nummer är nu presentation-only: inline-editorn startar
efter numret och sparar endast beskrivningen. Den gemensamma delegaten skriver
inte längre över editorns beskrivning med den formaterade celltexten, så texten
behålls efter sparning i stället för att visas som "Ny rekommendation".
## Fortsatt skrivning efter P&ID-tagg-popup (2026-08-28)

När en tagg väljs med Enter stängs popupen och fokus återgår till Barriär- eller
Konsekvensfältet direkt efter den infogade taggen, så användaren kan fortsätta
skriva utan att klicka i fältet igen.
## Barriärredigering och löpnummer (2026-08-28)

Barriärens löpnummer behandlas nu som separat visningsmetadata även efter
inline-redigering. Editorn lämnar plats för numret och sparandet återställer
numret explicit, så det försvinner inte när barriärtexten ändras.
## Scenario-layout och rekommendationstext (2026-08-28)

Den manuella knappen Fyll bredd är borttagen från användargränssnittet och
kolumnfördelningen körs automatiskt även efter fönster- och splitterstorlek.
Orsakseditorn lämnar nu plats för både löpnummer och hela objekt-taggen.
Rekommendationscellen läser dessutom om den länkade rekommendationen från det
aktuella underlaget vid rendering, så den sparade texten visas i stället för
standardtexten "Ny rekommendation".
## X-riktningsknappens bredd (2026-08-28)

X-riktningsknappen i riskmatrisinställningarna följer nu endast matrisens
egna kolumner. Konsekvenskategori-fälten till höger påverkar inte längre
knappens bredd; bredden synkroniseras efter ombyggnad och splitterdragning.

## Rekommendationstext sparas som vanlig text (2026-08-28)

Den kvarstående visningen av "Ny rekommendation" berodde på att
rekommendationskolumnen använde `_ScenarioDelegate` utan egen
`setModelData()`. Qt kunde då lägga in hela QTextEdit-dokumentet
(`<!DOCTYPE HTML ...>`) i tabellcellen. `itemChanged` tolkade dokumentet som
text, databasens HTML-rengöring lämnade en tom beskrivning och nästa rendering
föll tillbaka till standardtexten. Delegaten sparar nu alltid `toPlainText()`
och signalhanteraren rengör defensivt även äldre HTML-innehåll. Ett
integrationsprov täcker den verkliga editor/delegate/modell/databaskedjan.

## Enter accepterar vald P&ID-tagg (2026-08-28)

När taggpopupen visas i Konsekvens, Safeguard eller Rekommendation accepterar
Enter nu den markerade taggen innan den vanliga editor-committen körs. Popupen
är icke-aktiverande för att behålla fokus i textfältet, vilket på vissa
Windows/Qt-lägen gjorde att Enter annars stängde editorn utan att infoga
taggen. Fokus och markör återgår efter infogningen så fortsatt text kan
skrivas direkt. Rekommendationseditorn använder nu också P&ID-taggbiblioteket.

## Oberoende val och redigering av grupporsak (2026-08-28)

Grupporsakens primär- och sekundärhändelse väljs nu oberoende av varandra.
Första klicket på exempelvis `Felar högt` sparar endast primärhändelsen;
sekundärhändelsen skapas först när ett separat sekundärval görs. Tillståndet
lagras som en bitmask i `group_choices_set`, så popupen visar också korrekt
vilken sida som ännu inte är vald. Grupporsakens text kan dessutom
inline-redigeras som ett helt textblock, inklusive egna formuleringar, utan
att den reduceras till första raden.

## Grupporsak visas i trädet (2026-08-28)

En grupp som skapades med utrustningstyper där ingen automatisk
primär/sekundär mekanism kunde härledas fick tidigare texten "Ny orsak" i
trädet. Trädpanelen visar nu i stället alltid primärt objekt följt av
sekundärt objekt, exempelvis `A-101` följt av `B-202`, tills en händelsetext har valts
eller redigerats.

Gruppobjekten visas på separata rader i trädet (`A-101` följt av `B-202`) för
att återgå till den tidigare tydliga grupppresentationen.

## Radstyrd redigering av grupporsak i HAZOP Scenario (2026-08-28)

En grupporsak i Scenario-panelens Orsak-cell är en cell med två visuella
rader. Dubbelklick på primärraden öppnar nu endast primärtexten och ett klick
på sekundärraden endast sekundärtexten. Klick på respektive fetstilt objekt
öppnar dessutom Grupporsak-popupen filtrerad till rätt sida. Vid direkt
redigering skrivs endast den valda raden tillbaka; den andra raden bevaras.

## Två visuella rader även för äldre grupporsaker (2026-08-28)

Äldre grupporsaker kan ha båda händelserna sparade som en enda piltext.
Scenario-renderingen delar därför även dessa värden visuellt mellan primär- och
sekundärraden utan att ändra det sparade databasvärdet.

## Radanpassad Grupporsak-popup (2026-08-28)

När ett gruppobjekt klickas i Orsak-cellen öppnas nu en popup för den klickade
raden. Primärraden visar endast `Primärhändelse` med primärobjektets val och
sekundärraden visar endast `Sekundärhändelse` med sekundärobjektets val.
Den gemensamma popupens funktion `Byt primär / sekundär` visas endast när hela
gruppen redigeras. Direkt textredigering i cellen lämnar den andra raden
oförändrad.

## Primärval bevarar sekundärrad (2026-08-28)

När båda grupphändelserna redan finns valda ändrar ett nytt primärval endast
primärtexten. Sekundärtexten behålls i databasen och i den återrenderade
Orsak-cellen. Den separata tvåklicksregeln för helt nya grupper gäller fortsatt
så att ett första primärval inte skapar en sekundärhändelse.

## Ingen gemensam Grupporsak-popup (2026-08-28)

Popupen som visade primär och sekundär samtidigt är borttagen. Alla popupvägar
för grupporsak visar nu endast den rad som klickades: primärhändelse för den
övre raden och sekundärhändelse för den nedre. Även klickzoner som saknar
direkt radmetadata bestämmer raden från klickets vertikala position.

## Fri text till höger om gruppobjekt (2026-08-28)

Vid direkt redigering av en grupporsaksrad ligger primär- eller
sekundärobjektet kvar som synlig tagg till vänster. Editorn startar efter
taggen och visar endast den fria händelsetexten. Vid sparning sätts rätt tagg
tillbaka på den valda raden och den andra grupp-raden lämnas orörd.
## Dubbelklick till hÃ¶ger om grupptagg (2026-08-28)

Radmarkeringen fÃ¶r grupporsakens inline-editor raderas nu fÃ¶rst efter att
`setEditorData()` har anvÃ¤nt den. Qt kan annars skapa delegateditorn pÃ¥
nÃ¤sta event-loop-varv efter `table.edit()`, vilket gjorde att dubbelklick i
fritextdelen till hÃ¶ger om taggen tappade den valda primÃ¤r- eller
sekundÃ¤rraden.
## Grupporsak: två beständiga rader och radvis standardorsak (2026-08-28)

Grupporsakens editor följer nu enkelobjektets flöde. Primär- och sekundärrad
använder rätt utrustning när standardorsaker hämtas, och popupval commitas via
den aktiva P&ID-delegaten. En grupp sparar alltid båda taggarna på separata
rader; en ej vald händelse är kvar som bar tagg. När den ena raden ändras
bevaras den andra raden ordagrant, även om den innehåller fritext.
## Group cause editing: preserve both rows (2026-08-28)

The grouped cause path now mirrors the single-object standard-cause editor,
but selects the equipment type from the clicked visual row. Stored grouped
causes keep two tag-prefixed rows at all times. Changing one row updates only
that row; the other row, including arbitrary free text, is preserved verbatim.
## Grupporsak: robust dubbelklick till höger om tagg (2026-08-28)

Dubbelklickets radposition samlas nu in både från tabellens viewport och från
själva tabellen. Om Qt inte levererar positionen via eventfiltret används
musens globala position som reserv. Detta gör att dubbelklick i fritextdelen
fortsatt väljer primär eller sekundär rad och öppnar inline-editorn.

## Grupporsak: dubbelklick till höger om tagg öppnade fel popup (2026-08-28)

Trots gårdagens rad-/geometrilogik öppnade ett dubbelklick i fritextdelen av
EN grupporsaksrad ändå alltid Grupporsak-popupen (tagg-/objektväljaren) i
stället för inline-texteditorn, för både primär och sekundär rad. Orsaken:
`_on_cell_double_clicked`s fallback "orsaken saknar kopplat objekt ännu"
kontrollerade det gamla enkel-taggfältet (`obj_data`), som alltid är tomt för
en grupporsak eftersom dess identitet i stället ligger i den tvåradiga
`group_tags`-listan — så varje grupporsaksklick lästes felaktigt som
"inget objekt kopplat" och kortslöt till objektväljaren innan den redan
korrekta rad-/tag_hit-logiken ovanför någonsin nådde fram till att öppna
editorn. Villkoret kräver nu även `len(group_tags) < 2`, så en grupporsak
aldrig tar den vägen. Verifierat med ett nytt regressionstest som simulerar
en riktig dubbelklickposition (inte bara sätter `_group_edit_line` manuellt)
och som fallerar mot den gamla koden; `tests.test_smoke` och riktade
`tests.test_integration`-tester passerar. Ingen visuell GUI-verifiering är
gjord.
