# Teknisk plan: personlig agent (Worker + Manager)

Status: utkast för godkännande · 2026-10-09

## 1. Mål och avgränsning

En proaktiv personlig agent som körs på en Raspberry Pi 400 och pratar med dig via Telegram. Den kompletterar det du inte gör så bra, främst att leta efter möjligheter. Den lär sig vem du är och vad du håller på med, och den reglerar själv sitt eget tempo.

Inte i scope: automatisering av vardagstjänster (beställningar, köp och liknande).

## 2. Körmodell

**En enda Python-process** (asyncio) som körs som systemd-tjänst på Pi:n. Allt schemalagt, även Manager, körs inuti den med APScheduler. Fördelarna:

- Det finns ett enda ställe som styr åtkomsten till Claude. Pi 400 har 4 GB RAM och Pro har ett användningstak, så **högst en Claude-session körs åt gången**.
- Det finns en kö med prioritet: **chatt > brådskande > undersökningar > Manager > bakgrundsjobb för att uppdatera kontext**.
- Det blir färre rörliga delar att felsöka.

```
                 ┌──────────────────────── agent (en process) ────────────────────────┐
 TimeEdit ICS ──►│ Sources ──► Event-bus ──► Förfilter ──► Triage ──► Utkorg          │
 Apple ICS    ──►│ (plugins)                 (Python:     (Claude,    (SQLite)        │
 GitHub       ──►│                            dedupe,      Haiku)        │            │
                 │ Undersökningar ─────────────────────────────────────►┤            │
                 │ (schemalagda, WebSearch)                              ▼            │
                 │                                                   Dispatcher ──────┼──► Telegram
 Du (Telegram) ─►│ Chatt-session (beständig, --resume) ◄───── kontext ──┘            │◄── reaktioner,
                 │                                                                    │    svar, polls
                 │ Manager (nattlig) ──► policy.md, research.yaml, frågor, ändringslogg│
                 │                                                                    │
                 │ ClaudeGate: semafor(1) + prioritetskö + backoff vid användningstak │
                 └────────────────────────────────────────────────────────────────────┘
```

## 3. Så styrs Claude

- **`claude-agent-sdk`** (Python). Det kör Claude Code CLI i bakgrunden och använder därför **CLI:ns inloggning på Pi:n**, alltså din Pro-prenumeration. Om det byts till en `ANTHROPIC_API_KEY` i `.env` fungerar allt likadant, så du kan byta senare om taket blir ett problem.
- Claude arbetar alltid i mappen **`workspace/`**. Den har en egen `CLAUDE.md` med agentens personlighet och regler, och en `.claude/settings.json` som bara tillåter läsning och skrivning i `workspace/memory/`, samt WebSearch och WebFetch.
- Det finns tre sorters körningar:

| Typ | Modell | Session | Verktyg |
|---|---|---|---|
| **Chatt** | Sonnet | En beständig session som återupptas med `resume`. Session-id sparas i SQLite. | minne (Read/Edit), WebSearch, egna verktyg |
| **Uppgift** (triage, undersökning, dispatcher) | Haiku för triage och dispatcher, Sonnet för undersökningar | Engångskörning med strukturerat JSON-svar | begränsat per uppgift |
| **Manager** | Sonnet | Engångskörning | minne, config, DB-sammanfattning |

- **Egna verktyg** körs i samma process via SDK:ns MCP-server, `create_sdk_mcp_server`:
  - `record_feedback(message_ref, sentiment, note)`: när du skriver t.ex. "sluta skicka sånt".
  - `send_poll(question, options)`: används av Manager och av frågesystemet.
  - `add_open_question(topic, question, priority)`: en lucka i agentens förståelse som ska fyllas senare.
  - `interrupt_list_add/remove`: din lista över saker som alltid får störa.
- **Kontexten i chatten:** Claude Code komprimerar själv långa sessioner. Dessutom skriver Manager varje natt en sammanfattning till `memory/journal/` och startar en ny session med den sammanfattningen som grund om sessionen har blivit för lång.
- **Pro-taket:** Agenten delar kvot med din egen användning av Claude. Därför används Haiku för det som körs ofta, och bara det som klarar förfiltret batchas till triage. När taket nås pausas bakgrundsjobben automatiskt medan chatten fortsätter, och varje körning loggas i tabellen `claude_runs`.

## 4. Minne (markdown, lokalt)

`workspace/memory/` finns aldrig på GitHub. Mappen har ett **eget lokalt git-repo**, så att varje ändring från agenten eller Manager blir en commit som går att granska och ångra.

| Fil | Innehåll | Vem skriver |
|---|---|---|
| `profile.md` | Vem du är, mål, styrkor och vad agenten ska kompensera för | Startintervju, chatt, Manager |
| `school.md` | Program, kurser och deadlines | Chatt, kalender, frågor |
| `schedule.md` | Vilka TimeEdit-pass/grupper som gäller dig, och vanor | Agenten, efter frågor till dig |
| `projects.md` | Pågående projekt och status | Chatt, GitHub-källan |
| `interests.md` | Intresseområden och vad du letar efter | Chatt, Manager |
| `interrupt.md` | Saker som alltid får störa dig | Du, via chatten |
| `open-questions.md` | Luckor i agentens förståelse, prioriterade | Agenten; luckan stängs när du har svarat |
| `policy.md` | **Manager-policy i klartext**: tempo, prioriteringar, vad som är på väg upp eller ner | Endast Manager |
| `changelog.md` | Vad Manager har ändrat och varför | Manager |
| `journal/YYYY-MM-DD.md` | Daglig sammanfattning av chatt och händelser | Manager |

## 5. Pipeline: händelse → meddelande

> Balansen mellan frekvens, timing och mängd är ett eget delsystem: se **[ATTENTION.md](ATTENTION.md)**. Avsnitten nedan beskriver flödet, och ATTENTION.md beskriver hur besluten fattas och förbättras.

1. **Sources** (plugins i `agent/sources/`) skickar `Event(source, kind, payload, dedupe_key, ts)` till bussen.
   - Strömmande källor (websockets) har `async run(emit)`.
   - Källor som hämtar data med jämna mellanrum har `async poll()` och ett intervall i `config/sources.yaml`.
2. **Förfilter (Python, ingen LLM):** dubblettkontroll mot SQLite plus källspecifika regler som inte kräver bedömning, t.ex. "ignorera pass i kurser som enligt `schedule.md` inte gäller mig".
3. **Triage (Haiku, batchad):** profil, intressen och policy plus en batch händelser. Svaret är JSON per händelse med `relevant`, `why`, `urgency (now|soon|whenever)`, `draft` och `kind (opportunity|info|context_only|question)`.
   - `context_only` skickas inte till dig, men uppdaterar minnet, t.ex. GitHub-aktivitet som läggs in i `projects.md`.
4. **Utkorg (SQLite):** kandidater med status `pending | sent | dropped | expired` och ett eventuellt utgångsdatum, t.ex. en deadline.
5. **Dispatcher:** körs var 20:e minut om utkorgen inte är tom, och direkt när något har `urgency=now`.
   - Den får se nuläget: om du är upptagen och till när, nästa lucka, när den senast skrev, hur du har reagerat de senaste dagarna, kandidaterna i utkorgen, `policy.md` och `interrupt.md`.
   - Haiku avgör om något ska skickas nu, vilka kandidater det gäller och om de ska slås ihop. **Bedömningen görs alltså av modellen utifrån policyn, inte av formler.**
   - Den enda hårda regeln i koden är **nattro**, som du ställer in själv i `settings.yaml`.
6. **Telegram:** varje skickat meddelande kopplas till sitt kandidat-id, så att reaktioner och svar kan föras tillbaka till rätt kandidat.

## 6. Schemalagda undersökningar

`config/research.yaml` innehåller en lista med uppgifter:

```yaml
- id: events-hackathons
  goal: "Hitta events, hackathons och tävlingar som passar mina intressen och projekt"
  cadence: "två gånger i veckan"      # tolkas av Manager som sätter nästa körning
  scope: "Stockholm + online"
  enabled: true
```

En undersökning är en Sonnet-körning med WebSearch och WebFetch som har tillgång till minnet. Den returnerar fynd som JSON med `title`, `url`, `why_you`, `deadline` och `urgency`, och fynden går in i triage och utkorgen med URL:en som dubblettnyckel. **Manager får justera `goal`, `scope` och `cadence` fritt.** Att lägga till eller ta bort en undersökning räknas som en stor ändring och kräver att du godkänner den.

## 7. Schema och tillgänglighet

- **Källor:** TimeEdit-ICS och iCloud-kalendern via en delad ICS-länk. CalDAV är ett alternativ om delning inte fungerar. Båda hämtas var 30:e minut och tolkas med `icalendar` och `recurring-ical-events`.
- **Relevans:** Varje TimeEdit-pass matchas mot `schedule.md`. Om agenten hittar en okänd kurs eller grupp lägger den till en fråga i `open-questions.md`, t.ex. "Går du på labbet i X på tisdagar?". Svaret sparas, så frågan kommer inte igen.
- **Tillgänglighet:** Python räknar fram `busy_until`, `next_gap` och `day_density`. Siffrorna används som underlag för dispatchern och styr den inte.
- **Schemaändringar** (ett inställt eller flyttat pass som gäller dig) blir händelser med hög brådska.

## 8. Feedback och frågesystem

Signaler som sparas i tabellen `feedback`:

- **Reaktioner** (👍👎 m.fl.): via `message_reaction`-uppdateringar i Telegram. *Att det fungerar i privatchatt verifieras i M1.*
- **Svar på ett meddelande:** svaret kopplas till kandidaten och tolkas av chatt-sessionen, som anropar `record_feedback`.
- **Naturligt språk** i chatten, t.ex. "mindre sånt": samma verktyg.
- **Omröstningar** (icke-anonyma, så att `poll_answer` kommer fram) och **fritextfrågor** från Manager.
- **Ingen respons** räknas som en svag signal.
- *Senare:* länkklick via en redirect på Pi:n. Det kräver en publik endpoint eller att telefonen är ansluten till tailnet, så det skjuts upp.

**Frågesystemet:** `open-questions.md` är en prioriterad lista. En fråga i taget skickas till dispatchern som en kandidat av typen `question`, och den skickas helst i lugna luckor. Nya luckor uppstår på två sätt: när något okänt dyker upp, och när Manager varje natt läser igenom minnet och letar efter det som är oklart.

## 9. Manager (varje natt, t.ex. 03:30)

**Underlag:**
- dygnets meddelanden och feedback, sammanfattade från SQLite
- statistik från utkorgen: vad som skickades, släpptes och gick ut
- fynd per undersökning och vad som hände med dem
- användningen av Claude-kvoten
- `policy.md`, `research.yaml` och minnet

**Vad den gör:**
- Skriver om `policy.md` (tempo, prioriteringar och ton) med en motivering.
- Justerar `research.yaml` och uppdaterar profil, intressen och öppna frågor.
- Skriver dagens journal och avgör om chatt-sessionen ska startas om med en sammanfattning.
- Skriver i `changelog.md`.
- Gör en commit i minnesrepot.

**Vilka ändringar den får göra själv:** Det avgörs i Manager-prompten, i klartext, utan formler. Kalibrering gör den själv. Strukturella ändringar, som att lägga till eller ta bort källor och undersökningar, byta ton eller ändra störningslistan, skickas som förslag med knappar eller omröstning i Telegram.

**Rapportering:** Ibland skickar den en kort rad till dig om vad den har justerat. Hur ofta bestämmer den själv utifrån policyn.

## 10. Datamodell (SQLite, `data/agent.db`)

`events`, `candidates` (utkorgen, med värde-egenskaper), `decisions` (beslutsspår med modulversioner), `experiments` (Managers hypoteser), `messages` (telegram_msg_id ↔ candidate_id), `feedback`, `research_runs`, `claude_runs` (typ, modell, tid och ev. fel på grund av taket), `kv` (session-id, nästa körningar m.m.).

## 11. Repostruktur

```
audax/
├── pyproject.toml            # uv, Python 3.11 (Pi OS Bookworm)
├── README.md
├── .env.example              # TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ICS-URL:er, (ANTHROPIC_API_KEY)
├── config/
│   ├── settings.example.yaml # nattro, tidszon, modeller
│   ├── sources.yaml
│   └── research.yaml
├── agent/
│   ├── main.py               # startar allt
│   ├── claude_gate.py        # semafor, prioritet, backoff, loggning
│   ├── db.py
│   ├── telegram_bot.py
│   ├── tools.py              # egna MCP-verktyg
│   ├── pipeline/ (prefilter.py, triage.py, outbox.py, dispatcher.py)
│   ├── sources/  (base.py, calendar_ics.py, github.py)
│   ├── research.py
│   ├── manager.py
│   └── prompts/  (triage.md, dispatcher.md, research.md, manager.md, interview.md)
├── workspace/
│   ├── CLAUDE.md             # agentens personlighet och regler
│   ├── .claude/settings.json
│   └── memory/               # gitignored, eget lokalt git
├── deploy/ (agent.service, setup-pi.sh)
├── data/                     # gitignored
└── tests/
```

GitHub-repot ska vara **privat**. Personlig data ligger ändå aldrig i det.

## 12. Bibliotek

`claude-agent-sdk`, `python-telegram-bot` (v21+, async), `apscheduler`, `icalendar`, `recurring-ical-events`, `httpx`, `aiosqlite`, `pydantic`, `pyyaml`, `python-dotenv`. Testerna körs med `pytest` och `pytest-asyncio`.

## 13. Milstolpar

Allt körs först på Macen och flyttas sedan till Pi:n med `git pull` och systemd.

| # | Innehåll | Klart när |
|---|---|---|
| **M0** | Repo, skelett, ClaudeGate, DB. Pi: 64-bitars OS, Claude Code installerat och inloggat, uv. Bot via BotFather. | `claude -p "hej"` fungerar på Pi:n, och boten svarar "pong" |
| **M1** | Telegram ↔ beständig chatt-session, minnesfiler, startintervju, `record_feedback`, reaktioner | Agenten kommer ihåg vad du sa igår och sparar till minnet |
| **M2** | Kalenderkällor, tillgänglighet, frågor om okända pass | Den frågar om ett pass och kommer ihåg svaret |
| **M3** | En undersökning, triage med egenskaper, utkorg, leveransformer inkl. hylla, dispatcher med beslutsspår, nattro, `/tyst` `/nytt` `/varfor` | Ett fynd skickas i en lucka och inte under en föreläsning, och `/varfor` förklarar beslutet |
| **M4** | Manager med hypoteser, policy, ändringslogg, frågebudget, kontroll varannan vecka, förslag med knappar | En hypotes skapas, utvärderas och behålls eller backas med motivering |
| **M5** | GitHub-källan, systemd-drift på Pi:n, loggar | Agenten kör stabilt i ett dygn utan tillsyn |

Senare: uppmärksamhets-v2 (skuggläge, återspelning, bayesiansk mottaglighet), mejl, Canvas, todo-sidan, bloggen, länkspårning, sammanfattningar och att agenten utvecklar sig själv via Telegram.

## 14. Risker

- **Pro-taket:** Agenten och din egen användning av Claude delar kvot. Skydd: Haiku för det som körs ofta, pausade bakgrundsjobb när taket nås, och möjlighet att byta till en API-nyckel.
- **Inloggning med prenumeration i automatiska körningar:** Det fungerar för personligt bruk av din egen Claude Code-inloggning, men Anthropic kan ändra villkoren. Lösningen är att byta till en API-nyckel, och designen är redan förberedd för det.
- **Pi 400:s prestanda:** Med en session åt gången och utan lokala modeller bör det räcka gott. Det mäts i M0.
- **Integritet:** Minnet finns bara lokalt, Telegram-boten svarar bara ditt chat-id, och Claude-verktygen är låsta till `workspace/`.
