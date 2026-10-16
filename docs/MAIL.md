# Mejlen: skrivskyddat fönster

Status: Gmail byggt · KTH-mejlen öppen fråga · hör till [MODEL.md](MODEL.md)

## Vad det är till för
Mejlen ger Audax sammanhang om vad som händer i ditt liv: svar på ansökningar, bokningar, deadlines, människor och åtaganden. Audax kan inte skicka, radera eller ändra något.

- **Director** får varje natt en lista över de senaste veckornas viktiga mejl: avsändare, ämne, datum och en rad text. Reklam, sociala notiser och e-postlistor är bortfiltrerade. Den ser också vad du själv har skickat, eftersom det visar vad du faktiskt gör. Den kan söka och öppna enskilda mejl. Till sina filer skriver den bara slutsatser ("Spotify svarade 14/10, intervju 22/10"), aldrig kopior av mejl.
- **Chatten** söker och läser när du frågar ("har Spotify svarat?").
- **Manager** har ingen åtkomst. Den kalibrerar upplevelsen och behöver inte innehållet i ditt liv.

Motorn sparar inget från mejlen. Mejl är skrivna av andra, så de lämnas alltid till Claude som information och aldrig som instruktioner.

**Isolering:** en körning som har läst mejl kan inte nå nya adresser på webben. Så kan ett mejl från en främling inte lura Audax att föra ut privat data i en webbadress. Därför läser Directors nattliga genomgång mejlen utan webbläsning, och reflektionen, som har webben, ser aldrig mejlen. Lägga till bevakningar gör Researcher, som aldrig läser mejl: en källa Director ser i mejlen blir en beställning till Researcher, som kontrollerar adressen. I chatten stängs webbläsningen resten av rundan efter att Bruno läst ett mejl.

## Varför en Google-nyckel och inte ett lösenord
Nyckeln har bara behörigheten `gmail.readonly`, så den kan i sig inte skicka eller radera något. Motorn vägrar använda en nyckel med bredare behörighet. Ett app-lösenord hade gett full åtkomst.

## Så sätter du upp det (en gång, cirka 15 minuter)
1. Gå till [console.cloud.google.com](https://console.cloud.google.com) och skapa ett nytt projekt, till exempel "Audax".
2. **APIs & Services → Library**: sök efter **Gmail API** och klicka **Enable**.
3. **Google Auth Platform** (OAuth consent screen):
   - **Branding**: appnamn "Audax" och din e-post som kontakt.
   - **Audience**: välj **External** och lägg till dig själv som testanvändare. Klicka sedan **Publish app** så att den står som *In production*. Annars slutar nyckeln fungera efter 7 dagar. Appen granskas inte av Google, och det behövs inte för eget bruk.
4. **Clients → Create client**: välj typen **Desktop app**. Ladda ner JSON-filen.
5. Kör på Macen, i projektmappen:
   ```
   uv run python -m agent.mail_auth ~/Downloads/client_secret_XXXX.json
   ```
   Logga in med ditt Gmail-konto. Google varnar att appen inte är verifierad: klicka **Advanced → Go to Audax** och godkänn läsbehörigheten.
6. Skriptet skriver ut tre rader (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`). Lägg in dem i `.env` på Pi:n och starta om motorn. Klistra aldrig in dem i Telegram.
7. Testa genom att fråga Bruno något som står i din mejl.

Du kan dra tillbaka åtkomsten när som helst på [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## KTH-mejlen
KTH kör Microsoft 365, och där finns två vägar:
- **Vidarebefordra till Gmail.** KTH tillåter det, men då sparas inga kopior i KTH-lådan, och KTH avråder eftersom mejl kan försvinna på vägen.
- **Läsbehörighet via Microsoft (Mail.Read).** Det kräver att KTH låter studenter godkänna egna appar, vilket många universitet stänger av. Det behöver provas.

Koden är byggd så att en andra brevlåda kan läggas till bredvid Gmail.
