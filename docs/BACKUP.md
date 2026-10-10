# Säkerhetskopiering och återställning

## Vad finns var

| Vad | Var | Säkerhetskopia |
|---|---|---|
| Koden | Macen + Pi:n (`git`), GitHub när remote finns | git på flera ställen |
| Minnet (`workspace/memory/`, eget git-repo) | bara Pi:n | **Macen**, dagligen kl. 12 med hela historiken (`~/Backups/audax/memory`) |
| Databasen (`data/agent.db`) | bara Pi:n | Pi:n varje natt kl. 03:15 (`data/backups/`, 7 dagar) + **Macen** dagligen (`~/Backups/audax/db`) |
| Instansen (`instance/`, eget git-repo: config om ägaren) | Pi:n | **Macen** dagligen med historik, och vidare till ett privat GitHub-repo om `origin` är satt i backupen |
| `.env` (hemligheter) | Pi:n + Macen | **ingen kopia**. Återskapas från `.env.example` |

Det dagliga jobbet på Macen är `deploy/backup.sh` via launchd (`~/Library/LaunchAgents/audax.backup.plist`, installeras från `deploy/audax-backup.plist`). Pi:ns adress läses från `.deploy.env`. Om Macen sover kl. 12 körs det när den vaknar. Loggen finns i `~/Backups/audax/backup.log`, och senaste körningen står i `last-backup`.

Kör för hand: `./deploy/backup.sh`
Stäng av: `launchctl bootout gui/$(id -u)/audax.backup && rm ~/Library/LaunchAgents/audax.backup.plist`

## Återställa på en ny Pi

```bash
git clone <kod-repot> ~/Projekt/audax && cd ~/Projekt/audax
git clone <mac>:Backups/audax/memory workspace/memory      # eller kopiera mappen
mkdir -p data && scp <mac>:Backups/audax/db/agent-latest.db data/agent.db
git clone <privat-instansrepo eller mac>:Backups/audax/instance instance
cp .env.example .env && nano .env                              # tokens och lösenord igen
./deploy/setup-pi.sh                                           # uv, Claude Code, systemd
```

Claude Code-sessionen för chatten följer inte med. Agenten startar då en ny session med de senaste meddelandena som bakgrund.

## Ångra en enskild ändring i minnet

Varje ändring som agenten gör i minnet är en commit: `git -C workspace/memory log` och `git -C workspace/memory revert <commit>` på Pi:n.
