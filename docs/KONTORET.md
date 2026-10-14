# Kontoret: ritprotokoll

Status: utkast 2026-10-14 · hör till [MODEL.md](MODEL.md) · ljusprototypen: `/prototyp/ljus` ([web/ljus.html](../web/ljus.html))

Kontorssidans huvudbild är kontoret ovanifrån: de fyra rollerna vid sina skrivbord, och allt de gör i realtid. Här står allt som behöver ritas, varför det finns (vilken del av systemet det visar), och hur arbetet delas: **ägaren ritar, Claude räknar, städar och animerar.**

## Arbetsgången

1. **Planskiss** (du): hela rummet ovanifrån i grå block, i Figma. Var står skrivborden, var är fönster, dörr, arkiv, telexen, planeringsbordet och hyllan. Inga detaljer.
2. **Gråskiss i motorn** (jag): skissen blir en scen med höjder, ljus och den riktiga datan. Du ser hur rummet lever innan något är ritat på riktigt.
3. **Höjder och profiler** (jag föreslår, du godkänner): för varje föremål en höjd och, där det behövs, en enkel profil från sidan. Det är de "osynliga vinklarna" som skuggorna räknas fram ur. Du ritar aldrig sidovyer.
4. **Riktiga ritningar** (du): föremål för föremål, ovanifrån, efter namnreglerna nedan.
5. **Städning och import** (jag): ritningarna görs motorfärdiga (se "Vad jag gör med dina ritningar").
6. **Figurer och animationer** sist (du ritar delarna, jag ger dem rörelse och rytm).

## Kameran, och vad det betyder för ritandet

- **Rakt ovanifrån, utan perspektiv.** Bara det som är vänt uppåt syns: bordsskivor, toppar, papper som ligger ned, huvuden och axlar.
- **Det som sitter på väggar syns inte.** En tavla på väggen är ett streck sett uppifrån. Allt som ska gå att läsa måste ligga ned: på ett bord, en hylla med öppen ovansida, golvet. (Se "Beslut som är dina": schemat och hyllan.)
- **Skala: 1 px = 1 cm.** Figma har bara pixlar, så en pixel får betyda en centimeter. Rummet i prototypen är 900 × 620 cm, alltså en ram på 900 × 620; ett skrivbord är 160 × 80. Decimaler går bra, och eftersom allt är vektorer spelar skalan ingen roll för skärpan.
- **Platta färger.** Varje form har en färg. Ljus och skuggor läggs på av motorn; rita dem inte. Mönster, linjer och detaljer går bra, de räknas som en del av formens färg.
- **Ljuset kommer från riktiga källor:** solen genom fönstren (den riktiga solen för Stockholm), skrivbordslampor, Assistentens skärm. Rita gärna fönster stora; de är rummets klocka.

## Vad jag gör med dina ritningar

- Rätar upp och sluter former, tar bort överlapp och lösa punkter.
- Delar upp konkava former i konvexa bitar (skuggmatematiken vill ha konvexa delar). Du behöver inte tänka på det.
- Lägger på höjd och profil, och väljer vilka delar som kastar skugga och vilka som bara tar emot ljus.
- Sätter fästpunkter för det som rör sig (armar, lådor, dörren) och platser i rummet (där en figur står när den lämnar ett papper).
- Anpassar färger mot ljuset: en färg som ser bra ut i Figma kan bli lerig i kvällsljus. Jag föreslår justeringar; du bestämmer.
- Skalar och linjerar mot rutnätet.

Allt detta ändrar formen så lite som möjligt. Det som är ditt uttryck (proportioner, konturer, ojämnheter) behålls.

## Vem pratar med vem (mätt, vecka 7–14 okt)

Layouten följer flödet: det som skickas oftest ska ha kortast väg.

| Flöde | Per vecka | Mellan |
|---|---|---|
| Nytt från sinnena in till triage | ≈ 390 | inkommande → Director |
| Triage slänger | ≈ 310 (80 %) | Director → papperskorgen |
| Triage släpper vidare | ≈ 80 | Director → Manager |
| Dispatchern sparar till senare | ≈ 65 | Manager → hyllan |
| Dispatchern planerar eller skickar | ≈ 65 | Manager → schemat / Assistant |
| Ägaren frågar efter sparat | ≈ 25 | hyllan → Assistant |
| Samtalet | ≈ 110 svar | ägaren ↔ Assistant |
| Sökuppdragens fynd | ≈ 25 | Researcher → inkommande |
| Post mellan rollerna (order, frågor, rapporter) | några få, mest nattetid | alla |

Det ger en kedja: **inkommande → Director → Manager → schema och hylla → Assistant → ägaren.** Researcher står vid början av kedjan (den matar in och sköter sinnena) och nära Director (order och rapporter). Arkivet används av alla, mest av Assistant och Director.

## Rummet

| Föremål | Visar i systemet | Förslag på mått (cm), höjd | Att tänka på |
|---|---|---|---|
| Golv | — | 900 × 620 | Mönster går bra (plankor, matta). Ljuset ritar på det. |
| Väggar | — | 25 tjocka, 270 höga | Bara ovansidan syns: en mörk ram. |
| Fönster | dygnet, vädret senare | 140–180 breda, bröstning 85, överkant 225 | Glaset och karmen ovanifrån. De avgör var solen landar. |
| Dörr | det som kommer utifrån; byggläget | 90 bred | Ett dörrblad som kan svänga (fästpunkt i gångjärnet). |
| Telexen (inkommande) | sinnena: flöden, sidor, API:er och websockets som bevakas mekaniskt, utan att någon söker | maskin med pappersremsa, 50 × 40, höjd 30 | Skriver ut av sig själv när något kommer in. Researcher ställer in den (vilka källor), Director läser det den skriver ut (triage). Remsan som hänger ut visar vad som väntar. |
| Arkivskåpet | minnet (`memory/`) | 140 × 50, höjd 130 | Lådor som dras ut (syns ovanifrån när de är ute). En låda per roll: varje roll äger sina filer. |
| Planeringsbordet | Managers schema: fynd med en planerad tid och bokningar (#B) | stort bord, cirka 160 × 90, höjd 75 | En tidslinje där lappar läggs, flyttas och plockas. Manager lägger fynd, Assistant lägger bokningar och löften och plockar det som ska ut. |
| Hyllan | sparade fynd: värda att ha men inte värda att störa för | låg hylla med öppen ovansida, eller en del av planeringsbordet | Manager lägger dit, Assistant plockar när ägaren frågar "något nytt?" eller när det passar. Det som blir inaktuellt rensas bort. (Se beslut.) |
| Papperskorgen | bortsorterat (fynd som släpps) | rund, ø 30, höjd 40 | Pappersbollar syns ovanifrån. |

## Skrivborden (ett per roll)

Ett skrivbord ritas en gång och varieras per roll med det som ligger på det.

| Föremål | Visar | Mått, höjd |
|---|---|---|
| Skrivbord | rollens plats | 160 × 80, höjd 75 |
| Stol | sitter eller inte | sits 48 × 44, höjd 47; ryggstöd 92 |
| Lampa | arbetar på natten | fot ø 18; skärm ø 24 på cirka 115–125 |
| Inkorg | rollens oläst post (#P) | 34 × 26, höjd 8; papperen i den syns |
| Anteckningsbok | rollens arbetsminne | 15 × 21, platt |
| Papper i arbete | det rollen gör just nu | A4 ≈ 21 × 30, platt |

Per roll:
- **Assistant**: datorn med skärm och tangentbord, kanalen ut till ägaren. Skärmen lyser (motorn gör ljuset).

Regeln för skärmar: **en skärm är en förbindelse ut ur kontoret.** Assistants går till ägaren, Researchers till webben. Allt annat är papper.
- **Director**: kuvert, de lästa mejlen. Kanske en stämpel för triage (värt något / inte).
- **Researcher**: en dator, för sökuppdragen (Claude som söker och läser på webben). Och kartorna, en per sökuppdrag, kanske utbredda på bordet.
- **Manager**: närmast schemat. Kanske en klocka eller en kalender på bordet.

## Figurerna

Fyra figurer, en per roll, ovanifrån. Identiteten sitter i huvudet och axlarna (hår, mössa, kläder), eftersom det är det som syns.

Delar att rita, var för sig:
- **Huvud** (cirka ø 21–23)
- **Bål och axlar** (cirka 50 × 28)
- **Överarm, underarm och hand**, höger och vänster (två delar per arm, så att figuren kan skriva och sträcka sig)
- **Skor** (cirka 11 × 26); benen syns knappt ovanifrån
- **Sittande**: lår och knän som sticker fram under bålen när figuren sitter

Markera var varje del sitter fast (en liten punkt räcker; jag sätter de exakta fästpunkterna). Rita delarna i viloläge; rörelserna gör jag.

Bra att veta: ju enklare delar, desto bättre rör de sig. Rytmen i gången (fötter som pendlar, armar som svänger, axlar som vrider sig) kommer från rörelsen, inte från ritningen.

## Saker som flyttas mellan rollerna

| Föremål | Visar |
|---|---|
| Lapp, olika sorter | en post (#P): anteckning, fråga, svar, order, återkoppling, rapport. Sorterna kan skiljas åt med färg eller form. |
| Fyndkort | ett fynd (#F) på väg genom kedjan |
| Mapp | en minnesfil som hämtats ur arkivet |
| Kuvert | ett mejl (bara hos Director) |
| Bokningslapp | en bokning (#B) på schemat |

## Från systemet till kontoret

Det som händer i systemet blir korta följder av handlingar. Animationen ligger några sekunder efter verkligheten och väntar aldrig ut den: ett svar till ägaren går iväg direkt, figuren hinner ikapp.

| I systemet | I kontoret |
|---|---|
| Ägaren skriver (Telegram) | Assistentens skärm lyser; Assistant sätter sig vid datorn och skriver |
| Assistant svarar | skriver på tangentbordet |
| Något nytt kommer in från ett sinne | telexen skriver ut en remsa |
| Triage (Director) | Director river av remsan, läser, stämplar; värt något → till Managers inkorg, annars papperskorgen (det mesta) |
| Dispatchern (Manager) | Manager läser fyndet och sätter det på schemat vid rätt tid, lägger det på hyllan, eller ger det direkt till Assistant |
| Utskick vid planerad tid | Manager tar lappen från schemat och lämnar den till Assistant, som skriver |
| "Något nytt?" | Assistant hämtar från hyllan |
| Ett sparat fynd blir inaktuellt | det plockas bort från hyllan |
| En post som får väcka mottagaren (ägarens ord) | avsändaren går genast fram och räcker den; mottagaren tittar upp och börjar |
| Vanlig post | läggs i det egna utfacket och bärs över när figuren ändå reser sig |
| En roll läser en minnesfil | går till arkivet, drar ut sin låda, tar en mapp |
| En roll skriver i sin fil | mappen ligger på bordet; figuren skriver i den och ställer tillbaka den |
| Arbetsminnet skrivs om | skriver i anteckningsboken |
| Sökuppdrag (Researcher) | Researcher vid datorn; fynden läggs vid telexen, där triage tar dem som allt annat |
| Researcher planerar | lutar sig över kartorna, ritar på dem |
| Director läser mejl | öppnar kuvert vid bordet |
| Bokning görs eller flyttas | Manager sätter upp eller flyttar en lapp på schemat |
| Nattkörningen | lamporna tänds en i taget i nattkörningens ordning: Assistant, Director, Researcher, Manager |
| Kvoten pausad | se beslut |
| Byggläget bygger en ändring | se beslut |

Handlingarna figurerna behöver: sitta, resa sig, gå, vända sig, skriva för hand, skriva på tangentbord (Assistant), läsa, tänka (luta sig bakåt), räcka fram, lägga ned, plocka upp, dra ut och skjuta in en låda, sätta upp och ta ned en lapp, stämpla, kasta. Varje handling är rörelser av delarna ovan; inget av det kräver egna bilder.

## Hela kontorets lägen

- **Dygnet**: solen genom fönstren, färgen från gryning till kväll, blå timme, natt med lamporna (klart i prototypen).
- **Rummet visar läget även när ingen rör sig**: högen i varje inkorg är antalet olästa poster, remsan i telexen det som väntar på triage, lapparna på schemat de kommande bokningarna och fynden, mapparna på hyllan det sparade.

## Beslut som är dina

1. **Layouten**: kedjan ovan föreslår en ordning. Var dörren, fönstren och arkivet hamnar är ditt.
2. **Planeringsbordet**: ett eget bord mellan Manager och Assistant, eller en del av Managers skrivbord?
3. **Hyllan**: en egen hylla, eller en hög "när det passar" på planeringsbordet bredvid tidslinjen?
4. **Natten**: sitter rollerna kvar och arbetar i lampljuset, går de hem mellan sina pass, eller sover de vid bordet?
5. **Kvoten pausad**: släckt i taket och figurerna väntar, en skylt på dörren, eller något annat?
6. **Byggläget**: en hantverkare som kommer in genom dörren när en ändring byggs, och går när du har tryckt "Driftsätt"?
7. **Ägaren själv**: syns du i kontoret, eller är Assistentens skärm fönstret ut mot dig?

## Namn i Figma

- En ram per föremål, namngiven efter tabellerna ovan: `skrivbord`, `lampa`, `arkiv`, `inkorg`.
- Per roll med prefix: `director/skrivbord`, `figur-director/huvud`, `figur-director/arm-vänster-över`.
- Platser i rummet som små cirklar: `plats/director-stol`, `plats/director-inkorg`, `plats/arkiv-director`.
- Allt i 1 px = 1 cm, ovanifrån, i viloläge.

Exportera som SVG; jag tar det därifrån.

## Första omgången

Det minsta som får kontoret att leva:
1. Planskissen av hela rummet.
2. Ett skrivbord, en stol, en lampa och en inkorg (används av alla fyra).
3. Arkivskåpet, planeringsbordet och telexen.
4. Papper och lapp.
5. En figur (delarna), som först får vara alla fyra.

Resten (rollernas egna föremål, figurernas identiteter, hyllan, papperskorgen, dörren) kommer i nästa omgång.
