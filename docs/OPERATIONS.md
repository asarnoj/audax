# Drift: vad som kör, vad det kostar, vad som händer när något går fel

Status: granskad 2026-10-11 · hör till [ARCHITECTURE.md](ARCHITECTURE.md)

## Vad som kör

**En enda Python-process** (`agent.main`) på Pi:n, som systemd-tjänsten `agent`. Den är motorn och innehåller:
- **en schemaläggare (APScheduler), som crontab inuti processen**: Manager, Director, dispatcher, sökuppdrag, kalendersynk, hälsovakt, säkerhetskopia och ett livstecken var 5:e minut. Systemets crontab används inte.
- **sinnen** som lätta uppgifter i samma process: pollers (RSS, sidor, API:er, ämnen) och websocket-lyssnare.
- **Telegram-boten**.

**Claude anropas bara som Claude Code i projektmappen**, via Claude Code CLI på Pi:n med ägarens prenumeration (aldrig API-nyckel, aldrig Claudes egna schemalagda tjänster eller routines). Varje sorts anrop finns i [agent/calls.py](../agent/calls.py). Ett test ser till att kartan stämmer med koden.

Se det live:
- `./deploy/audax jobs`: livstecken, nästa körning för varje jobb, sinne och uppdrag
- `./deploy/audax claude`: varje sorts Claude-anrop med prompt, modell, verktyg, utlösare, och senaste veckans körningar, kostnadsandel och storlek på sammanhanget

## Kvoten

Prenumerationen har ett tak. Det som förbrukar den är främst **hur mycket sammanhang varje anrop skickar**, och om cachen hinner gå ut mellan anropen.
- Bakgrundsarbetet är billigt: triage, dispatcher och sidläsning körs med Haiku och utan sessioner.
- Chatten är dyrast eftersom den är en pågående session. Den 11 okt stod den för 91 % av förbrukningen, med cirka 109 000 tokens sammanhang per svar. **Åtgärd:** Manager skriver varje natt en sammanfattning, och om chattens sammanhang har vuxit över 60 000 tokens startar sessionen om med sammanfattningen och de senaste meddelandena.
- När Claude Code varnar för att kvoten tar slut pausas bakgrundsjobben, och chatten fortsätter (`claude_gate.py`).

## Felvägar

| Händelse | Vad som händer | Belägg |
|---|---|---|
| Processen kraschar | systemd startar om efter 10 s (`Restart=on-failure`) | `deploy/agent.service` |
| Pi:n startas om | tjänsten är aktiverad och startar; missade jobb (t.ex. Manager kl. 03:30) körs när motorn är uppe igen | `misfire_grace_time=None` i `main.py` |
| Claude svarar inte eller ger fel | körningen loggas som fel. Dispatchern går över till en regelbaserad reserv (bara brådskande skickas). Chatten säger till. Hälsovakten larmar vid upprepade fel | `test_dispatcher_falls_back_when_claude_fails`, `test_health_flags_repeated_claude_failures_only` |
| Kvoten tar slut | bakgrundsjobb pausas tills den återställs, och hälsovakten berättar det | `test_rate_limit_warning_pauses_background_but_not_chat` |
| Nätet går ner | sinnena loggar felet och försöker igen vid nästa intervall. Telegram-boten återansluter av sig själv | stream_stats.errors, sett i drift 9 okt |
| En webbsida blockerar | `web_read` prövar API, direkt hämtning och läsartjänst, och minns vad som fungerar per domän | `tests/test_web.py` |
| Ett utskick misslyckas | kandidaten är kvar i utkorgen och skickas vid nästa körning; om chattsessionen inte kan skriva skickas utkastet som det är | `test_delivery_goes_through_the_voice_and_falls_back` |
| Trasig inställningsfil under drift | den gamla inställningen behålls | `test_watcher_calls_back_on_change_and_survives_errors` |
| Trasig inställningsfil vid start | motorn startar ändå (utan de källor som inte går att läsa) och hälsovakten larmar via Telegram | `test_broken_sources_yaml_is_not_fatal_and_is_reported` |
| En ändring från byggläget som kraschar efter omstart | testerna och en importkontroll körs före driftsättning; om det ändå går fel: `./deploy/audax rollback` backar senaste commit och startar om | `deploy/audax` |
| SD-kortet går sönder | minnet, instansen och databasen säkerhetskopieras dagligen till Macen; instansen pushas till ett privat repo | [BACKUP.md](BACKUP.md) |
| Två motorer lyssnar på samma bot (t.ex. Macen och Pi:n) | Telegram ger konfliktfel. Kör bara en motor åt gången | — |

## Kända luckor

- **Ingen extern vakt.** Om motorn inte startar alls (t.ex. trasig kod eller full disk) kan den inte larma själv. `audax jobs` visar ett inaktuellt livstecken. En vakt på en annan maskin skulle kunna larma, men den finns inte än.
- **Ingen automatisk återställning** efter en trasig driftsättning. Det sker manuellt med `audax rollback`.
