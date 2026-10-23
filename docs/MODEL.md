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
| `lardomar.md` | alla | kontorets lärdomar om arbetet, sådant som inte kan vara en regel |

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
- **Bara Assistant pratar med dig.** Rollernas frågor till dig är frågor (#Q) som Assistant ställer med egna ord när det passar.

## Kalendern och städpassen

- **Kalendern** (`agent/due.py`) går var femte minut. När ett steg, ett experiment eller en fråga är dags väcks ägaren. Ett steg som Assistant äger och som ska tas upp med dig gör Assistant vid sitt datum, utom under nattro eller paus. Så når en plan dig, i stället för löften i chatten.
- **Ett steg till Researcher görs direkt.** Researcher väcks när steget skapas; datumet är när svaret behövs senast.
- **Ett steg kan vänta på något.** När det det väntar på blir klart är steget dags direkt.
- **En fråga utan svar** stängs efter en vecka. Det är också något att förstå, inte något att fråga om igen direkt.
- **Städpasset:** varje långsam körning får en granskning av det som är dags, har stannat, pågår eller väntar, uppfattningar som passerat sitt datum, texter som vuxit förbi sin storlek, och för Researcher varje källas avkastning. Rollen tar ställning till varje sak.

## Schemat (Managers)

Allt kontoret planerar att höra av sig om ligger i ett gemensamt schema med tider: utskick av fynd, knuffar före deadlines, påminnelser du har bett om, Assistants löften.

- **Dispatchern** är Managers snabba förmåga. När något nytt har sorterats placerar den det i schemat med en tidpunkt, i det sparade, eller släpper det.
- **Leveransen är Python, inte Claude.** När klockan slår görs en sista kontroll (nattro, paus, gäller saken fortfarande?). Sedan skriver Assistant meddelandet.
- **Bokning före löfte.** När du ber om något ("påminn mig fredag") bokar Assistant först och får direkt "bokat" eller en krock ("fredag 23:30 är nattro"). Vid en krock frågar Assistant dig innan den lovar något.
- **Dina bokningar ligger fast.** Manager kan föreslå en flytt, men flyttar aldrig i tysthet.
- **Det sparade** är en hylla utan tid, med bäst-före-datum. Du kan be om det ("något nytt?"), och Manager kan ta fram något därifrån när det passar.
- **Din kalender** (TimeEdit och Apple) är fakta om dig och läses men ägs inte.

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

## Director och Manager som utforskare

Båda prövar saker över tid med samma former. Director gör det för innehållet, Manager för upplevelsen.

- **Planer** är steg (#T) under det de tjänar, med ägare, datum och vad som räknas som klart.
- **Experiment** (#E) *förbättrar* det som finns eller *utforskar* något utanför de nuvarande målen. Ingen takt är hårdkodad; granskningen visar balansen.
- **Frågor** (#Q) ställs när ett svar skulle hjälpa mer än signalerna. Svaret går tillbaka till den som frågade.
- **Reflektionen** är en rutin som Director själv ändrar takten på.

## Möjligheter: en post per verklig sak

Varje annons, program eller event som kontoret hittar blir en möjlighet (#O) med sin exakta länk, och för varje uppgift (ort, sista dag, vem som får söka) står varifrån den kommer och om den är läst på sidan själv. Två annonser med samma rubrik för olika orter är två möjligheter; en kopia av samma annons på en annan sajt är samma. Sökuppdraget ser de möjligheter som finns och säger om varje fynd är samma, nytt eller oklart, och koden håller det mot adresserna. Oklart blir ett steg där Researcher reder ut det direkt. Det du satsar på (#S) tar länken och datumen från möjligheten, så en påminnelse eller ett svar i chatten bygger på det kontrollerade, inte på ett fynds rubrik. Det som inte är bekräftat följer med hela vägen och sägs ut.

Det byggdes efter 23 okt: ett sökuppdrag hade länkat Londonannonsen till fakta om Stockholmsannonsen, och förbehållet tappades på vägen.

## Bevakningar och sökuppdrag är rutiner

Varje bevakad källa och varje sökuppdrag är en rutin (#N) med en ägare (Researcher), ett syfte, en historik och en hälsa. Det som körs ändras direkt när en rutin ändras.

- **En källa som bara ger fel** pausar sig själv efter fem försök i rad, med felet som hälsa.
- **Sökfrågorna** är en del av sökuppdragets rutin. Ett sökuppdrag väljer bland beprövade och nya frågor; de som körts några gånger utan att något behållits visas för Researcher som värda att byta.
- **Platser sökningarna stöter på** blir anteckningar som Researcher kan göra till källor, steg eller kartanteckningar.
- **Täckning** prövas som ett experiment när Researcher misstänker en blind fläck: ett oberoende riktmärke jämförs med det som bevakas.

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

Allt som ska gå att följa upp har ett id: formerna (#S, #T, #E, #U, #Q, #G, #N), poster (#P), fynd (#F), bokningar (#B) och körningar (#R). Meddelanden i samtalet kopplas till de id:n de handlar om, och allt en körning skriver bär dess id. Assistant slår upp i stället för att minnas. Id:n är kökets verktyg och syns på webbsidan, inte i chatten. Rådatan finns under `/data/`.

## Återkoppling: hur kontoret blir bättre

- **Director → Researcher:** vad varje uppdrag gav och hur du reagerade blir feedback inför nästa planering.
- **Manager → dispatchern:** varje natt dispatcherns beslut med skäl och stickprov mot policyn. Missar ("varför sa du inte det här?", det du själv hämtade ur det sparade) kopplas till beslutet som höll inne dem.
- **Mått för Manager** räknas fram av Python och tolkas av Manager: svarstid, reaktioner, pauser, hämtningar och knuffar som följdes. Det är signaler, inte mål.
- **Lärdomar** som gäller hela kontoret står i `lardomar.md`.

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
