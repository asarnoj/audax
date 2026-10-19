# Så tänker Audax

Status: beslutad modell, 13 okt 2026 · etapp 1–4 byggda (grunden, schemat, Researcher, natten); se "Från i dag till modellen" längst ner

Audax är ett litet kontor som arbetar för dig. Fyra roller har var sitt ansvar och var sin anteckningsbok. De pratar med varandra genom en intern postgång, och bara en av dem pratar med dig.

## Vad en roll är

En roll definieras av tre saker:

1. **En fråga den äger.** Varje bedömning i systemet tillhör exakt en roll.
2. **Anteckningar som är dess minne och dess "jag".** Varje körning är en ny instans som vaknar, läser sina anteckningar, gör sitt jobb och skriver tillbaka. Kontinuiteten bor i anteckningarna, inte i en pågående session.
3. **Uppgifter: de stunder då den vaknar.** Långsamma uppgifter tänker efter och skriver anteckningar. Snabba uppgifter tillämpar rollens anteckningar i stunden och skriver bara sina beslut, aldrig i anteckningarna.

En ny roll behövs bara när en ny fråga behöver sin egen anteckningsbok och kan krocka med de andra.

## De fyra rollerna

| | Assistant | Director | Researcher | Manager |
|---|---|---|---|---|
| **Frågan** | Vad säger du, och hur svarar jag? | Vad spelar roll för dig, och varför? | Hur hittar vi det som finns, och vad missar vi? | Hur ska det kännas: när, hur mycket, hur det låter? |
| **Anteckningar** | minnet om dig (det du har sagt): profilen, med skola, intressen, vad som får störa, schema och vanor; arbetsminne | bild av dig, läget (fakta), målen och idéerna (#S); arbetsminne | sökuppdragen, sökkartor, frågeportföljer, ledtrådar, täckning, bevakningarna; arbetsminne | policyn (läget för din uppmärksamhet, tempo, hur chatten skriver), experiment, schemat, det sparade; arbetsminne |
| **Långsamt** | nattpass: rensar och slår ihop minnet, skriver sitt arbetsminne och samtalssammanfattningen | genomgång (varje natt), reflektion (tre nätter i veckan) | planering (varje vecka, eller när ett uppdrag tystnar eller en ny beställning kommer) | nattgranskning: läser samtalet som en UX-granskare och planerar dagens schema |
| **Snabbt** | samtalet, utskick, intervjun | triage: är det här värt något för dig? | sökuppdragen: letar brett och lämnar fynd | dispatchern: när ska det här nå dig? |
| **Gör inte** | bedömer inte relevans eller tidpunkt | bestämmer inte hur det letas, eller när du störs | bestämmer inte vad som är värt något | bestämmer inte vad som är relevant |

**Gränsen mellan Director och Researcher:** Director är uppdragsgivaren och säger *vad* som behövs och *varför* ("sommarpraktik inom mjukvara, inte bank, före februari"). Researcher äger *hur* och *var*: sökfrågor, bevakningar, kartor och täckning. Arbetsrelationen ser ut så här:

1. **Beställning:** Director skickar en post.
2. **Leverans:** Researcher letar brett och lämnar fynd. Researcher frågar "kan det här spela roll?".
3. **Mottagningskontroll:** triage, Directors snabba bedömning av varje leverans. Triage frågar "spelar det roll?".
4. **Återkoppling:** i genomgången ser Director vad varje uppdrag gav och hur du reagerade, och skickar feedback till Researcher.

**Gränsen mellan Director och Manager:** Director äger fakta om läget ("tenta torsdag, flytt"). Manager läser dem och äger bara slutsatsen för din uppmärksamhet ("höj ribban lite, färre men bättre"). Läget höjer ribban men stänger aldrig av.

## Arbetsminnet

Varje roll har ett kort arbetsminne som skrivs om vid varje långsam körning och läses först vid nästa:

- **nuvarande fokus**
- **öppna hypoteser** och vad som skulle pröva dem
- **vad den väntar på** (svar, datum, poster)
- **vad den har prövat** och vad den lärde sig

Det är det som gör Director till samma varelse från natt till natt. Varje körning (#R) sparar dessutom en kort redogörelse för vad den gjorde och varför. Den är historik för dig och webbsidan och matas inte tillbaka till rollerna; några loggfiler finns inte.

## Rättigheter: alla läser, ägaren skriver

- Varje fil och varje tabell har en ägare. Bara ägaren skriver, och rättigheterna i koden följer ägarskapet. Promptarna är inte det enda skyddet.
- Alla roller kan läsa allt. Varje uppgift får ett utvalt sammanhang, och resten kan den slå upp själv.
- **Dina ord vinner.** Det du uttryckligen säger gäller direkt, via Assistant, även i en annan rolls område: "sluta bevaka X" eller "jag har sökt Y". Ägaren får en post och bygger vidare, men kör inte över det du har sagt.

## Postgången

All kommunikation mellan rollerna går genom en central postlogg. Varje rolls inkorg är "det i loggen som är adresserat till mig".

- **Post:** en anteckning eller en förfrågan, med avsändare, mottagare, ID och, när det gäller, dina exakta ord.
- **Kan vänta** är det vanliga. Mottagaren läser posten vid sin nästa ordinarie körning och markerar den som behandlad, med en notis om vad den gjorde.
- **Väck** startar mottagarens körning snart. Det är tillåtet i två fall:
  - när kedjan började med dig (du sa något),
  - när något är brådskande och väcker **framåt mot dig** (Researcher → triage → dispatcher → Assistant).

  Ingen kan väcka bakåt, så loopar kan inte uppstå.
- Rollerna väcker aldrig varandra direkt. De lägger post, och motorn väcker.
- **Bara Assistant pratar med dig.** Frågor från de andra rollerna är poster till Assistant.

## Schemat (Managers)

Allt kontoret planerar att höra av sig om ligger i ett gemensamt schema med tider:

- utskick av fynd,
- knuffar före deadlines,
- påminnelser du har bett om,
- frågor till dig,
- idéer och lägesrapporter,
- avstämningar,
- Assistants löften.

- **Andra roller begär en tid eller ett tidsfönster** ("knuff om Google, helst 16 okt, senast 21 okt"). Manager bestämmer var det hamnar: den sprider ut, buntar ihop, flyttar och stryker enligt policyn och din kalender.
- **Dispatchern** är Managers snabba uppgift. När något nytt kommer in placerar den det i schemat, med en tidpunkt, eller i det sparade, eller släpper det. Den planerar bara om när något har ändrats: något nytt har kommit in, din kalender har ändrats, du har bett om tystnad, eller en deadline har flyttats.
- **Leveransen är Python, inte Claude.** När klockan slår görs en sista kontroll (nattro, paus, gäller saken fortfarande?). Sedan skriver Assistant meddelandet.
- **Bokning före löfte.** När du ber om något ("påminn mig fredag") bokar Assistant först. Bokningen svarar direkt med "bokat" eller en krock ("fredag 23:30 är nattro", "du har tenta 09:00"). Vid en krock frågar Assistant dig innan den lovar något.
- **Dina bokningar ligger fast.** Manager kan föreslå en flytt via Assistant, men flyttar aldrig i tysthet.
- **Det sparade** är en lista utan tid. Du kan be om det ("något nytt?"), och Manager kan boka in något därifrån när det passar.
- **Din kalender** (TimeEdit och Apple) är fakta om dig och läses men ägs inte. **Kontorets egna körningar** är infrastruktur och hör inte till schemat.

## Från fynd till dig

```
Nytt fynd → triage (Director: värt något?) → dispatcher (Manager: när?) → schemat → Assistant skriver
```

- Triage körs inom ett par minuter efter att något nytt har kommit in. Ett brådskande fynd kan nå dig på minuter.
- **Direkt, förbi schemat:** allt du själv tar initiativ till. Det gäller svar när du skriver, resultat av en sökning du har bett om, bekräftelser, byggläget och larm om att något är trasigt. Tar du initiativet kommer svaret direkt. Tar kontoret initiativet bestämmer schemat.

## Natten: ett svep i ordning

En kedja som startar när du med störst sannolikhet sover. Starttiden räknas fram ur när du brukar skriva sist och nattron (en smartklocka kan kopplas in senare).

1. **Assistant** rensar minnet och bokar sina löften.
2. **Director** gör sin genomgång, eller reflektion de nätter den ska göras.
3. **Researcher** planerar, om en beställning, feedback eller ett tyst uppdrag kräver det.
4. **Manager** granskar gårdagen och planerar dagens schema med den färska bilden.

Tar kvoten slut stannar kedjan och fortsätter där den slutade.

## Körfält och köer

- **Två körfält:** ett för samtalet (det får aldrig vänta på bakgrundsarbete) och ett för allt annat.
- **En instans per roll åt gången.** Väcks en roll medan den står i kö blir det en körning som läser all post. Snabba uppgifter skriver inte i anteckningarna och kan därför köras samtidigt som rollens långsamma uppgift.

## ID:n och uppslag

Allt som ska gå att följa upp får ett ID: poster (#P), det du satsar på (#S), fynd (#F), bokningar (#B) och körningar (#R). Meddelanden i samtalet kopplas till de ID:n de handlar om, och allt en körning skriver bär dess id.

**#S** är en enda sorts post för mål, projekt, tillfällen (det du visat intresse för, sökt eller valt bort) och Directors idéer. Var och en har ett varför, en status, ett nästa steg, en deadline och anteckningar, och ett tillfälle eller en idé hör till målet det tjänar. Ett mål bär också planen för det som drivs över tid (tidigare "trådar"). Director äger målen och idéerna; Assistant för in det du säger. Assistant slår upp ("vad lovade jag om Google?") i stället för att minnas. Schemat är minnet av löftena. ID:n är kökets verktyg och syns på webbsidan, inte i chatten.

Allt som ska följas över tid ligger i en gemensam kärna (`agent/forms.py`, `agent/items.py`). Allt har samma grund: id, form, ägare, status, vad det tjänar, källa, när det ses över, hur länge det gäller och en historik. Varje form har dessutom egna fält som koden kontrollerar. Formerna och deras bokstäver:

| Bokstav | Form | Vad det är |
|---|---|---|
| S | spår | mål, projekt, tillfälle, idé |
| T | steg | en del av en plan: ägare, när, klart när |
| E | experiment | något en roll prövar, för att förbättra eller utforska |
| U | uppfattning | något Audax tror om dig, med källa |
| Q | fråga | något en roll vill veta |
| G | regel | något koden gör utan Claude |
| N | rutin | återkommande arbete |

Rollerna läser och skriver med kontorets verktyg (`agent/office_tools.py`), som kontrollerar varje skrivning mot formen och rollens rättigheter. Rådatan finns under `/data/items/`.

## Återkoppling: hur kontoret blir bättre

- **Director → Researcher:** vad varje uppdrag gav, vad som behölls och hur du reagerade blir feedback inför nästa planering.
- **Manager → dispatchern:** varje natt en rapport med dispatcherns beslut och skäl, plus stickprov där Manager prövar om besluten följde policyn. Missar ("varför sa du inte det här?", saker du själv hämtade ur det sparade) kopplas tillbaka till beslutet som höll inne dem.
- **Mått för Manager** räknas fram av Python och tolkas av Manager: svarstid, reaktioner per sort och tid på dygnet, pauser, hämtningar och knuffar som följdes. Det är mått, inte regler.
- **Täckning:** Researcher reviderar regelbundet mot ett oberoende riktmärke för att se vad den missar.

## Utanför kontoret

- **Infrastruktur utan omdöme:** sinnen och sidläsning, förfilter, kalendersynk, hälsovakt, säkerhetskopia och leverans enligt schemat.
- **Byggläget** är en extern konsult. Det ändrar koden när du ber om det, har inget minne, och ingenting går live förrän du har godkänt det med en knapp.

## Från i dag till modellen

1. **Klart (etapp 3):** Researcher är utbruten ur Director. Kartor, ledtrådar, täckning, bevakningar och takt flyttar, och djupsvep och nya sökfrågor går upp i planeringen. Uppdragsfilen ägs av Researcher, och Director beställer via post.
2. **Klart (etapp 2–3):** triage är Directors snabba uppgift och dispatchern Managers.
3. **Klart (etapp 1):** postgången ersätter anteckningar och förfrågningar, och `open-questions.md` är borta.
4. **Klart (etapp 2):** schemat ersätter "vänta på lucka" och rundan var 10:e minut. Fynd och knuffar får en tidpunkt, och påminnelser och löften är bokningar. Kvar: frågor, avstämningar och schemafrågor ställs fortfarande i en ledig stund (kontrolleras var 15:e minut) i stället för att bokas.
5. **Klart (etapp 1, 3 och 4):** arbetsminne för alla fyra roller, och Assistants nattpass (som nu skriver samtalssammanfattningen).
6. **Klart (etapp 1):** rättigheterna följer ägarskapet. Assistant har skrivrätt bara till sina egna filer.
7. **Klart (etapp 1 och 4):** läget: Director äger fakta (`now.md`), Manager äger slutsatsen. Målen hör till Director; Assistants nattpass flyttar dem ur profilen.
8. **Klart (etapp 4):** nattsvepet i ordning, med starttid räknad ur när ägaren brukar skriva sist.
9. **Klart (etapp 1):** två körfält.
10. **Klart (etapp 1 och 2):** ID:n (#P, #S, #F, #B) och uppslagsverktyget.
11. **Klart (etapp 4):** Managers rapport om dispatcherns beslut och missarna, stickprov och mått.
12. **Klart (etapp 2–4):** promptarna är omskrivna mot den här sidan, en roll i taget. `how_i_work` läser den här sidan direkt (`self.md` är borta).
13. **Klart (16 okt):** förenklat minne. Mål, trådar, projekt, ansökningsspår och idéer är #S-poster i databasen i stället för filer. Profilen tog upp skola, intressen, vad som får störa och schemat; policyn tog upp stilen och läget. Loggarna och journalen ersattes av en redogörelse per körning. Minnet är nio filer: profilen, bilden av dig, läget, policyn, fyra arbetsminnen och sökkartorna.
