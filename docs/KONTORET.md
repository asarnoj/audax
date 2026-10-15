# Kontoret: ritprotokoll

Status: utkast 2026-10-16, kopplat till systemet · hör till [MODEL.md](MODEL.md) · gråskissen: `/prototyp/kontor` ([web/kontor.html](../web/kontor.html)) · ljusprototypen: `/prototyp/ljus`

Kontorssidans huvudbild är kontoret ovanifrån: de fyra rollerna vid sina skrivbord, och allt de gör i realtid. Här står allt som behöver ritas, varför det finns (vilken del av systemet det visar), och hur arbetet delas: **ägaren ritar, Claude räknar, städar och animerar.**

## Arbetsgången

1. **Planskiss** (du): hela rummet ovanifrån i grå block, i Figma. Var står skrivborden, arkivet, telexen, kalendern och board. Inga detaljer. (Klar 15 okt, se Planlösningen.)
2. **Gråskiss i motorn** (jag): skissen blir en scen med höjder, ljus och den riktiga datan. Du ser hur rummet lever innan något är ritat på riktigt.
3. **Höjder och profiler** (jag föreslår, du godkänner): för varje föremål en höjd och, där det behövs, en enkel profil från sidan. Det är de "osynliga vinklarna" som skuggorna räknas fram ur. Du ritar aldrig sidovyer.
4. **Riktiga ritningar** (du): föremål för föremål, ovanifrån, efter namnreglerna nedan.
5. **Städning och import** (jag): ritningarna görs motorfärdiga (se "Vad jag gör med dina ritningar").
6. **Figurer och animationer** sist (du ritar delarna, jag ger dem rörelse och rytm).

## Kameran, och vad det betyder för ritandet

- **Rakt ovanifrån, utan perspektiv.** Bara det som är vänt uppåt syns: bordsskivor, toppar, papper som ligger ned, huvuden och axlar.
- **Det som sitter på väggar syns inte.** En tavla på väggen är ett streck sett uppifrån. Det som ska gå att se måste ligga ned eller luta mot kameran, som board och kalendern.
- **Skala: 1 px = 1 cm.** Figma har bara pixlar, så en pixel får betyda en centimeter. Rummet i prototypen är 900 × 620 cm, alltså en ram på 900 × 620; ett skrivbord är 160 × 80. Decimaler går bra, och eftersom allt är vektorer spelar skalan ingen roll för skärpan.
- **Platta färger.** Varje form har en färg. Ljus och skuggor läggs på av motorn; rita dem inte. Mönster, linjer och detaljer går bra, de räknas som en del av formens färg.
- **Ljuset kommer från riktiga källor:** dagsljuset från sidan, skrivbordslamporna, skärmarna.

## Vad jag gör med dina ritningar

- Rätar upp och sluter former, tar bort överlapp och lösa punkter.
- Delar upp konkava former i konvexa bitar (skuggmatematiken vill ha konvexa delar). Du behöver inte tänka på det.
- Lägger på höjd och profil, och väljer vilka delar som kastar skugga och vilka som bara tar emot ljus.
- Sätter fästpunkter för det som rör sig (armar, lådor) och platser i rummet (där en figur står när den lämnar ett papper).
- Anpassar färger mot ljuset: en färg som ser bra ut i Figma kan bli lerig i kvällsljus. Jag föreslår justeringar; du bestämmer.
- Skalar och linjerar mot rutnätet.

Allt detta ändrar formen så lite som möjligt. Det som är ditt uttryck (proportioner, konturer, ojämnheter) behålls.

## Planlösningen (15 okt)

- **Manager** uppe till vänster, med **kalendern** längs vänsterväggen och **board** överst i mitten, båda intill sig.
- **Director** uppe till höger, vid ett L-format bord med **telexen** (nya papper kommer ut där) och **papperskorgen** precis bredvid.
- **Assistant** nere till vänster, nära kalendern och arkivet.
- **Researcher** nere till höger, under Director.
- **Arkivet** längst ned i mitten: fyra lådor, en per roll.
- **Inga fönster och ingen dörr.** På dagen kommer ett **naturligt ljus från sidan** över hela rummet: riktningen följer den riktiga solen (norr uppåt i bild: från höger på morgonen, nedifrån mitt på dagen, från vänster på kvällen), vinkeln är stiliserad som om horisonten låg högre (ungefär 32° i gryning och skymning, 60° mitt på dagen, året runt, så skuggorna aldrig blir oändligt långa) och färgen varm i början och slutet av dagen. Bara möblerna skuggar. På natten lyser skrivbordslamporna.
- **Golvet**: ljusgult, med rödbruna rutstreck (50 cm) i låg opacitet som löper en bit ut över golvets kant. Inga väggar: kontoret smälter ut i sidan, och **sidans bakgrund är marken i rummets ljus** (varm på morgonen, neutral mitt på dagen, grå när det är mulet, mörk på natten).
- **Moln**: ett reglage för hur mulet det är. Lägre kontrast: mindre direkt sol, mer jämnt himmelsljus, lite svalare färg; skuggorna behåller formen och blir ljusare. Kan senare styras av det riktiga vädret.
- **Möbler byggs av sina delar**, som de riktiga: en tunn skiva på ben, så att ljuset går under och skuggan blir skivans form plus tunna streck från benen. Ett massivt block från golvet ger tunga, felaktiga skuggor. Bara det som verkligen är massivt (arkivet, papperskorgen) är block.
- **Stolarna**, en för varje person: Manager en grågrön stålstol, fyrkantig och prydlig; Director en stor mörk chefsstol med hög rygg; Assistant en ljus rund stol; Researcher en enkel trästol på fyra ben. Kontorsstolarna har fem ben med hjul som syns utanför sitsen. Ryggen står på sitsens kant hela vägen (på en rund sits som en korda).
- **Lamporna**, en för varje person: Researcher en enkel Strindberglampa (rund fot, bred rund mässingsskärm), Manager en grågrön arkitektlampa (fot, två armar, konisk skärm ut över bordet), Director en bankirlampa (avlång grön glasskärm på mässingsfot), Assistant en liten krämvit svamplampa. Ljuset kommer från under skärmen och är riktat nedåt, så det stannar runt borden.
- **Telexen** på Directors sidobord, mer som en skrivare: en ljus kropp, papper som står redo bak, en utmatningsspringa, en liten panel och en färsk utskrift framför.
- **Inkorgarna**: samma enkla mörka brevkorg på varje bord, med ett par lösa papper i. Senare: lika många papper som olästa poster.
- **Papperskorgen** vid Director: en metallkorg som är vidare upptill, mörk inuti, med en liten hög skrynklade papper överst.
- **Lite snett**: stolarna är utdragna en bit från borden och vridna några grader, som om någon nyss satt där; datorer och småsaker står nästan raka; de stora möblerna står raka.
- **Sittgruppen** i mitten: en hörnsoffa, en fåtölj vänd mot soffans hörn och en golvlampa med veckad skärm runt om, öppen upptill; på natten lyser skärmens insida och lampan ger ljus åt sittgruppen.
- **Figurerna**, platta och enkla i stil med referensbilden: sedda uppifrån men lite framåtlutade, så att bröstet syns framför huvudet (kavajen, skjortans V, slipsen), armarna fram mot bordet och händerna på tangentbordet. Manager grågrön kostym och mörk slips, mörkt kammat hår; Director koksgrå kostym, röd slips, grått hår; Assistant senapsgul kofta, ljust längre hår; Researcher brun tweedkavaj, ljusblå skjorta, rufsigt rödbrunt hår. Director är just nu den som går omkring.
- **Arkivet**: ett lågt skåp med fyra öppna lådor, en per roll, fulla av gula hängmappar i rader; ovanifrån syns mapparnas överkanter och små ljusa etiketter som sticker upp, och mapparna skuggar varandra.
- **Lampornas ljus** räknas fram en gång mot de stillastående möblerna och sparas som en bild (vid laddning och när fönstret ändrar storlek); när lamporna tänds läggs bilden bara på. Den gående figuren tar lampljuset där den står men kastar ingen egen lampskugga.
- **Datorerna**, för dem som går ut på nätet: Researcher har en gammal beige stationär (djup skärm, torn, tangentbord), Assistant en laptop med skärmen uppfälld. Manager går aldrig ut på nätet och har ingen. **Director lånar Researchers dator**: när Directors körning läser mejl eller går ut på webben eller GitHub (den dagliga genomgången och reflektionerna, i nattkörningen) går Director till Researchers bord och läser där. Det är sant: i natten kör Director före Researcher, och sökuppdragen körs inte på natten, så datorn står ledig. Väcks Director på dagen medan Researcher sitter vid datorn, väntar Director i kön. Programmet ändras inte: mejlen måste ligga kvar hos Director (en körning som läst mejl når aldrig nya webbadresser, och Manager har ingen mejlåtkomst).
- **Skrivborden** (första versionen, bara form och färg): Assistant ljus björk med mjuka hörn, Director mörk valnöt i L, Researcher ett långt arbetsbord i ek, Manager ett salviagrönt stålskrivbord med snäva hörn.
- **Lutande ytor** (board, kalendern, laptopskärmen) tar emot skuggor på sitt eget plan, med rätt höjd i varje punkt, och får mer eller mindre sol efter hur de vetter mot den. Det som ligger på dem är förkortat i lutningens riktning (post-its blir breda remsor, ramens över- och underkant tunnare än sidorna); det är det som gör att ögat ser lutningen.
- **Ramen** fyller hela fönstret, rummet i mitten; marken och skuggorna fortsätter ut till kanterna. Testinställningarna ligger i en liten panel som kan döljas.
- **Board och kalendern lutar** mot rummet, så att man ser några post-its och dagarnas streck. Det räcker att se vad möbeln är; innehållet visas när man klickar på den.

## Kopplat till systemet (16 okt)

Kontoret visar läget just nu och följer sedan det som händer, i realtid. Ingen tillbakaspolning.

- **Data**: `agent/kontor.py` läser det kontoret redan sparar (nya händelser från sinnena, triage- och dispatcherbeslut, post, meddelanden, bokningar, plock från board, sparningar i minnet, pågående körningar). `/api/kontor/lage` ger läget, `/api/kontor/strom` en ström av händelser (server-sent events, genom nginx utan buffring), `/api/kontor/objekt/<namn>` innehållet bakom ett klick.
- **I vila** sitter alla vid sina bord och rör händerna lite. **Arbetar** de (en körning pågår) skriver Assistant och Researcher på tangentbordet och Director och Manager arbetar med papper.
- **Inkorgarna** visar allt som väntar på rollen: Director nya fynd som inte gått igenom triage (skrivaren skriver ut dem dit), Manager fynd som triage släppt fram men dispatchern inte beslutat om, Assistant frågor till ägaren, plus post till var och en. Board visar sparade fynd, kalendern det planerade på rätt dag, papperskorgen det som slängts i dag.
- **Ärenden**: varje roll har en kö. Triage som släpper fram ett fynd: Director bär det till Managers inkorg; slänger: Director kastar från stolen. Dispatchern: Manager bär fyndet till kalendern, board, Assistants inkorg eller papperskorgen. Post: avsändaren bär den till mottagarens inkorg. Leverans från kalendern: Assistant hämtar lappen. "Något nytt?": Assistant hämtar från board. Bokning: lappen sätts upp i kalendern. Sparning i minnet: en tur till den egna lådan i arkivet. Director som går ut på nätet: lånar Researchers dator tills körningen är klar. Två likadana ärenden i rad blir en tur; en lång kö gör att figuren går fortare.
- **Gångvägar**: punkter mellan möblerna och kortaste vägen mellan dem.
- **Klick**: en vit kontur som följer formen när man hovrar över ett skrivbord, arkivet, papperskorgen, kalendern eller board; ett klick öppnar ett fönster med innehåll och historik (arbetsminne, senaste arbetet och post för ett skrivbord; filerna i varje låda; det slängda; det planerade och dina pass; det sparade).
- **Kvar**: hur en pausad kvot syns (väntar till felhanteringen).

## Vem pratar med vem (mätt, vecka 7–14 okt)

Layouten följer flödet: det som skickas oftest ska ha kortast väg.

| Flöde | Per vecka | Mellan |
|---|---|---|
| Nytt från sinnena in till triage | ≈ 390 | inkommande → Director |
| Triage slänger | ≈ 310 (80 %) | Director → papperskorgen |
| Triage släpper vidare | ≈ 80 | Director → Manager |
| Dispatchern sparar till senare | ≈ 65 | Manager → board |
| Dispatchern planerar eller skickar | ≈ 65 | Manager → kalendern / Assistant |
| Ägaren frågar efter sparat | ≈ 25 | board → Assistant |
| Samtalet | ≈ 110 svar | ägaren ↔ Assistant |
| Sökuppdragens fynd | ≈ 25 | Researcher → inkommande |
| Post mellan rollerna (order, frågor, rapporter) | några få, mest nattetid | alla |

Det ger en kedja: **inkommande → Director → Manager → schema och hylla → Assistant → ägaren.** Researcher står vid början av kedjan (den matar in och sköter sinnena) och nära Director (order och rapporter). Arkivet används av alla, mest av Assistant och Director.

## Rummet

| Föremål | Visar i systemet | Förslag på mått (cm), höjd | Att tänka på |
|---|---|---|---|
| Golv | — | 900 × 620 | Mönster går bra (plankor, matta). Ljuset ritar på det. |
| Väggar | — | 25 tjocka, 270 höga | Bara ovansidan syns: en mörk ram. |
| Telexen (inkommande) | sinnena: flöden, sidor, API:er och websockets som bevakas mekaniskt, utan att någon söker | maskin med pappersremsa, 50 × 40, höjd 30 | Skriver ut av sig själv när något kommer in, rakt ned i Directors inkorg; står därför intill Directors bord. Researcher ställer in den, men det syns bara som att Researcher arbetar vid sin dator. |
| Arkivet | minnet (`memory/`) | 160 × 55, höjd 95 | Lådor som dras ut (syns ovanifrån när de är ute). En låda per roll: varje roll äger sina filer. |
| Kalendern | Managers schema: fynd med en planerad tid och bokningar (#B), med ägarens egna lektioner och möten som fasta block | lutande tavla, 40 × 210, 95–190 hög | Assistant bokar det ägaren ber om (fast, bara ägaren flyttar det) och sina löften; Manager planerar fynd i luckorna och får flytta löften inom deras senaste tid. |
| Board | sparade fynd: värda att ha men inte värda att störa för, som post-its | lutande tavla, 150 × 55, 95–185 hög | Managers yta. Manager sätter upp en lapp; Assistant går dit och tar den när ägaren frågar "något nytt?"; lappen följer med och är sedan borta. Inaktuella lappar tas bort. |
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
- **Manager**: närmast kalendern och board.

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
| Bokningslapp | en bokning (#B) i kalendern |

## Från systemet till kontoret

Animationerna hålls enkla. Det som berättar något är **när rollerna sätter igång varandra**: då läggs ett papper i mottagarens inkorg. Allt annat är att sitta och arbeta.

**1. Att arbeta** (en roll har en körning igång): figuren sitter vid sitt bord. Assistant och Researcher vid datorn, Director och Manager med papper. Det täcker allt som inte är en överlämning: att läsa källor, webben och mejlen, att söka, att ställa in telexen, att läsa i arkivet. Inget av det animeras för sig.

**2. Överlämningar** (det som syns): ett papper till någon annans inkorg, eller till ett av de gemensamma ställena.

| Från → till | Vad | Per vecka |
|---|---|---|
| Telexen → Directors inkorg | nytt från sinnena och sökuppdragen (av sig själv, ingen går) | ≈ 390 |
| Director → papperskorgen | triage: inte värt något (skrynklas och kastas från stolen) | ≈ 310 |
| Director → Managers inkorg | triage: värt något | ≈ 80 |
| Manager → kalendern | ett fynd planeras till en tid | ≈ 50 |
| Manager → board | ett fynd sparas till senare (en post-it) | ≈ 65 |
| Manager → Assistants inkorg | ett fynd ska ut nu | ≈ 15 |
| Manager → papperskorgen | ett fynd släpps | ≈ 20 |
| Kalendern → Assistant | det är dags: Assistant plockar lappen och skriver till dig | |
| Board → Assistant | du frågar "något nytt?" | ≈ 25 |
| Assistant → kalendern | en påminnelse eller ett löfte bokas | |
| Vilken roll som helst → någon annans inkorg | post: anteckning, fråga, order, återkoppling, rapport, svar | få |

När mottagaren börjar sin körning tar den upp sina papper ur inkorgen; högen visar alltid hur mycket som väntar.

**3. Arkivet**: varje roll sparar sina ändringar i minnet en gång, när körningen är klar (samtalet ändrar mitt i rundan, nattens körningar efteråt, men allt sparas i ett svep på slutet, med vilka filer som ändrades). Det blir en egen handling i rollens kö: när figuren är ledig går den till arkivet, drar ut sin egen låda och lägger in eller tar ut. En tur per körning, hur många filer det än gällde. Läsningarna i arkivet syns inte; de sker hela tiden.

**Kön och takten**
- Varje figur har en kö. Ett papper som ska lämnas lämnas när figuren är klar med det den gör, i tur och ordning.
- **En tur per mottagare och körning**: tre fynd till Manager från samma triage blir en bunt, inte tre promenader. Sex papper i papperskorgen blir ett kast med en hög.
- Animationen ligger några sekunder efter verkligheten och väntar aldrig ut den. Ett svar till dig går iväg direkt; figuren hinner ikapp.
- Blir kön för lång går figuren fortare, eller slår ihop det som ska till samma ställe.

**Handlingarna figurerna behöver**: sitta och arbeta (dator eller papper), resa sig, gå, lägga i en inkorg, ta upp ur sin inkorg, skrynkla och kasta, sätta upp och ta ned lappar på kalendern och board, dra ut och skjuta in sin låda.

## Hela kontorets lägen

- **Dygnet**: dagsljuset från sidan vandrar från höger till vänster (norr uppåt) och skiftar i färg; på natten är rummet mörkt och skrivbordslamporna lyser (klart i gråskissen).
- **Rummet visar läget även när ingen rör sig**: högen i varje inkorg är antalet olästa poster, remsan i telexen det som väntar på triage, lapparna i kalendern de kommande bokningarna och fynden, post-its på board det sparade.

## Beslut som är dina

1. **Manager och papperskorgen**: en egen liten papperskorg, eller går Manager till Directors (≈ 20 i veckan)?
2. **Natten**: sitter rollerna kvar och arbetar i lampljuset, går de hem mellan sina pass, eller sover de vid bordet?
3. **Kvoten pausad**: figurerna väntar vid släckta lampor, en skylt, eller något annat?
4. **Byggläget**: en hantverkare som dyker upp när en ändring byggs, och går när du har tryckt "Driftsätt"?
5. **Ägaren själv**: syns du i kontoret, eller är Assistentens skärm fönstret ut mot dig?

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
3. Arkivet, kalendern, board och telexen.
4. Papper och lapp.
5. En figur (delarna), som först får vara alla fyra.

Resten (rollernas egna föremål, figurernas identiteter, papperskorgen) kommer i nästa omgång.
