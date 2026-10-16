# Arkitektur v2: en röst, tre hjärnor

Status: designförslag för diskussion · 2026-10-10 · ersätter rollfördelningen i [PLAN.md](PLAN.md) där de skiljer sig åt

## Varför v2

v1 kan hitta saker och reglera takten, men den **förstår** inte användaren. Den ser användaren genom chatten och schemat och tänker bara när något händer. Målet nu är tänkandet från *Her*: en assistent som löpande förstår vad användaren gör och kämpar med, som tar tid att tänka själv, och som ser potential han inte själv ser. Den föreslår, men gör aldrig något åt honom.

## Tre roller, var och en med sin egen fråga

| Roll | Frågan | Äger (bara den skriver) | Takt |
|---|---|---|---|
| **Worker** | *Vad händer, och vad säger vi?* | sinnen, händelser, triage, utkorg, **chatten (den enda rösten)** | hela tiden |
| **Director** | *Vad spelar roll för användaren, och vad ser jag som hen inte ser?* | `direction/` (levande bild, mål, trådar), uppdrag och sökfrågor, sinnen som inte kräver användaren | dagligen lätt, 2–3 gånger i veckan på djupet |
| **Manager** | *Känns det naturligt?* Upplevelsen, helt och hållet | `policy.md` (när och hur mycket), `style.md` (hur det låter), `state.md` (användarens läge) | varje natt, plus löpande signaler |

**Regeln som håller isär rollerna:** varje fil har exakt en ägare. Rollerna påverkar varandra genom att läsa varandras filer och lämna förfrågningar, aldrig genom att skriva i varandras filer.

## En röst

Bara Worker pratar med användaren, i en enda Telegram-chatt, i **en** pågående session:

- **Proaktiva meddelanden skrivs av chattsessionen själv.** När dispatchern (under Managers policy) bestämmer att något ska sägas nu, får chattsessionen ett internt uppdrag: "berätta kort om X". Svaret går till användaren som ett vanligt meddelande. Allt agenten säger finns därmed i samma samtal, med hela sammanhanget och samma stil.
- **Director och Manager når användaren via förfrågningar:** en fråga, ett förslag eller ett behov av en token. Varje förfrågan har ett *vad*, ett *varför* och en *ägare*. Dispatchern avgör när, och Worker väver in den naturligt, gärna när de ändå pratar.
- **Svaren sorteras tillbaka utan förvrängning.** Worker sparar användarens **exakta ord**, sin tolkning och vilken förfrågan det gällde, och skickar det till rätt ägare (minnet, Director eller Manager). Originalet finns alltid kvar, så en felaktig tolkning går att rätta.

Konflikter löses genom att det användaren senast uttryckligen har sagt gäller, och eftersom originalen sparas syns alltid varför.

## Hur delarna kommunicerar (protokollet)

Tre kanaler, och inga andra. Ingen roll anropar en annan direkt och ingen skriver i en annans filer.

### 1. Delat tillstånd: filer med en ägare
Varje fil har exakt en ägare som skriver. Alla andra läser.

| Fil | Ägare | Läses av |
|---|---|---|
| `direction/` (person, now, goals, threads, ideas) | Director | Worker (triage, sökning, chatt), Manager |
| `policy.md`, `style.md`, `state.md` | Manager | Worker (chatt, triage, dispatcher), Director |
| minnet om användaren (profile, interests, projects …) | Worker (chatten) | alla |
| instansens config (källor, uppdrag, bevakningslista) | Director, och Worker när användaren uttryckligen ber om det | Worker (sinnena) |

### 2. Anteckningar: Worker → Director/Manager
Det användaren säger som rör en annan roll skickas som en **anteckning**: användarens exakta ord, chattens tolkning, och vilken fråga det svarar på. Anteckningar behandlas vid mottagarens nästa körning.
- **`act_now`** väcker mottagaren nu i stället för vid nästa schemalagda körning. Det gäller Director när något ska drivas ("hjälp mig hitta X") och Manager vid tydlig feedback om upplevelsen ("det här blir för mycket"). Väckningar slås ihop och sker högst var 30:e minut per roll.
- När Worker själv ändrar config på användarens begäran (lägger till, stänger av, ändrar takt) får Director automatiskt en anteckning, så att dess bild stämmer.

### 3. Förfrågningar: Director/Manager → användaren (via Worker)
En roll som behöver något av användaren, som ett svar eller en åtkomst, lämnar en **förfrågan** med *vad* och *varför*. Dispatchern avgör när, och Worker ställer den med egna ord. Det finns en öppen fråga åt gången, för alla roller tillsammans. Svaret går tillbaka som en anteckning.

### Rytm
Rollerna, postgången, schemat och nattsvepet beskrivs nu i [MODEL.md](MODEL.md), som ersätter den här delen.

| Vad | När |
|---|---|
| Sinnen, triage, dispatcher | löpande; leveranser enligt schemat, kontrolleras varje minut |
| Nattsvepet: Assistant → Director → Researcher → Manager | en gång per natt, när ägaren brukar ha somnat |
| Director, reflektion | i nattsvepet måndag, onsdag och lördag morgon |
| Sökuppdrag | efter sitt intervall; djupsvep när Researcher ber om det |

Manager och Director påverkar varandra bara genom sina filer: Managers *läge* får Director att prioritera, och Directors *mål* styr vad Worker letar efter. Det gör strukturen enkel att resonera om. Varje ändring har en ägare, en kanal och ett spår i loggen.

## Director

**Läser:** chatten (via de sorterade svaren och journalen), schemat (hur tiden faktiskt används), utfall från sinnena, och efter hand fönstren mot användarens arbete: GitHub, anteckningar och texter, todo-sidan och bloggen. Alla fönster är skrivskyddade.

**Skriver i `direction/`:**
- `person.md`: **grunden.** Vem användaren är och vad hen vill: önskningar, intentioner, drivkrafter, värderingar, mönster. Det som är sagt skiljs från det som är slutsatser, och slutsatser är hypoteser med belägg.
- `now.md`: den levande bilden. Vad användaren lägger tid på, vad som rör sig och vad som står still, vad som skaver, vad som har hänt nyligen.
- `goals.md`: mål med djup analys. Varför målet spelar roll, vad som skulle hjälpa, vad som skulle vara värt att veta. **Worker läser filen vid varje sökning och triage.**
- `threads/*.md`: saker som drivs över tid (t.ex. "hitta en praktikplats"). Varje tråd har mål, läge, vad som har prövats, nästa steg och de uppdrag och sinnen som hör till.
- `ideas.md`: idéer och insikter med status (undersöks, föreslagen, landade, avfärdad).

**Reflektion** (2–3 gånger i veckan, djupt tänkande med den starkaste modellen) använder konkreta tankemönster:
- dolda tillgångar: något användaren gjort som har värde någon annanstans
- återkommande friktion
- glapp mellan mål och hur tiden används
- kopplingar mellan områden
- "vad skulle en klok mentor märka?"

En idé **undersöks innan den når användaren**: en riktad sökning kollar att tävlingen finns och att deadlinen inte har passerat. Att reflektionen ofta inte ger något är meningen.

**Mandat:** styr sökandet fritt (uppdrag, sökfrågor, fokus, sinnen som inte kräver något av användaren). Söktakten följer informationsbehovet, och Managers läge påverkar den. Nya trådar, större kursbyten och sådant som kräver användaren blir förfrågningar.

## Manager

**Huvudmål:** att allt känns naturligt för just användaren. Manager äger *när*, *hur mycket* och *hur det låter*, men inte *vad*.

**Information:**
- explicit feedback och avstämningar
- implicita signaler: svar, svarstid, reaktioner, tystnad, vad som hämtas från hyllan, /tyst
- UX-kommentarer som Worker sorterar vidare ordagrant
- kontext från schemat
- **en nattlig granskning av hela chattloggen**, som en UX-forskare

**Styr:**
- `policy.md`: takt och timing (som i v1, med hypoteser och experiment)
- `style.md`: stilguiden som Worker följer (längd, ton, struktur). Granskningen ändrar den **bara vid återkommande mönster med belägg**, i små steg. Den lagar inte det som fungerar, och varje ändring loggas och kan ångras.
- `state.md`: användarens läge just nu, t.ex. "tentavecka, mindre utrymme". Alla roller läser det. **Läget får inte göra systemet överkänsligt.** Det höjer ribban något men blockerar aldrig. Något verkligt värdefullt går fram även under tentaveckan.
- **Hyllan som aktivt lager:** det mesta från strömmarna hamnar där. Manager tar fram saker när det finns utrymme och låter gamla saker gå ut.

## Flöden

```
Sinnen ─► triage (läser goals.md, state.md) ─► utkorg ─► dispatcher (policy.md, state.md)
                                                              │ "säg det här nu"
Director ─ förfrågningar ─────────────────────────────────────►│
Manager  ─ förfrågningar ─────────────────────────────────────►│
                                                              ▼
                                  chattsessionen skriver meddelandet (style.md) ─► användaren
                                                              │
användarens svar ─► Worker sorterar (exakta ord + tolkning) ─► minnet / Director / Manager
```

## Att lägga till sinnen

| Vad | Hur |
|---|---|
| Ny instans (en sida, ett flöde, ett företag) | Säg det i chatten. Det är config, gäller direkt och kan ångras. Director gör samma sak på eget initiativ. |
| Ny typ (GitHub, todo-sidan, bloggen) | Byggs med Claude Code på datorn eller i byggläget. |
| Hemligheter | användaren lägger själv in dem i `.env` på Pi:n. Aldrig via Telegram. Agenten säger vilken variabel som behövs och hur man skapar en **skrivskyddad** nyckel. |

## Insyn

Produkten är chatten: "vad tror du jag håller på med?", "vilka trådar driver du?", "varför skickade du det?". Från datorn går allt i backend att läsa: klona minnesrepot från Pi:n och öppna `direction/`, `policy.md`, `style.md` och `state.md` i en editor.

## Byggordning (förslag)

1. **En röst:** proaktiva meddelanden skrivs av chattsessionen, förfrågningskön och sorteringen av svar.
2. **Manager v2:** `style.md` med nattlig granskning, `state.md` och hyllan som aktivt lager.
3. **Director:** `direction/`, trådar, sinnen och uppdrag från chatten, och att triage läser `goals.md`.
4. **Fönster:** GitHub först, sedan mejlen (Gmail, skrivskyddat, se [MAIL.md](MAIL.md)), anteckningar, todo-sidan och bloggen.
5. **Reflektion:** djupa tankekörningar som utforskar idéer innan de föreslås.
