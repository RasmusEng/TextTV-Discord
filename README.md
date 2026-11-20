# Text-TV-bot för Discord

Boten postar SVT Text-TV som färgad text i en kanal (ett `ansi`-kodblock där grafiken
ritas med blocktecken som ▀▄▌▐), fäster meddelandet och uppdaterar
samma meddelande en gång varje kvart (:00, :15, :30, :45). Under sidan står det när
nästa uppdatering sker. Knapparna byter sida direkt:

| Knapp | Gör |
|---|---|
| ◀ ▶ | Föregående / nästa sida |
| 🔢 Gå till sida | Skriv in valfritt sidnummer (100–899) |
| 🏠 100 | Tillbaka till startsidan |
| ⏹ Stoppa | Slutar uppdatera panelen (kräver *Hantera meddelanden*). Döljs när panelen är fäst |

Sidor med flera undersidor visar nästa undersida vid varje uppdatering.
Panelerna sparas i `panels.json` och fortsätter uppdateras efter en omstart.

## 1. Skapa boten i Discord

1. Gå till <https://discord.com/developers/applications> och välj **New Application**.
2. Under **Bot**: tryck **Reset Token** och kopiera token. Visa den aldrig för någon.
3. Under **OAuth2 → URL Generator**: kryssa i `bot` och `applications.commands`.
   Under bot-behörigheter kryssar du i **Send Messages**, **Read Message History**
   och **Pin Messages**.
4. Öppna länken som skapas och bjud in boten till din server.

## 2. Installera och starta

Kräver Python 3.10 eller nyare.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (på Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # Mac/Linux: cp .env.example .env
```

Öppna `.env` och klistra in din token efter `DISCORD_TOKEN=`. Starta sedan:

```bash
python bot.py
```

### Med Docker

Varje push till `main` bygger en image till GitHub Container Registry
(`:latest` och `:prod-<sha>`; från `dev` blir det `:dev` och `:dev-<sha>`).

```bash
docker run -d --name texttv --restart unless-stopped --env-file .env -v texttv-data:/data ghcr.io/rasmuseng/texttv-discord:latest
```

Panelerna och teckencachen sparas i volymen `/data`.

## 3. Använd

Skriv `/texttv` i en kanal, eller till exempel `/texttv sida:377` för målservice.
Kommandot kan som standard bara användas av personer som får hantera meddelanden.
Det kan du ändra under *Serverinställningar → Integrationer*. Knapparna kan alla använda.

Det kan ta upp till en timme innan `/texttv` dyker upp första gången, eftersom
Discord sprider ut globala kommandon. Starta om Discord-appen om det dröjer.

## Inställningar (`.env`)

| Variabel | Standard | Betydelse |
|---|---|---|
| `UPDATE_MINUTES` | 15 | Minuter mellan uppdateringarna, på jämna klockslag. Måste gå jämnt upp i 60 |
| `TEXTTV_APP_NAME` | discord-texttv-bot | Namn som skickas till texttv.nu |
| `DATA_DIR` | mappen med `bot.py` | Var `panels.json` och cachen sparas (`/data` i Docker) |

## Bra att veta

- Färgerna i `ansi`-kodblock visas i Discord på datorn och webben. I mobilappen kan sidan
  visas utan färger.

- Innehållet hämtas från texttv.nu:s inofficiella API och tillhör SVT. Använd det privat
  eller för din egen server, inte som en stor publik tjänst.
- Boten måste vara igång hela tiden för att panelerna ska uppdateras. Kör den på en dator
  som alltid är på, en Raspberry Pi eller en billig VPS.
