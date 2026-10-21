# Sinnen: hur agenten tar in världen

Status: lager 1–3 byggda · 2026-10-10 · hör till [PLAN.md](archive/PLAN.md)

## Problemet

Första versionen hade två sorters sinnen: flöden (RSS och websocket) och en generell webbsökning per mål var tredje dag. Det räcker inte:

- **Sällan finns ett enda flöde** för det som intresserar användaren. Världen är spretig: jobb ligger i hundratals företags egna rekryteringssystem, events på olika sajter och sidor utan flöden.
- **En sökning per mål ger slumpmässig täckning.** Ett par slumpmässiga träffar i veckan är ett symptom på det. Söker man brett blir resultatet ytligt, och söker man smalt missar man saker.

## Principen

**Det som går att räkna upp ska räknas upp. Sökning är till för det som inte går att räkna upp. Det sökningen hittar ska bli ett fast sinne.**

Därför finns tre lager:

### 1. Strukturerade sinnen: fullständig täckning där det går
Källor som går att gå igenom systematiskt, med dubblettkontroll:

| Typ | Exempel | Vad den ger |
|---|---|---|
| `rss`, `websocket` | HN best, bloggar, universitetets flöden | allt nytt i flödet |
| `jobtech` | Arbetsförmedlingens öppna API | alla svenska annonser som matchar frågorna |
| `ats` | Lever, Greenhouse, Teamtailor, Ashby | alla annonser hos företagen på bevakningslistan |
| `devpost` | Devpost | alla öppna och kommande hackathons |
| `hn_hiring` | HN "Who is hiring" | månadens tjänster, filtrerade på plats och roll |
| `page` | sidor utan flöde (t.ex. en eventsida) | nya saker på sidan: diffen läses ut av Haiku |

### 2. Bevakningslista: följ aktörer, inte bara ämnen
`instance/config/watchlist.yaml` innehåller företag och organisationer som är intressanta för användaren. Varje företag med ett känt rekryteringssystem bevakas automatiskt i sin helhet. Det är det mest systematiska sättet att inte missa jobb: om företaget publicerar något syns det.

### 3. Spaning: sökning som portfölj, inte som enstaka försök
För det som inte går att räkna upp söker agenten med en **portfölj av frågor** per uppdrag. Den mäter vad varje fråga ger:

- Frågor genereras utifrån profilen i olika riktningar: roll × plats × tid × typ av källa, på svenska och engelska.
- Varje körning väljer ut några frågor. De flesta är beprövade frågor som har gett bra fynd, och några är nya för att utforska (explore/exploit).
- För varje fråga sparas utfallet: fynd, och vad triage, dispatchern och användaren tyckte. Frågor som inte ger något fasas ut.
- **Från sökning till sinne:** domäner som ofta ger bra fynd föreslås som fasta sinnen (`page`, `rss` eller `ats` via bevakningslistan). Förslaget kommer som en knapp till användaren.

Allt hamnar som händelser i samma pipeline som tidigare (förfilter → triage → dispatcher). Sinnena avgör bara vad agenten *ser*. Vad användaren *får* avgörs som förut av uppmärksamhetssystemet.

## Täckning: att se vad sökningen missar

Att mäta varje sökfråga visar hur bra frågorna är, men inte hur mycket av det som finns Audax når. "Inga fynd" kan betyda att det inte finns något, eller att Audax letar på fel ställen, vid fel tidpunkt eller för snävt. Ägaren kan inte se skillnaden, så systemet måste kunna det ([agent/search_map.py](../agent/search_map.py)).

- **En karta per sökuppdrag** (`memory/research/maps/<uppdrag>.md`, skriven av Researcher): områdets segment (för sommarpraktik t.ex. storbolagens program, scaleups, startups, offentlig sektor, forskning, konsultbolag), var varje segment annonserar och när, vilka metoder som täcker det och vad de har gett. Kartan slutar med "Luckor och nästa steg", och sökningarna och Researchers nya sökfrågor riktas dit.
- **Ledtrådar bygger nätet över tid.** Varje sökning lämnar, utöver fynd, ledtrådar: företag, aggregatorer, listor, program, communities och eventkalendrar den stötte på. De samlas med antal gånger de setts, och Researcher lägger till de bra som bevakningar.
- **Utforska metoder, inte bara ord.** Är ett segment tunt byter Researcher sorts metod (bevaka företagen själva, hitta aggregatorn, följa en karriärsida, tajma efter när ansökan öppnar), inte bara sökord.
- **Täckningsrevision** i Researchers planering: Researcher tar fram ett oberoende riktmärke, 20–50 aktörer som brukar erbjuda det uppdraget letar efter, utan att först titta på vad Audax bevakar. Systemet kollar varje namn mot allt Audax bevakar eller har sett. Andelen som täcks är en uppskattning av hur mycket som nås, och de som saknas är luckorna.
- **Uppdrag som tystnar** granskas: fel ställen, fel tid på året eller för snävt? Hänger det på hur ägaren ser det frågar Researcher Director, som avgör eller frågar ägaren.
- **Insyn:** ägaren kan fråga hur Audax letar (chattens `situation`, about=searches). Researcher berättar själv, sällan och via chatten, när en sökning har svårigheter, går påtagligt bra eller upptäcker något ägaren borde veta.

## Kostnad och kvot

- Strukturerade sinnen kostar inget av Claude-kvoten förrän något passerar förfiltret och når triage, och då i batchar.
- Sidbevakning anropar Haiku bara när sidan faktiskt har ändrats.
- Spaningen kör ett begränsat antal frågor per körning, och det är Manager som justerar takten.

## Stegordning

1. Pollers för `jobtech`, `devpost`, `ats`, `hn_hiring` och `page`, samt bevakningslistan.
2. Spaning: portfölj, frågestatistik och generering av nya frågor.
3. Förslag om nya sinnen från sökstatistik, via Manager.
