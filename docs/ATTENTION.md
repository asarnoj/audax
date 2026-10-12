# Uppmärksamhetssystemet: rätt sak, vid rätt tillfälle, i rätt mängd

Status: designutkast · 2026-10-09 · hör till [PLAN.md](PLAN.md)

## 1. Problemet

Agenten kan bara skapa värde genom att ta din uppmärksamhet i anspråk, och varje meddelande kostar något. Det finns tre sätt att misslyckas:

- **För mycket:** du vänjer dig vid meddelandena och slutar läsa. Då når inte heller de bra sakerna fram.
- **För lite:** möjligheter missas, och agenten slutar kännas närvarande.
- **För mycket kalibrering:** frågor och justeringar blir i sig en belastning, och systemet hinner aldrig stabilisera sig.

Det finns ingen perfekt lösning. Målet är därför ett system som är **bra från början, förbättras långsamt och tryggt, och aldrig blir störigt av att försöka förbättra sig.**

**Målbild:** agenten ska kännas som en klok vän som har koll på dina intressen. Den hör av sig när det är värt det, väljer bra tillfällen, och tiger när den inte har något att säga.

**Det vi optimerar är upplevt värde, inte engagemang.** Antal öppnade meddelanden, svarstid eller tid i chatten är aldrig mål i sig. Det är signaler som kan tolkas fel, eftersom ett meddelande som stör kan få snabbt svar.

## 2. Forskning vi bygger på

> Referenserna är hämtade ur minnet och ska verifieras innan vi citerar dem formellt. Principerna är väletablerade.

| Princip | Källa (urval) | Hur vi använder den |
|---|---|---|
| **Avbrott kostar mer än de verkar**: stress och tidspress ökar även när uppgiften blir klar | Mark, Gudith & Klocke (2008), *The cost of interrupted work* | Varje "skicka nu" måste vara värt kostnaden. Standardläget är att vänta. |
| **Avbrott vid naturliga brytpunkter** kostar mindre än mitt i en uppgift | Iqbal & Bailey (2008), *Effects of intelligent notification management* | Kalenderns luckor och övergångar mellan pass är de bästa tillfällena. |
| **Mottaglighet går att förutsäga** av kontext som tid och nylig aktivitet, men den skiljer sig mycket mellan personer | Pielot et al. (2014, 2017); Mehrotra et al. (2016), *My Phone and Me* | Mottaglighet lärs **per person** utifrån ditt beteende och tidpunkter i veckan, inte utifrån generella regler. |
| **Att samla notiser i batchar** ökar välbefinnandet jämfört med att få dem direkt eller inte alls | Fitz, Kushlev et al. (2019), *Batching smartphone notifications* | Det mesta samlas ihop. Det som skickas direkt är undantag. |
| **Blandat initiativ**: agera bara när det förväntade värdet är större än kostnaden. Vid osäkerhet är det bättre att fråga billigt eller vänta. | Horvitz (1999), *Principles of mixed-initiative user interfaces* | Varje beslut har en bedömd nytta, kostnad och säkerhet, och vid osäkerhet väljs en försiktigare leveransform. |
| **Lugn teknik**: information kan finnas i periferin och hämtas när man själv vill | Weiser & Brown (1996); Case (2015), *Calm Technology* | En **hylla** där saker finns tillgängliga utan att agenten säger något. |
| **Larmtrötthet**: många lågvärdiga larm gör att även de viktiga ignoreras | Forskning om kliniska beslutsstöd (t.ex. Ancker et al., 2017) | Gränsen för "stör nu" hålls hög och skyddas extra. |
| **Mätbördan** i experience sampling: för många frågor försämrar både svaren och upplevelsen | ESM-metodlitteratur | Frågor om feedback har en egen sparsam budget (se §6). |
| **Goodharts lag**: när ett mått blir ett mål slutar det vara ett bra mått | — | Flera kompletterande mått plus skyddsmått (se §5). |
| **Autonomi** (självbestämmandeteori): kontroll känns bättre än att bli styrd | Deci & Ryan | Dina egna kommandon går alltid före det systemet har lärt sig. |
| **Optimering av notisvolym** som ett avvägningsproblem mellan nytta och kostnad | LinkedIns forskning om notisvolym (KDD 2018–19) | Inspiration för v3: lärda strategier (bandits) när det finns tillräckligt med data. |

## 3. Designprinciper

1. **Att tiga är standard.** Ett meddelande måste motivera sin plats, inte tvärtom.
2. **Fler leveransformer ger bättre beslut.** Valet är inte bara skicka eller låt bli (§4.3).
3. **Mät det som går att observera, låt omdöme fatta beslut.** Statistik beskriver läget, och språkmodellen väger det mot policyn. Det finns inga hårdkodade formler för hur mycket som får skickas.
4. **Ändringar behandlas som hypoteser.** Manager ändrar en sak i taget, säger vad den förväntar sig och utvärderar först efter en tid. Det hindrar systemet från att svänga fram och tillbaka utan att det behövs gränsvärden (§4.6).
5. **Feedback hämtas i första hand passivt.** Explicita frågor är dyra och används sparsamt.
6. **Du har sista ordet.** Kommandon som `/tyst`, `/mer` och `/mindre` gäller direkt och går före det inlärda.
7. **Allt går att följa.** Varje beslut sparas med motivering, så att det går att förstå i efterhand och förbättra.
8. **Varje modul går att byta ut.** Bedömningen av värde, mottaglighet och dispatcher har tydliga gränssnitt och versioner.

## 4. Arkitektur

```
 Kandidat ──► [Värdebedömning] ──► Utkorg ◄── [Mottaglighet] ◄── kalender, tid, beteende
                                     │
                                     ▼
                               [Dispatcher]  ◄── policy.md, interrupt.md, dina kommandon
                                     │  väljer leveransform + tillfälle + paketering
                    ┌────────┬───────┼─────────┬──────────┐
                  Avbryt   Lucka   Bunt     Sammanf.    Hylla
                    └────────┴───────┴─────────┴──────────┘
                                     │
                               [Formulering]  (rendering: kort, varför-du, handling)
                                     │
                                  Telegram ──► [Signaler] ──► [Kalibrering / Manager]
                                                                  (hypoteser, veckokoll)
```

### 4.1 Värdebedömning (vad är värt något?)

Triage ger inte ett enda betyg utan **flera egenskaper**, så att dispatcher och Manager kan resonera om dem:

| Egenskap | Fråga |
|---|---|
| `relevance` | Hur väl stämmer det med profil, projekt och mål? |
| `actionability` | Kan du göra något konkret med det? |
| `time_sensitivity` | Försvinner värdet snart (deadline) eller håller det? |
| `novelty` | Är det nytt för dig, eller liknar det något du redan har fått? |
| `confidence` | Hur säker är agenten på bedömningen? |
| `complement` | Hjälper det dig med det agenten ska kompensera för, att leta upp möjligheter? |

Varje egenskap får en grov nivå (låg/medel/hög) och en kort motivering. Grova nivåer räcker, eftersom modellens precision ändå är begränsad.

### 4.2 Mottaglighet (när passar det?)

Två delar:

- **Kontext just nu**, framräknad i Python: upptagen till, nästa lucka, hur packad dagen är, nattro, `/tyst`, hur länge sedan förra meddelandet var och om det besvarades.
- **Inlärd mottaglighet:** för varje timme i veckan (168 fack) räknas hur ofta du har reagerat på meddelanden och hur snabbt. **v1:** enkel statistik som visas för dispatchern som "så här brukar det se ut". **v2:** bayesiansk skattning (beta-fördelning per fack, så att osäkerheten syns). Den här statistiken beskriver ditt beteende och sätter inga gränser.

### 4.3 Leveransformer

| Form | När | Kostnad för dig |
|---|---|---|
| **Avbryt nu** | Hög brådska och högt värde, eller något på `interrupt.md` | Hög |
| **Nästa lucka** | Värdefullt och tidskänsligt, inte akut | Medel |
| **Bunt** | Flera saker som hör ihop eller väntar, skickas ihop i en lucka | Låg per sak |
| **Sammanfattning** | Senare, när rytmen har satt sig: en fast stund | Låg och förutsägbar |
| **Hylla** | Bra men inte viktigt nog att skicka. Hämtas med `/nytt` eller "något nytt?", och agenten kan nämna den naturligt i chatten. | Ingen |
| **Släpp** | Under tröskeln, ligger kvar i loggen | Ingen |

Hyllan är den viktigaste avlastningen. Agenten behöver aldrig välja mellan att störa och att kasta bort något.

### 4.4 Dispatcher

Varje gång dispatchern körs får Haiku ett kompakt beslutsunderlag: kandidaterna med egenskaper, mottaglighet, policy, din störningslista, de senaste dagarnas utfall och vilka experiment som pågår. Den returnerar:

```json
{ "decisions": [ { "candidate_id": 41, "tier": "next_gap", "bundle_with": [43],
                   "not_before": "14:05", "reason": "deadline fredag, lucka efter labb" } ],
  "hold": [ { "candidate_id": 44, "tier": "shelf", "reason": "intressant men inte bråttom" } ] }
```

Det enda som är hårdkodat är din nattro, `/tyst` och att dubbletter aldrig skickas. Allt annat är omdöme utifrån policyn.

### 4.5 Signaler (feedback)

Signalerna ordnas efter hur mycket de kostar dig, från ingenting till mest:

1. **Passiva:** svar och svarstid, om något besvaras eller inte, vad du hämtar från hyllan, om du frågar efter mer. Senare även länkklick.
2. **Billiga explicita:** emoji-reaktioner.
3. **Naturligt språk:** "mer sånt", "det där var onödigt", "varför sa du inte det här?". Det sista är en signal om att något missades, och den är extra värdefull.
4. **Frågor:** omröstningar och fritext. Dyrast, och styrs av en egen budget (§6).

Varje signal kopplas till ett beslut, så att det går att se både *vad* som skickades och *hur* det skickades.

### 4.6 Kalibrering som experiment (Manager)

Manager ändrar inte inställningar fritt. Den driver **hypoteser**:

```markdown
## H-007 · aktiv sedan 2026-10-14
Observation: 3 av 4 hackathon-tips under föreläsningsdagar fick ingen respons.
Ändring: tidskänsliga tips skickas i kvällsluckan på föreläsningsdagar.
Förväntan: fler svar på de tipsen utan att antalet missade deadlines ökar.
Utvärderas: tidigast 2026-10-21 (eller efter ≥5 sådana tips).
```

Regler som Manager-prompten följer, i klartext:
- Högst ett aktivt experiment per område (tempo, timing, innehåll, ton).
- Ett experiment utvärderas mot vad som förväntades. Det behålls, ändras eller backas, och utfallet skrivs i `changelog.md`.
- Om det inte finns tillräckligt med data blir svaret "vänta". **Att inte göra något är ett giltigt och vanligt beslut.**
- Hypoteser som gäller struktur (nya källor, ändrad ton) blir förslag till dig.

Det här ger stabilitet genom metod och inte genom gränsvärden. Systemet ändras när det finns belägg, och det syns varför.

## 5. Mått

**Huvudmått:** *upplevd balans och nytta.* Det mäts med en enda fråga med ett tryck, ungefär varannan vecka: "Hur har det känts? För mycket · Lagom · För lite" plus "Var något riktigt bra?". Det är det enda som direkt mäter det vi bryr oss om.

**Kompletterande mått:**
- Andel meddelanden som lett till en handling: svar, klick eller att du gör något.
- Ånger: 👎, "onödigt", `/mindre`, `/tyst`.
- Missat: "varför sa du inte…" och värdefulla saker som du själv hittade på hyllan.
- Avbrott: hur ofta "avbryt nu" används och hur det mottogs.

**Skyddsmått** (larm till Manager, inte gränser): ökande antal obesvarade meddelanden i rad, återkommande `/tyst`, och fler frågor om feedback än vanligt.

## 6. Mätbördan: frågor har en egen budget

- Frågor om feedback och om ditt liv (`open-questions.md`) delar på **samma sparsamma utrymme** och tävlar med varandra om det.
- En fråga prioriteras efter **vad svaret skulle förändra**. Om svaret inte ändrar något agenten gör, ställs inte frågan.
- Frågor ställs helst i lugna luckor eller naturligt i en pågående konversation.
- Om du inte svarar ska nästa fråga dröja längre, och då ska agenten fråga mindre, inte mer.
- Under de första veckorna är frågorna fler (kallstart) och blir sedan successivt färre.

## 7. Kallstart

- **Startintervjun** ger en första bild av dig: intressen, rytm, vad som får störa och vad agenten ska kompensera för.
- **Försiktig början:** det första läget lutar mot hyllan och buntar. Det är lättare att öppna upp än att vinna tillbaka förtroende efter att ha stört för mycket.
- **Öppenhet:** agenten säger att den håller på att lära sig, och att du får säga till.

## 8. Dina kontroller

`/tyst [tid]`, `/fokus` (bara avbrott från störningslistan), `/mer` och `/mindre` (generellt eller för en viss sorts innehåll), `/nytt` (hyllan), `/varfor` (svarat på ett meddelande visar det beslutsunderlaget), `/policy` (visar nuvarande policy och experiment). Kommandona ger direkt effekt och räknas samtidigt som starka signaler.

## 9. Utvärdering och ständig förbättring

- **Beslutsspår:** varje beslut från dispatchern sparas med underlag, val, motivering och modulversioner (`decisions`-tabellen), och kopplas till utfallet.
- **Versionerade moduler och promptar:** `triage@3`, `dispatcher@5`. Versionen följer med varje beslut, så att det går att jämföra före och efter.
- **Skuggläge:** en ny version av dispatchern kan köras parallellt utan att skicka något. Den loggar vad den *skulle* ha gjort, och skillnaderna går att granska innan bytet.
- **Återspelning:** gamla beslutsunderlag kan köras mot en ny version för att se hur den hade bedömt. Det ger ingen exakt kontrafaktisk analys men fångar uppenbara försämringar.
- **Veckorapport (för dig, valfri):** vad som skickades, vad som landade, pågående experiment och vad som ligger på hyllan. Den finns på hyllan och skickas inte ut.

## 10. Det vi medvetet avstår från

- Att optimera för engagemang, streaks eller tid i appen.
- Att låta agenten be om uppmärksamhet ("har du sett mitt förra meddelande?").
- Hårdkodade gränser för hur mycket som får skickas eller ändras, utöver nattro och dina egna kommandon.
- Komplex maskininlärning innan det finns data som motiverar den.

## 11. Uppgraderingsväg

| Version | Värde | Mottaglighet | Beslut | Kalibrering |
|---|---|---|---|---|
| **v1** (MVP) | LLM-egenskaper | Kalender + enkel statistik per timme i veckan | Haiku-dispatcher + policy | Manager med hypoteser, kontroll varannan vecka |
| **v2** | + likhet mot tidigare 👍/👎 (embeddings) | Bayesiansk skattning per fack | + skuggläge och återspelning | + automatisk utvärdering av hypoteser mot beslutsspår |
| **v3** | Personlig rankingmodell | + fler kontextsignaler (t.ex. telefonens fokusläge via Genvägar) | Kontextuell bandit för leveransform och timing, med LLM:en som kontroll | Manager väljer mellan strategier |

Varje steg byter bara ut en modul bakom samma gränssnitt, så att resten av systemet inte påverkas.

## Ansökningsspåret

Att hitta möjligheter är halva jobbet. Den andra halvan är att hjälpa ägaren förbi tvekan, så att hen faktiskt söker ([agent/pursuits.py](../agent/pursuits.py)).

- **Spåret:** allt ägaren visar intresse för (en 👍 på ett tips räknas), förbereder, söker, får svar på eller väljer bort, med deadline och nästa steg. Chatten för in det ägaren säger, Director håller det sant utifrån samtalet och mejlen (bekräftelser, svar, intervjuer), och systemet markerar missade deadlines.
- **En knuff, i rätt tid:** för det ägaren inte har sökt än blir en knuff en kandidat i utkorgen på den dag Director bedömt hjälper mest (beroende på hur mycket jobb ansökan är), annars tre dagar före deadline. Dispatchern väljer stunden och chatten skriver den, en mening. En knuff per sak, aldrig mot något ägaren sagt nej till.
- **Uppföljning:** efter en ansökan noterar Director när svar kan väntas, och ser i mejlen om det kommer.
- **Lärande:** vad ägaren driver, släpper och gör efter en knuff syns för Director (bilden av vad hen egentligen vill) och Manager (om knuffarna landar bra eller känns påträngande).

