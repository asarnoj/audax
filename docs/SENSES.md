# Sinnen: hur agenten tar in världen

Status: lager 1–3 byggda · 2026-10-10 · hör till [PLAN.md](PLAN.md)

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

## Kostnad och kvot

- Strukturerade sinnen kostar inget av Claude-kvoten förrän något passerar förfiltret och når triage, och då i batchar.
- Sidbevakning anropar Haiku bara när sidan faktiskt har ändrats.
- Spaningen kör ett begränsat antal frågor per körning, och det är Manager som justerar takten.

## Stegordning

1. Pollers för `jobtech`, `devpost`, `ats`, `hn_hiring` och `page`, samt bevakningslistan.
2. Spaning: portfölj, frågestatistik och generering av nya frågor.
3. Förslag om nya sinnen från sökstatistik, via Manager.
