# Så tänker Audax

Status: modellen efter omdesignen, etapp 1–7 byggda (21 okt 2026); se "Från i dag till modellen" längst ner

Människor blir lätt blinda av sin egen osäkerhet, eller lever i en bubbla med en smal bild av sina vägar. Audax finns för att hjälpa dig ut ur båda: förstå vem du är och vad du vill, hitta det som passar dina intressen, mål och projekt, och pröva sådant du inte bett om. Audax föreslår. Den gör aldrig saker i ditt namn och skriver aldrig meddelanden åt dig.

Audax är ett litet kontor som arbetar för dig. Fyra roller har var sin fråga. De pratar med varandra genom en intern postgång, och bara en av dem pratar med dig.

## Vad en roll är

1. **En fråga den äger.** Varje bedömning i systemet tillhör exakt en roll.
2. **Förmågor: de stunder då den vaknar.** Långsamma förmågor tänker efter och planerar. Snabba förmågor tillämpar det rollen har bestämt, i stunden. Varje körning bär bara det underlag och de verktyg dess förmåga behöver.
3. **Det den äger:** former i databasen och texter i minnet. Varje körning är en ny instans som vaknar, läser sitt arbetsminne och sin inkorg, gör sitt jobb och skriver tillbaka. Kontinuiteten bor i det rollen äger, inte i en pågående session.

## De fyra rollerna

| | Assistant | Director | Researcher | Manager |
|---|---|---|---|---|
| **Frågan** | Vad säger du, och hur svarar jag? | Vad spelar roll för dig, och varför, och vad kan spela roll som du inte ser än? | Hur hittar vi det som finns, och vad missar vi? | Hur ska det kännas: när, hur mycket, hur det låter? |
| **Äger** | uppfattningarna ur det du sagt; samtalet; arbetsminne | bilden av dig (`person.md`), läget (`now.md`), målen och idéerna, planerna, experiment om innehållet; arbetsminne | sökuppdragen med sina sökfrågor, bevakningarna, sökkartorna, reglerna för källorna; arbetsminne | policyn, experiment om upplevelsen, schemat, det sparade; arbetsminne |
| **Långsamt** | nattpass: städar uppfattningarna, skriver samtalssammanfattningen, bokar löften | genomgång (varje natt), reflektion (en rutin, i början varannan dag) | planering (varje vecka, eller när ett steg, en beställning eller ett tyst uppdrag kräver det) | nattgranskning: läser samtalet som en UX-granskare och ser över schemat |
| **Snabbt** | samtalet, leveranser (med kort underlag), intervjun | triage: är det här värt något för dig? | sökuppdragen: letar brett och lämnar fynd | dispatchern: när ska det här nå dig? |
| **Gör inte** | bedömer inte relevans eller tidpunkt | bestämmer inte hur det letas, eller när du störs | bestämmer inte vad som är värt något | bestämmer inte vad som är relevant |

**Gränsen mellan Director och Researcher:** Director är uppdragsgivaren och säger *vad* som behövs och *varför* ("sommarpraktik inom mjukvara, inte bank, före februari"). Researcher äger *hur* och *var*.

1. **Beställning:** en post för ett bestående behov, eller ett steg för en fråga med datum ("kolla att Collide går att gå på som individ, till tisdag").
2. **Leverans:** Researcher letar brett och lämnar fynd, eller svarar på steget.
3. **Mottagningskontroll:** triage, Directors snabba bedömning av varje fynd.
4. **Återkoppling:** i genomgången ser Director vad varje uppdrag gav och hur du reagerade, och skickar feedback.

**Gränsen mellan Director och Manager:** Director äger fakta om läget ("tenta torsdag, flytt"). Manager läser dem och äger bara slutsatsen för din uppmärksamhet ("höj ribban lite, färre men bättre"). Läget höjer ribban men stänger aldrig av.

## Tre lager

Det som behöver vara stabilt är kod. Det kontoret själv bygger och ändrar är former i databasen. Det som är tolkning och ton är text.

**1. Koden** är grunden ingen roll kan ändra: formerna och deras kontroller, motorerna (kalendern, budgeten, reglerna före triage, den sista kontrollen före Telegram) och verktygen. Byggläget ändrar den när du ber om det.

**2. Formerna** är allt som är värt att följa. Allt har samma grund: id, form, ägare, status, vad det tjänar, källa, när det ses över, hur länge det gäller och en historik. Varje form har egna fält som koden kontrollerar (`agent/forms.py`, `agent/items.py`), och rollerna skriver med kontorets verktyg (`agent/office_tools.py`), som prövar varje skrivning mot formen och rollens rättigheter.

| Bokstav | Form | Vad det är |
|---|---|---|
| S | spår | mål, projekt, tillfälle, idé |
| T | steg | en del av en plan: ägare, när, klart när |
| E | experiment | något en roll prövar, för att förbättra eller utforska |
| U | uppfattning | något Audax tror om dig, med källa |
| Q | fråga | något en roll vill veta |
| G | regel | något koden gör utan Claude |
| N | rutin | återkommande arbete: en bevakad källa, ett sökuppdrag, en takt |
| O | möjlighet | en verklig annons, ett program eller ett event: exakt länk, ort, sista dag, vem som får söka, varifrån varje uppgift kommer |

Inget löper för evigt: steg, experiment och frågor har ett datum då de ses över igen.

**3. Texterna** är tolkningar, resonemang, ton och lärdomar (`agent/texts.py`). Tre principer:
- **Texten hänvisar, den kopierar inte.** En deadline eller något du sagt står i en form, med ett id, och upprepas inte.
- **Texten ändras i delar.** En roll ändrar ett avsnitt åt gången, så att inget försvinner i en omskrivning.
- **Varje text har en ägare och en ungefärlig storlek.** En text som växer förbi sin storlek hamnar i ägarens städpass.

| Text | Ägare | Vad den är |
|---|---|---|
| `direction/person.md` | Director | berättelsen om dig: vad som driver dig, mönster, hur bilden har ändrats |
| `direction/now.md` | Director | läget med egna ord; datumen läggs till av koden |
| `policy.md` | Manager | resonemanget om din uppmärksamhet och tonen; dina egna önskemål om stil ordagrant |
| `research/maps/` | Researcher | hur man söker inom ett område |
| `arbetsminne/<roll>.md` | varje roll | fokus, öppna hypoteser, väntar på, prövat och lärt |
| `lardomar.md` | koden | kontorets praxis: skrivs av koden ur uppfattningarna av sorten `practice` (#U), med vad varje sak vilar på |

**Profilen** (`profile.md`) är ingen text någon skriver: koden bygger den ur de uppfattningar som gäller just nu. Det är samma vy som webbsidan visar: det här vet Audax om dig, och varifrån.

## Principer

Det här är principer att bedöma efter, inte lagar. Rollerna väger dem mot läget.

- **Din uppmärksamhet är knapp.** Tystnad är standard, och det som når dig ska förtjäna sin plats. Det du inte bett om kommer med varför just det här.
- **Dina egna ord väger tyngst** och vinner även i en annan rolls område. Ägaren av området bygger vidare på dem.
- **Man ändrar sig.** En tvekan stänger inget; ett beslut gör det. Rollerna kan komma tillbaka till något varsamt, men är aldrig envisa.
- **Fråga när det är oklart.** Det är alltid okej att fråga.
- **Pröva, och bestäm.** Experiment slutar i ett beslut. Ingen manipulation, och frågar du om något var ett test får du ett ärligt svar.
- **Sanning.** Ingen roll påstår en handling den inte gjort.
- **Sparsamhet.** Varje körning använder din prenumeration.

## Rättigheter: alla läser, ägaren skriver

- Varje form och varje text har en ägare. Bara ägaren skriver, och rättigheterna i koden följer ägarskapet. Promptarna är inte det enda skyddet.
- Alla roller kan läsa allt. Varje förmåga får ett utvalt underlag, och resten kan den slå upp.
- **Dina ord vinner.** Det du uttryckligen säger gäller direkt, via Assistant, även i en annan rolls område: "sluta bevaka X" eller "jag har sökt Y". Ägaren får en post och bygger vidare.

## Postgången

All kommunikation mellan rollerna går genom en central postlogg (#P). Varje rolls inkorg är det i loggen som är adresserat till den.

- **Kan vänta** är det vanliga. Mottagaren läser posten vid sin nästa körning och markerar den som behandlad.
- **Väck** startar mottagarens körning snart, bara när kedjan började med dig, eller när något brådskande väcker framåt mot dig (Researcher → triage → dispatcher → Assistant). Ingen kan väcka bakåt, så loopar kan inte uppstå.
- **Order gäller till ett datum.** En order (i början två veckor om inget annat sägs) syns både hos den som gav den och den som arbetar under den, tills den löper ut eller återkallas. En roll som får en order som stoppar arbetet säger ifrån.
- **Bara Assistant pratar med dig.** Rollernas frågor till dig är frågor (#Q) som Assistant ställer med egna ord när det passar.

## Kalendern och städpassen

- **Kalendern** (`agent/due.py`) går var femte minut. När ett steg, ett experiment eller en fråga är dags väcks ägaren. Ett steg som Assistant äger och som ska tas upp med dig går vid sitt datum till desken som ett ärende (sorten `steg`). Desken väljer stunden, inte om det ska fram, och steget är klart när meddelandet gått ut. Så når en plan dig, i stället för löften i chatten. Det som infaller samtidigt, till exempel en påminnelse du bokat och ett fynd, går ut som ett meddelande.
- **Ett steg till Researcher görs direkt.** Researcher väcks när steget skapas; datumet är när svaret behövs senast.
- **Ett steg kan vänta på något.** När det det väntar på blir klart är steget dags direkt.
- **En fråga utan svar** stängs efter en vecka. Det är också något att förstå, inte något att fråga om igen direkt.
- **Städpasset:** varje långsam körning får en granskning av det som är dags, har stannat, pågår eller väntar, uppfattningar som passerat sitt datum, texter som vuxit förbi sin storlek. För Researcher blir en svag källa en punkt; alla källors avkastning hämtas med `sources`. Rollen tar ställning till varje sak.
- **Att avgöra nu** (omgång 16, `agent/docket.py`): det som kräver ett ställningstagande är en *punkt*, inte en rad i genomgången. En punkt är en sak, med underlaget inskrivet, och den ligger kvar tills rollen svarar med verktyget `answer` (gjort, ändrat, behållet, struket, inget, senare med en dag, eller en lärdom). En obesvarad punkt kommer tillbaka räknad, och efter tre körningar ser Manager den. En punkt vars sak klarnar av sig själv stängs av koden. Punkterna kommer i genomgångens början, de med sista dag först, inom en budget; det som inte ryms väntar räknat. En punkt med många rader att bedöma (hänvisningar, arbete bredvid ägarens ändrade ord) numrerar dem, och svaret måste namnge numren som inte håller, eller inga: ett `behållet` med en rad som inte håller går inte igenom, och ett `ändrat` säger vilken rad. Så kan inte en rad som säger emot sin källa glida förbi i ett samlat svar. Koden tar upp punkter där något händer (`agent/points.py`):
  - **en bevakad post går inte att läsa** (Researchers, och den stängs när posten läses igen);
  - **ägarens egna ord ändras** medan en roll har arbete som gör något för ägaren (uppdrag, bevakningar, regler, mål, experiment) eller egna tolkningar och antaganden om samma del av ägarens liv: en öppen punkt per roll samlar ändringarna, de senaste först, med arbetet och tolkningarna bredvid. Assistant, som håller ägarens ord, ser också ägarens äldre ord om samma del av livet och avgör om ett nytt ord ersätter ett gammalt; annars står båda och binder. Den hänger inte på att orden matchar ("säkerhet" hittar inte en fråga om "cybersecurity");
  - **ett nästa steg har passerats**: det nämner något som avslutats, eller bara dagar som passerat. En gång per sådant läge;
  - **en rad i en roll texter hänvisar** till en uppfattning eller till något avslutat: raden ställs bredvid det den hänvisar till, och ägaren av texten avgör om den säger samma sak. En avstämd rad frågas inte igen förrän den ändras;
  - **något avslutas** (ett experiment avgörs, ett mål eller uppdrag tar slut, en regel eller bevakning stryks, en efterlysning avslutas, ett rapporterat fel löses): vad lärde vi oss?
  - **ett fält har vuxit förbi sin storlek**: skriv om till hur det står nu.

## Schemat (Managers)

Allt kontoret planerar att höra av sig om ligger i ett gemensamt schema med tider: utskick av fynd, knuffar före deadlines, påminnelser du har bett om, Assistants löften.

- **Dispatchern** är Managers snabba förmåga. När något nytt har sorterats placerar den det i schemat med en tidpunkt, i det sparade, eller släpper det.
- **Leveransen är Python, inte Claude.** När klockan slår görs en sista kontroll (nattro, paus, gäller saken fortfarande?). Sedan skriver Assistant meddelandet.
- **Bokning före löfte.** När du ber om något ("påminn mig fredag") bokar Assistant först och får direkt "bokat" eller en krock ("fredag 23:30 är nattro"). Vid en krock frågar Assistant dig innan den lovar något.
- **Dina bokningar ligger fast.** Manager kan föreslå en flytt, men flyttar aldrig i tysthet.
- **Det sparade** är en hylla utan tid, med bäst-före-datum. Du kan be om det ("något nytt?"), och Manager kan ta fram något därifrån när det passar.
- **Din kalender** (TimeEdit och Apple) är fakta om dig och läses men ägs inte. Den är underlag för innehåll (deadlines, din vecka), ingen spärr: ett pass håller inte tillbaka ett meddelande, och Audax frågar inte vilka pass som gäller dig.

## Kontakten: underlag och omdöme

**Ett meddelande är en notis** (omgång 12). Du läser det när det passar, så stunden betyder lite och innehållet mest. Det som kostar är innehåll som inte passar, och en sådan miss rättas i urvalet (Director och triagen), inte genom färre meddelanden. Desken avgör om och när något når dig: är det bra hör Audax av sig. Bara nattron och dina egna ord binder. Policyn har inga tak eller antal. **Osäkert betyder kolla:** saknar ett lovande fynd en avgörande uppgift (datum, om det är öppet, hur man anmäler sig) namnger triagen eller desken den, och en kort körning (`agent/checks.py`) läser sidan och söker vid behov. Fyndet kommer tillbaka till desken med det som bekräftats, en gång; det som fortfarande är oklart går ut sagt som oklart eller släpps med skäl. Hyllan är bara för det ljumma. När något ändå går ut kan något från hyllan följa med, och det är hyllans väg till dig.

**Senast nyttig** (omgång 14, `agent/timing.py`). Den som hittar något anger när du senast måste veta det och varför. Det är inte sista dagen i sig, utan sista dagen minus det du behöver för att hinna: några dagar för ett event med godkännande eller väntlista, en kväll för en ansökan. Triagen anger det, kontrollen rättar det och en knuff utgår från sin deadline. Brådska är ingen nivå längre utan räknas fram ur tidpunkten och nuet:
- desken kan inte planera efter tidpunkten; en plan efter den flyttas dit (kvällen innan, om tidpunkten faller i natten);
- det du bett om att bara få det viktiga, eller dina egna antal och tider, håller bara kvar det som kan vänta förbi tidpunkten; nattron och "tyst" håller alltid;
- det som ligger på hyllan eller är planerat för sent kommer tillbaka till desken en gång när tidpunkten är ett dygn bort;
- det som hölls tills tidpunkten passerat räknas som deskens fel ("lät tiden gå") och i facit mot uppfattningarna desken lutade sig mot;
- reserven, när Claude är borta, skickar det du behöver veta inom tolv timmar.

Det är en utlösare, ingen garanti. Bäst-före (`expires_at`) finns kvar som gränsen för när saken inte är värd något alls.

**Ett omdöme är lika gammalt som det det bedömdes mot** (`agent/pipeline/rejudge.py`). Triagen bedömer ett fynd en gång, mot målen, `now.md` och profilen som de såg ut den dagen. Innan ett fynd, en insikt eller ett steg når dig jämför koden därför när det bedömdes med när det triagen läser senast ändrades: dina egna ord (uppfattningar med dig som källa), `now.md`, profilen och praxis. Bedömdes det före ändringen tittar triagen på det en gång till (knuffar och schemaändringar undantas, eftersom de skapas ur fakta när de blir aktuella), och behåller det eller släpper det med ett skäl. Det du själv har sagt om något liknande slås upp i fulltextindexet (`agent/search.py`) per fynd i stället för att hela historiken skickas med, så prompten växer med antalet fynd, inte med historiken. Bakgrunden: OpenAI-storyn behölls 1 nov, du sa "redan läst" 4–5 nov, den kom tillbaka från hyllan till desken (som bedömer när, inte vad) och gick ut en tredje gång 10 nov.

**Kontaktliggaren** (`agent/ledger.py`) sparar varje egen kontakt med sitt sammanhang: vilken sort, tid, längd, hur många saker den samlade och vilken i ordningen den dagen den var. Den sparar också hur kontakten landade: svar, reaktioner och "för mycket". Ur det räknar koden fram tre saker:
- hur olika sorters kontakter landar;
- deskens fel åt båda hållen: det som hölls tillbaka men som du sedan hämtade eller frågade efter, eller som hölls tills tiden gått, och det som skickades men som du inte ville ha;
- vad som aldrig prövats.

Desken och Manager bedömer utifrån det. Det är underlag och inga mål.

**Inställningarna** (`agent/assumptions.py`) är `quiet_hours`, `daily_contacts`, `bundle_times` och `message_length`. Var och en är en uppfattning om dig med källa. Bara det du själv sagt binder. Nattron binder också, eftersom den skyddar din natt, tills du säger något annat. Allt annat är kontorets förståelse, som vägleder desken utan att vara en gräns. Manager flyttar förståelsen när liggaren pekar dit, eller när den vill lära sig något som aldrig prövats, och frågar dig rakt ut när ett svar säger mer. Se `instance/granskning/2026-11-04-omgang-11-kontakten.md`.

## Kontorets tankar

Director, Researcher och Manager kan var och en lägga en tanke till dig (`agent/thoughts.py`). Det kan vara något de märkt, en idé, en plan som tagit form, vad de håller på med eller en gissning om vad du vill. Ibland bär tanken en direkt fråga för att lära sig något. Den formuleras aldrig som "säg till om …".

Kravet är detsamma som för fynd: du ska bli glad att höra det, även om inget följer av det. Desken bedömer en tanke som allt annat, och Assistant säger den med kontorets enda röst ("Jag har märkt att …"). Varje roll ser hur dess egna tankar landade.

Ett fynd som triagen är osäker på bär med sig vad osäkerheten gäller, och då kan meddelandet sluta med en direkt fråga.

## Från fynd till dig

```
Nytt fynd → regler (koden) → triage (Director: värt något?) → dispatcher (Manager: när?) → schemat → Assistant skriver
```

- **Koden före Claude** (`agent/rules.py`): dubbletter (samma adress eller rubrik som ett fynd de senaste 30 dagarna, från vilken källa som helst), det som redan passerat sitt datum, och det rollernas regler (#G) säger, sorteras bort innan triage frågar Claude. Varje regel räknar sina träffar.
- **Triage sorterar i omgångar**, ungefär en gång i timmen. En källa du själv har bett om är brådskande och sorteras direkt.
- **Hyllan har bäst-före-datum:** nyheter två veckor, möjligheter och event en månad.
- **Direkt, förbi schemat:** allt du själv tar initiativ till. Svar när du skriver, resultat av en sökning du bett om, byggläget och larm om att något är trasigt.

## Rösten

Assistant är det enda du möter, och skriver allt du läser.

- **Bara svaret går ut.** Det som skickas är chattens färdiga meddelande, aldrig text mellan verktygsanropen, och en sista kontroll före Telegram tar bort kontorets interna id:n.
- **Leveranser skrivs för sig**, med ett kort underlag: de senaste meddelandena, vad som ska fram och varför, profilen och Managers policy. Nästa gång du skriver får samtalet en rad om vad som skickats och vad det gällde.
- **Läget läses, inte minns.** Verktyget `situation` visar vad rollerna senast gjorde, de planerade stegen med datum, vad som väntar på budgeten, vad som är bokat och vad som skickats i dag. Chatten säger inget om bakgrunden som den inte läst där.
- **Löften är bokningar eller steg.** "Jag hör av mig i morgon" är en bokning; "jag hör av mig när planen är klar" är ett steg som väntar på planen. Utan något sådant ger chatten inget löfte.
- **Samtalet hålls kort.** När det vuxit förbi sitt fönster (i början 30 000 tokens) och varit tyst en stund viks det ihop till en sammanfattning, och nästa meddelande börjar en ny session från den, de senaste meddelandena och det sökningen hittar.

## Minnet av dig

- **Det du säger om dig själv** blir uppfattningar (#U) med ditt meddelande och dina ord som källa. En ny ersätter en äldre, och den gamla finns kvar i historiken, så det syns när du ändrade dig. Det som bara gäller ett tag har ett slutdatum. En tvekan sparas som en tvekan.
- **Directors slutsatser** är också uppfattningar: mönster, värden och hypoteser, med belägg. Tre markeringar gör bubblan synlig: *utesluter*, *kanske en begränsning* och *aldrig övervägt*.
- **Sökningen** (`agent/search.py`) går över samtalet, posten, fynden och formerna, klarar böjningar och visar vem som sa vad och när. Före varje svar söker koden på det du skrev och lägger med det relevanta.

## Trådar: när Audax hör av sig

Audax hör av sig själv, men inte efter ett schema. Allt en roll bedömer viktigt för sitt arbete kan den följa: ett mål, en uppfattning om personen, en hypotes, ett experiment. Att följa något betyder att rollen själv har sagt när den vill se på det igen och varför, utifrån vad den vet (till exempel "inget att fråga förrän i december, när sommarjobben öppnar"). Saken kommer upp då, eller tidigare om något nytt händer i den: personen nämner den, en annan roll ändrar den, eller en deadline närmar sig.

En roll kan också följa en **sparad fråga** (omgång 16), till exempel "möjligheter med deadline inom 21 dagar". Den byggs av samma ord som `find`, körs en gång när den sparas så att rollen ser vad den ger, och det den hittar visas i varje körning. Det är rollens sätt att forma vad den ser, utan kod.

Tråden är vyn som koden bygger kring saken: dess historia, vad personen sagt om den, vad som hör ihop med den, varje gång kontoret tagit upp den och hur det landade, och det som väntar i världen. Med den avgör rollen själv om något är värt att fråga, vilken vinkel, och om det redan frågats. Svar gör bilden aktuell, tystnad låter trådarnas egna datum komma, och obesvarade kontakter syns, så balansen uppstår ur förståelsen i stället för ur en takt.

Frågorna till personen går via Assistant. När personen skriver tar chatten upp den som passar; när det är tyst sprider Managers desk ut dem, en kontakt i taget och vid naturliga tillfällen. Manager ser varje kontakt och hur den landade, och avgör i ord hur ofta Audax ska höra av sig till just den här personen just nu.

## Director och Manager som utforskare

Båda prövar saker över tid med samma former. Director gör det för innehållet, Manager för upplevelsen.

- **Planer** är steg (#T) under det de tjänar, med ägare, datum och vad som räknas som klart.
- **Experiment** (#E) *förbättrar* det som finns eller *utforskar* något utanför de nuvarande målen. Ingen takt är hårdkodad; granskningen visar balansen.
- **Frågor** (#Q) är allt en roll vill veta och som spelar roll: vad som är oklart, vad som hänger på det, rollens bästa gissning och vad som skulle avgöra saken. Rollen väljer var svaret kommer ifrån: från dig (Assistant frågar när det passar), från en annan roll, från världen (Researcher tar reda på det direkt) eller från rollen själv (ett experiment eller en signal att vänta på). Svaret går tillbaka till den som frågade, bredvid gissningen, och den skriver vad det betydde. Chatten kan också lämna något du sa som pekar åt flera håll hos Director, i stället för att gissa. Inga frågor tvingas fram.
- **Reflektionen** är en rutin som Director själv ändrar takten på.

## Möjligheter: en post per verklig sak

Varje annons, program eller event som kontoret hittar blir en möjlighet (#O) med sin exakta länk, och för varje uppgift (ort, sista dag, vem som får söka) står varifrån den kommer och om den är läst på sidan själv. Två annonser med samma rubrik för olika orter är två möjligheter; en kopia av samma annons på en annan sajt är samma. Sökuppdraget ser de möjligheter som finns och säger om varje fynd är samma, nytt eller oklart, och koden håller det mot adresserna. Oklart blir ett steg där Researcher reder ut det direkt. Det du satsar på (#S) tar länken och datumen från möjligheten, så en påminnelse eller ett svar i chatten bygger på det kontrollerade, inte på ett fynds rubrik. Det som inte är bekräftat följer med hela vägen och sägs ut.

Det byggdes efter 23 okt: ett sökuppdrag hade länkat Londonannonsen till fakta om Stockholmsannonsen, och förbehållet tappades på vägen.

## Bevakningar och sökuppdrag är rutiner

Varje bevakad källa och varje sökuppdrag är en rutin (#N) med en ägare (Researcher), ett syfte, en historik och en hälsa. Det som körs ändras direkt när en rutin ändras.

- **En källa som bara ger fel** pausar sig själv efter fem försök i rad, med felet som hälsa.
- **Sökfrågorna** är en del av sökuppdragets rutin. Ett sökuppdrag väljer bland beprövade och nya frågor; de som körts några gånger utan att något behållits blir en punkt hos Researcher, numrerade, att byta ut.
- **Vad ett sökuppdrag vet** (omgång 16, etapp 2): dina ord inom sökningens områden (intressen, möjligheter, det du inte vill ha, skolan, gränserna), med dina egna ord först; de mål det tjänar i sin helhet (varför, vad som hjälper och vad som är brus) och de andra en rad var; och de möjligheter det självt hittat först. Ett mål tjänas när målet nämner uppdraget eller uppdraget nämner målet (#S); ett uppdrag som inte tjänar något mål blir en punkt.
- **Researchers planering** får det den ska avgöra som punkter med underlaget inskrivet: en källa som brusar eller inte ger något (med vad den gav senast), ett uppdrag vars frågor inte ger något eller som är tyst, och ett testfall sökningen missade. Uppdragen står en rad var och källorna inte alls; resten hämtas med `mission` och `sources`. Arbetsminnet är kvar men högst 1 500 tecken.
- **Platser sökningarna stöter på** blir anteckningar som Researcher kan göra till källor, steg eller kartanteckningar.
- **Täckning** prövas som ett experiment när Researcher misstänker en blind fläck: ett oberoende riktmärke jämförs med det som bevakas.

## Efterlysningar: utred innan du bevakar

När ägaren ber Audax hålla utkik efter något utan att ge en adress, till exempel en tävling, en utlysning, ett evenemang eller nyheter om något, blir det en efterlysning (#J). Samma sak gäller när ägaren hittat något själv som Audax borde ha fångat. Det är en miss.

Researcher utreder den först i en egen körning:
- vad saken heter, eftersom ägarens ord sällan är världens;
- vem som står bakom den;
- tidigare omgångar, med datum och adress;
- om den återkommer, och i så fall när;
- var den annonseras.

Utifrån det väljs bevakningar. Koden prövar varje bevakning mot de tidigare omgångarna: en sida som inte nämner saken, eller en nyhetssökning som inte hade hittat förra omgången, underkänns.

Att inget hittas är en fråga och inget svar. En efterlysning väcks igen:
- när dess fönster närmar sig;
- när bevakningarna länge varit tysta;
- efter en miss;
- när ägaren svarat på en följdfråga.

Ägaren kan fråga vad Audax letar efter. Chatten säger aldrig "finns inget" efter en sökning.

## Testfallen: hur bra sökningen är

Det som visat sig spela roll samlas av koden som testfall:
- tidigare omgångar som efterlysningarna hittat;
- missar ägaren hittat själv;
- det ägaren gått vidare med;
- det ägaren hämtat från hyllan;
- det ägaren gillat.

För varje fall räknar koden ut hur Audax fick det:
- **hittat:** en källa hittade det och det sparades;
- **sett men släppt:** en fråga om bedömning, som Director äger;
- **aldrig sett:** en blind fläck i sökningen;
- **före:** det hände innan Audax bevakade något.

Researcher ser andelen som sökningen hittade, vilken sorts metod som hittade det, och de fall den inte hittade. Ett nytt fall den inte hittat väcker Researcher. Sökningen är ett pågående projekt: ett metodförsök i taget, riktat mot den svagaste punkten, med testfallen som mått. Arbetsminnet är labbjournalen.

**Bevakat betyder att det fungerar** (omgång 15, `agent/watching.py`). En bevakning med flera källor i en, i dag bevakningslistan med bolagens rekryteringssystem, har hälsa per post: när den senast lästes utan fel och sedan när den fallerar. Researcher ser de poster som inte går att läsa, väcks när en ny börjar fallera, och rättar adressen (`remove_company` och sedan `add_company`) eller stryker posten. En bevakning som fallerar i sin helhet pausar sig själv som förut. En oläslig bevakningslista är ett hälsolarm till förvaltaren, och ett bolag läggs till i filens egen form och läses tillbaka innan det sparas.

**Uppdragens egna texter åldras** som fynd gör (omgång 14). Ett aktivt uppdrag vars mål och frågor skrevs innan ägarens egna ord ändrades visas en gång i Researchers genomgång, bredvid orden som ändrats sedan dess, och väcker Researcher. Det behålls, skrivs om eller får en fråga struken.

## Budgeten

Audax andel av din veckokvot sätts löst, i början 10 % (`agent/budget.py`). Varje förmåga har en nivå. Samtalet, det du bett om och det billiga flödet väntar aldrig. Rollernas vanliga arbete väntar när veckans andel är använd, det tunga och utforskande när dagens. Det som väntar syns i läget och på webbsidan.

## Natten: ett svep i ordning

En kedja som startar när du med störst sannolikhet sover, räknat ur när du brukar skriva sist och nattron.

1. **Assistant** städar uppfattningarna, skriver samtalssammanfattningen och bokar sina löften.
2. **Director** gör sin genomgång, eller reflektion när den är dags.
3. **Researcher** planerar, om något kräver det.
4. **Manager** granskar gårdagen och ser över schemat med den färska bilden.

Tar kvoten slut stannar kedjan och fortsätter där den slutade.

## Körfält och köer

- **Två körfält:** ett för samtalet och leveranserna (det får aldrig vänta på bakgrundsarbete) och ett för allt annat.
- **En instans per roll åt gången.** Snabba förmågor kan köras samtidigt som rollens långsamma.

## ID:n och uppslag

Allt som ska gå att följa upp har ett id: formerna (#S, #T, #E, #U, #Q, #G, #N, #J), poster (#P), fynd (#F), bokningar (#B), körningar (#R) och punkter att avgöra (D). Meddelanden i samtalet kopplas till de id:n de handlar om, och allt en körning skriver bär dess id. Assistant slår upp i stället för att minnas. Id:n är kökets verktyg och syns på webbsidan, inte i chatten. Rådatan finns under `/data/`.

**Spåra en handling** (`agent/trace.py`, `./deploy/audax trace <id>`): ett meddelande, ett fynd, en post, en bokning, en punkt, en körning eller en rad i en text följs bakåt: körningen som gjorde det, de rader i dess prompt som handlade om saken, vad den gjorde, och varifrån det den lutade sig mot kom (vilken körning som skrev det, när och varför). Ett fel namnges sedan efter sin orsak: hur prompten byggdes, vad arkitekturen sparar, eller ett tidigare misstag som fördes vidare. Triagens och deskens prompter sparas därför också.

**Läget och historiken** (omgång 16): en posts fält säger hur det står nu, och formulären har en storlek för de fält som annars växer (ett måls "varför" högst 400 tecken). Den som skriver över en text måste ge en anteckning, och den gamla texten sparas i postens historik. **Länkarna gör koden:** varje id som en post nämner i sina fält blir en länk (`mentions`), så "allt om X" fungerar utan att någon roll behöver länka.

## Återkoppling: hur kontoret blir bättre

- **Director → Researcher:** vad varje uppdrag gav och hur du reagerade blir feedback inför nästa planering.
- **Manager → dispatchern:** varje natt dispatcherns beslut med skäl och stickprov mot policyn, med de uppfattningar varje beslut lutade sig mot. Missar ("varför sa du inte det här?", det du själv hämtade ur det sparade, det som hölls tills tiden gått) kopplas till beslutet som höll inne dem.
- **Director → triagen** (omgång 14, `agent/crosscheck.py`): triagen är Directors snabba förmåga, och det den släpper ser du aldrig, så testfallen kan inte visa det. Directors dagliga genomgång får därför ett stickprov av vad triagen gjort sedan sist: några släppta fynd slumpvis, alla släppta som lutade sig mot en uppfattning som sedan ändrats eller strukits, och det den behöll som du inte ville ha. Ett felaktigt släppt fynd som fortfarande är aktuellt går tillbaka till desken och räknas i facit mot det triagen lutade sig mot. Rättelsen görs där triagen läser, i `now.md` och målen.
- **Researcher → triagen:** Researcher ser triagens skäl för det dess egna uppdrag hittade och som släpptes, och ifrågasätter dem med en post till Director när de ser fel ut.
- **Mått för Manager** räknas fram av Python och tolkas av Manager: svarstid, reaktioner, pauser, hämtningar och knuffar som följdes. Det är signaler, inte mål.
- **Lärdomar** är uppfattningar av sorten `practice`; `lardomar.md` är vyn av dem. När något avslutas frågar en punkt vad det lärde (se Kalendern och städpassen), och svaret blir en lärdom länkad till det den lärdes av. När en roll skapar något som prövar ett arbetssätt (ett experiment, en regel, en rutin, en efterlysning, en idé) letar koden upp det som liknar det och redan prövats: avslutade poster av samma slag med vad det blev, och lärdomarna. Det kommer med i verktygets svar (`agent/lessons.py`), så samma misstag inte görs omedvetet igen.
- **Mätning** (`agent/measure.py`, `./deploy/audax prompts`): per sorts körning promptens storlek, hur många id den visade och hur många körningen använde, hur ofta den hämtade, punkternas utfall och hur ofta "tidigare prövat" visats.
- **Levande kunskap** (omgång 13): allt som styr vilar på en uppfattning, och en uppfattning på sina observationer (daterade rader). Koden visar överallt vad den vilar på ("vilar på 3 observationer, 3–5 nov", "ägarens egna ord", "ett antagande"), och liggaren jämför senaste veckan med ägarens egen baslinje. Före varje körning visar genomgången hänvisningar till sådant som stängts och rader i policyn och `now.md` som styr utan att vila på något. Inget faller för att tiden går; det faller när underlaget inte håller. **Facit** (`agent/facit.py`): när triagen eller desken lutar sig mot en uppfattning namnger de den (`rests_on`), och koden följer vad det blev: det som hölls tillbaka och sedan hämtades eller efterfrågades, det som skickades och besvarades eller inte var önskat. En uppfattning vars facit pekar emot den sedan den sist sågs över kommer upp till prövning hos sin ägare: behåll med skäl, ändra eller släpp. Vilken roll som helst kan ifrågasätta en annans uppfattning med en post. Du kan fråga varför Audax gör något och få uppfattningen, vad den vilar på och dess facit; stryker du den får rollen veta det, och det binder.

## Utanför kontoret

- **Infrastruktur utan omdöme:** sinnen och sidläsning, förfilter, kalendersynk, hälsovakt, säkerhetskopia och leverans enligt schemat.
- **Byggläget** är en extern konsult. Det ändrar koden när du ber om det, har inget minne, och ingenting går live förrän du har godkänt det med en knapp.

## Från i dag till modellen

Omdesignen i oktober 2026 byggdes i sju etapper, var och en driftsatt för sig. Designen och problemlistan finns i instansens granskningsmapp.

1. **Kärnan:** en tabell för allt som är värt att följa, med former som koden kontrollerar.
2. **Uppfattningar och minne:** det du säger sparas med källa, profilen är en vy, sökningen hittar det som sagts.
3. **Förmågor, budget och gränsvakt:** varje körning bär bara sina verktyg, budgeten fördelar efter nivå, inget internt når Telegram.
4. **Director och Manager som utforskare:** planer i steg, experiment, frågor, kalendern och städpassen.
5. **Flödet:** bevakningar och sökuppdrag som rutiner, regler före Claude, sortering i omgångar, hylla med bäst-före-datum.
6. **Rösten:** leveranser med kort underlag, läget, löften som håller, ett kort samtal.
7. **Texten:** texter som ändras i delar med ägare och storlek, en gemensam CLAUDE.md med kortare rollprompter, sökfrågorna i uppdragen, kontorssidan visar formerna.

Kvar att pröva i drift: att Director lägger planer i steg och att Manager och Director frågar och experimenterar i den takt läget kräver.
