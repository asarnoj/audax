# Promptarna: förteckning och granskning

Status: steg B klart (alla promptar på engelska) · 2026-10-12 · hör till [ARCHITECTURE.md](ARCHITECTURE.md)

## Förteckning

| Prompt | Används av | När | Får in | Ger ut | Storlek |
|---|---|---|---|---|---|
| `claude_md.md` → `workspace/CLAUDE.md` | **alla** Claude-körningar (Claude Code laddar den automatiskt) | alltid | ägarens namn | — | 1,6k tecken |
| `chat.md` (+ style, state, interests) | chatten (Worker) | varje meddelande, varje utskick | samtalet + markörer | text till ägaren | 8,6k |
| `interview.md` | chatten | av sig själv när minnet är tomt, eller när ägaren ber om det (`interview_guide`) | — | frågor, minnesfiler, agentens namn | 1,9k |
| `self.md` | chattverktyget `how_i_work` | när ägaren frågar hur den fungerar | — | — | 4,6k |
| `triage.md` | triage | nya händelser | minne, policy, händelser | egenskaper + fakta-brief till chatten | 2,3k |
| `dispatcher.md` | dispatchern | var 10:e min vid ändring | läge, luckor, aktivitet, kandidater | beslut med skäl (chatten skriver utskicket) | 2,1k |
| `research.md` (djupsvepet som en rad i samma prompt) | sökuppdrag | per intervall, veckovis | mål, minne, frågor, tidigare fynd | fynd + bortvalda | 1,7k |
| `scout_queries.md` | sökportföljen | när portföljen är tunn | mål, bra frågor, befintliga | nya frågor | 1,3k |
| inbakad i `sources/pages.py` (engelska) | sidbevakning | när en sida ändrats | nya rader | saker | 0,6k |
| `manager.md` | Manager | varje natt + när den väcks | policy, samtal, feedback, hylla … | policy/stil/läge, frågor, journal, sammanfattning (läser minnet, skriver det inte) | 5,9k |
| `director.md` (gemensam bas) + `director_understand.md` | Director | varje natt + när den väcks | bild, minne, tid, samtal, uppdrag, fönster | bild/mål/trådar, uppdrag, sinnen | 5,5k + 0,8k |
| `director.md` + `director_reflect.md` | Director | mån, ons och lör | som ovan + idéutfall | idéer, delningar (fakta-brief till chatten), frågor | 5,5k + 2,8k |
| `builder.md` | byggläget | när ägaren ber om en ändring | önskad ändring | ändrade filer + sammanfattning | 2,0k |
| 22 verktygsbeskrivningar i `tools.py` (engelska) | chatten, Director | — | — | — | — |
| markörer i `chat.py`, `outbox.py`, `dispatcher.py` | chatten | varje runda | `[tid: …]`, `[schema: …]`, `[internt uppdrag]`, `[öppen fråga]` … | — | — |
| fältbeskrivningar i scheman | alla strukturerade körningar | — | — | — | — |

Varje anrop har dessutom cirka 13 000 tokens från Claude Code själv: verktygsdefinitioner och den egna systemprompten. Det är uppmätt och oberoende av våra promptar.

## Det som är fel, tvärs över alla

1. **`CLAUDE.md` laddas i *alla* körningar, men den är skriven som chattens persona.** Directorn får "Du är Director" i sin prompt *och* "Du är en personlig agent som pratar via Telegram … skriv kort" från CLAUDE.md. Triage, Manager och sökningen får samma motstridiga identitet. **Detta är det viktigaste felet.** CLAUDE.md ska beskriva den gemensamma världen (systemet, ägaren, minnets struktur, ägarskap, integritet), inte en roll.
2. **Vuxen bit för bit.** Särskilt `chat.md`: samma verktyg beskrivs på tre ställen, saker hamnar under fel rubrik ("Internt ser det ut så här" innehåller verktygsregler), och en rad är inaktuell ("schemalagda sökningar … har du inte ännu").
3. **Ägarskapet bryts i promptarna.** `chat.md` hårdkodar längd och ton ("~400 tecken"), men det är Managers `style.md` som äger rösten. Manager-prompten säger att Manager uppdaterar minnesfiler (profile, interests …), men enligt protokollet äger Worker minnet.
4. **Dubbletter mellan lagren.** Syfte och ton står i både `claude_md.md`, `chat.md` och `self.md`. Triage skriver utkast "i agentens ton", men chatten skriver om allt ändå (en röst). Dispatchern har kvar `combined_message`, som blev överflödig när chatten började skriva utskicken.
5. **Inaktuella hänvisningar till kommandon** (`/tyst`, `/fokus`, `/nytt`) i Manager-prompten, trots att ägaren inte vill använda kommandon. Signalerna finns kvar, men de ska beskrivas som signaler.
6. **Överbetoning.** Mycket fetstil, "aldrig" och "alltid". Nyare modeller följer sådant för bokstavligt och för brett. Det räcker att säga det en gång, med ett skäl.
7. **Språket.** Allt är på svenska. Beslut: motorns promptar skrivs på engelska. Det som ägaren läser och får (meddelanden, och troligen minnet och journalen) förblir på svenska, och det ska sägas uttryckligen i promptarna.

## Föreslagen struktur

- **Lager 0, gemensam värld (`CLAUDE.md`, genererad):** systemet med tre roller och en röst, ägaren, minnets karta och vem som äger vilken fil, integritet, och språkregeln. Ingen persona.
- **Lager 1, en rollprompt per anrop:** varje prompt har samma skelett:
  - *Role*: vem du är i systemet
  - *Goal*: vad som är ett bra utfall
  - *Inputs*: vad du får
  - *How to work*: principer, få och med skäl
  - *Output*: vad varje fält betyder
- **Lager 2, kontextblock:** konsekventa engelska markörer, dokumenterade på ett ställe, så att alla roller läser dem på samma sätt.

## Granskningsordning

1. Lager 0, `CLAUDE.md`, eftersom det påverkar allt annat
2. `chat.md` och markörerna, rösten
3. `triage.md` och `dispatcher.md`, filtret
4. `manager.md`
5. `director_understand.md` och `director_reflect.md`
6. `research.md`, `scout_queries.md` och sidextraktionen
7. `interview.md`, `self.md`, `builder.md` och verktygsbeskrivningarna
