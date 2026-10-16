# Drift: vad som kör, vad det kostar, vad som händer när något går fel

Status: granskad 2026-10-11 · hör till [MODEL.md](MODEL.md)

## Vad som kör

**En enda Python-process** (`agent.main`) på Pi:n, som systemd-tjänsten `agent`. Den är motorn och innehåller:
- **en schemaläggare (APScheduler), som crontab inuti processen**: Manager, Director, Researcher (planering och sökuppdrag), schemats tick varje minut, kalendersynk, hälsovakt, säkerhetskopia och ett livstecken var 5:e minut. Systemets crontab används inte.
- **sinnen** som lätta uppgifter i samma process: pollers (RSS, sidor, API:er, ämnen) och websocket-lyssnare.
- **Telegram-boten**.

**Claude anropas bara som Claude Code i projektmappen**, via Claude Code CLI på Pi:n med ägarens prenumeration (aldrig API-nyckel, aldrig Claudes egna schemalagda tjänster eller routines). Varje sorts anrop finns i [agent/calls.py](../agent/calls.py). Ett test ser till att kartan stämmer med koden.

Se det live:
- `./deploy/audax jobs`: livstecken, nästa körning för varje jobb, sinne och uppdrag
- `./deploy/audax claude`: varje sorts Claude-anrop med prompt, modell, verktyg, utlösare, och senaste veckans körningar, kostnadsandel och storlek på sammanhanget

## Kontorets sida

En webbsida i samma process (`agent/site.py`, data i `agent/office.py`). På `/` det illustrerade kontoret ovanifrån ([KONTORET.md](KONTORET.md)): läget just nu och det som händer, i realtid, och bakom varje klick innehåll och historik. Under `/data/` rådatan i enkla sidor: vem som arbetar nu och kvoten, de senaste körningarna (#R) med sina steg, ärenden följda via id (#P, #S, #F, #B), filerna (minnet och instansens inställningar) och alla tabeller. Sidans fasta text är engelska. Sidan kan bara läsa.

- Motorn lyssnar bara på `127.0.0.1:8400` (`site:` i settings.yaml). **nginx** på Pi:n gör den nåbar på Pi:ns Tailscale-adress, port 8400, och ingen annanstans (inte hemnätet): `/etc/nginx/sites-available/audax` med `listen <tailscale-ip>:8400;` och `proxy_pass http://127.0.0.1:8400;`. Öppna `http://<pi-namnet>:8400` på en enhet i tailnetet.
- `.env` ligger utanför arkivet, och dolda filer (`.git`) visas inte.
- Inga Claude-körningar når sidan: netguard vägrar Pi:n själv.

## Kvoten

Prenumerationen har ett tak. Det som förbrukar den är främst **hur mycket sammanhang varje anrop skickar**, och om cachen hinner gå ut mellan anropen.
- Bakgrundsarbetet är billigt: triage, dispatcher och sidläsning körs med Haiku och utan sessioner.
- Chatten är en pågående session, så varje svar skickar med hela samtalet (i dag upp till cirka 110 000 tokens). Det mesta läses från cachen och kostar lite; det dyra är när cachen har hunnit gå ut. Assistants nattpass skriver varje natt en sammanfattning, och om sammanhanget har vuxit över 60 000 tokens startar sessionen om med sammanfattningen och de senaste meddelandena.
- **Mätfel, rättat 12 okt:** Claude Code rapporterar kostnaden för en återupptagen session som en löpande summa. Varje chattsvar loggades därför med alla tidigare svars kostnad, och chatten såg ut att stå för 90 % och kosta över $2 per svar. Nu loggas varje körnings egen kostnad (och historiken är omräknad). Vid kvotvarningen 11 okt (91 %) hade Audax bara använt cirka $1,70 av fönstret; resten var annan användning på kontot.
- **Kostnad i dollar är API-ekvivalent**, alltså vad det hade kostat via API:t. Prenumerationen debiteras inte per dollar, men beloppet är ett jämförbart mått på hur mycket av kvoten något tar. Fönstren (5 timmar och vecka) delas med all annan Claude-användning på kontot, och Claude Code säger hur fulla de är först nära taket. `audax usage` visar Audax andel bredvid senast kända nivå.
- När Claude Code varnar för att **5-timmarsfönstret** tar slut pausas bakgrundsjobben tills det återställs, och chatten fortsätter (`claude_gate.py`).
- Varnar den för **veckan** under 90 % väntar bara det tunga och valfria (sökuppdrag, djupsvep, reflektion) tills veckan återställs. Sinnena, triage och dispatchern fortsätter, eftersom de kostar lite och ägaren annars missar saker i flera dagar. Över 90 % väntar allt i bakgrunden. Veckan delas med ägarens egen Claude-användning.

## Felvägar

| Händelse | Vad som händer | Belägg |
|---|---|---|
| Processen kraschar | systemd startar om efter 10 s (`Restart=on-failure`) | `deploy/agent.service` |
| Pi:n startas om | tjänsten är aktiverad och startar; nattsvepet fortsätter där det slutade om motorn kommer upp under natten | `misfire_grace_time=None` i `main.py` |
| Claude svarar inte eller ger fel | körningen loggas som fel. Dispatchern går över till en regelbaserad reserv (bara brådskande skickas). Chatten säger till. Hälsovakten larmar vid upprepade fel | `test_dispatcher_falls_back_when_claude_fails`, `test_health_flags_repeated_claude_failures_only` |
| Kvoten tar slut | bakgrundsjobb pausas tills den återställs, och hälsovakten berättar det | `test_rate_limit_warning_pauses_background_but_not_chat` |
| Nätet går ner | sinnena loggar felet och försöker igen vid nästa intervall. Telegram-boten återansluter av sig själv | stream_stats.errors, sett i drift 9 okt |
| En webbsida blockerar | `web_read` prövar API, direkt hämtning och läsartjänst, och minns vad som fungerar per domän | `tests/test_web.py` |
| Ett utskick misslyckas | kandidaten släpps tillbaka till utkorgen och skickas vid nästa körning; om chattsessionen inte kan skriva skickas utkastet som det är | `test_failed_delivery_releases_and_crash_recovery` |
| Samma sak når kedjan två gånger samtidigt (12 okt: tre dubbla meddelanden) | triage och dispatchern kör en i taget (lås); varje händelse kan bli högst en kandidat (unikt index); varje kandidat låses innan den skickas och kan bara skickas en gång | `test_concurrent_dispatcher_runs_deliver_a_candidate_once`, `test_concurrent_triage_makes_one_candidate_per_event` |
| Samma nyhet från två källor | triage ser vad som redan är skickat, väntar eller ligger på hyllan och sorterar bort det som inte säger något nytt; chatten kan också avstå ett utskick som redan är sagt i samtalet | `test_triage_sees_what_was_already_sent`, `test_chat_can_decline_a_delivery_it_already_made` |
| Något loopar och skickar för mycket | en mekanisk säkring håller inne utskicken vid fler än 6 egna meddelanden på en timme (inte en UX-regel); hälsokollen säger till | `test_breaker_holds_delivery_when_something_loops` |
| Motorn startar om mitt i ett utskick | vid start: kandidater som hann skickas markeras skickade, övriga går tillbaka till utkorgen | `test_failed_delivery_releases_and_crash_recovery` |
| Telegram ber om paus eller nätet hackar | nytt försök när meddelandet säkert inte gick iväg; vid timeout görs inget nytt försök (då kan det redan ha gått fram) | `telegram_bot._send_retrying` |
| Chatten märker ett fel hos sig själv | den rapporterar det med `report_problem`, och det syns i hälsokollen (`audax health`) | — |
| Trasig inställningsfil under drift | den gamla inställningen behålls | `test_watcher_calls_back_on_change_and_survives_errors` |
| Trasig inställningsfil vid start | motorn startar ändå (utan de källor som inte går att läsa) och hälsovakten larmar via Telegram | `test_broken_sources_yaml_is_not_fatal_and_is_reported` |
| En ändring från byggläget som kraschar efter omstart | testerna och en importkontroll körs före driftsättning; om det ändå går fel: `./deploy/audax rollback` backar senaste commit och startar om | `deploy/audax` |
| SD-kortet går sönder | minnet, instansen och databasen säkerhetskopieras dagligen till Macen; instansen pushas till ett privat repo | [BACKUP.md](BACKUP.md) |
| Två motorer lyssnar på samma bot (t.ex. Macen och Pi:n) | Telegram ger konfliktfel. Kör bara en motor åt gången | — |

## Säkerhet

- **Bara den publika webben.** Allt som kan följa en adress från en sida, ett mejl eller en modell (web_read, sinnena, kontroll av nya källor) går genom `agent/netguard.py`. Den vägrar Pi:n själv, hemnätverket och tailnet, även efter omdirigeringar.
- **Mejl och webben blandas aldrig i samma körning.** En körning som har läst mejl kan inte nå nya adresser på webben, så att ett mejl från en främling inte kan få Audax att föra ut privat data i en adress. I chatten gäller det resten av rundan efter att den läst ett mejl. Directors nattliga genomgång läser mejlen men har ingen webbläsning, och den lägger inte till källor själv: en källa den ser blir en beställning till Researcher, som aldrig läser mejl och kontrollerar adressen. Kvar finns en indirekt väg: något från ett mejl kan hamna i en fil (t.ex. Directors bild av dig) som en senare körning med webben läser. Den är smal, men den finns.
- **Innehåll från andra är data.** Webbsidor och mejl lämnas till Claude som information, aldrig som instruktioner (lager 0, och märkt i verktygens svar).
- **Bara läsrätt utåt.** GitHub- och Gmail-nycklarna kan bara läsa; Gmail-nyckeln vägras om den kan mer. Inget verktyg skickar något åt ägaren.
- **Ändringar av motorn** kräver ägarens knapptryck, och byggläget har ingen åtkomst till hemkatalogen.

## Kända luckor

- **Ingen extern vakt.** Om motorn inte startar alls (t.ex. trasig kod eller full disk) kan den inte larma själv. `audax jobs` visar ett inaktuellt livstecken. En vakt på en annan maskin skulle kunna larma, men den finns inte än.
- **Ingen automatisk återställning** efter en trasig driftsättning. Det sker manuellt med `audax rollback`.
