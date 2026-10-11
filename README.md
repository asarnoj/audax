# Audax

*Latin: bold, daring.*

Audax is a personal agent that doesn't automate your life. It tries to **understand** it: what you work on, what you're after, what you keep running into. It thinks on its own about what would be good for you and tells you about it, especially the things that self-doubt makes easy to overlook. It suggests. It never acts on your behalf.

It runs on a Raspberry Pi, talks to you in a single Telegram chat, and uses Claude through your own Claude subscription (never a paid API key).

## How it works: one voice, three minds

| Role | Question it answers | Runs |
|---|---|---|
| **Worker** | *What is happening, and what do we say?* Senses, triage, outbox, and the chat, which is the agent's only voice | continuously |
| **Director** | *What matters to you, and what do I see that you don't?* A living picture of your life, goals, ongoing threads, what to look for. Reflects in depth a few times a week | daily + 2–3×/week (deep) |
| **Manager** | *Does it feel natural?* Pacing, timing, tone, and your current bandwidth. Reviews the conversation like a UX researcher | nightly |

- **Senses** in three layers: structured sources that can be enumerated completely (job boards, company applicant tracking systems, Devpost, feeds, watched pages), a company watchlist, and a search *portfolio* that measures which queries pay off. Places that keep producing good finds become permanent senses.
- **Attention** is treated as scarce: the default is silence, items can go to a shelf instead of interrupting, and every send is a logged decision you can ask about.
- **Build mode:** ask in the chat for a change to how the agent works. Claude Code builds it on a branch in a sandboxed worktree, the tests run, and you approve with a button.

Design docs (in Swedish): [ARCHITECTURE](docs/ARCHITECTURE.md) · [OPERATIONS](docs/OPERATIONS.md) · [ATTENTION](docs/ATTENTION.md) · [SENSES](docs/SENSES.md) · [PLAN](docs/PLAN.md) · [BACKUP](docs/BACKUP.md)

## Engine and instance

This repository is the **engine**. It contains nothing about any particular person. Everything personal lives in the **instance**, which is never part of this repo:

| | Where | |
|---|---|---|
| Your config: sources, missions, watchlist, settings (incl. your name) | `instance/` (own git repo, keep it private) | created from `config/*.example.yaml` on first start |
| What the agent knows about you | `workspace/memory/` (own git repo) | created from `templates/memory/` |
| Secrets | `.env` | from `.env.example` |
| Database | `data/` | |

## Running it

Requirements: a Raspberry Pi with 64-bit OS (or any Linux/macOS machine), a Claude Pro/Max subscription, and a Telegram bot from @BotFather.

```bash
git clone <this repo> ~/Projekt/audax && cd ~/Projekt/audax
./deploy/setup-pi.sh                # uv, Claude Code, dependencies, instance, systemd service
claude                              # log in once with your Claude subscription, then /exit
nano .env                           # TELEGRAM_TOKEN (+ calendars, GITHUB_USER/TOKEN as needed)
nano instance/config/settings.yaml  # owner: your name
uv run python -m agent.smoketest
sudo systemctl start agent
```

Message `/start` to your bot, put the chat id it replies with into `.env`, and restart. Then start with `/intervju`.

From a development machine: put `PI_HOST=user@host` in `.deploy.env`, then `./deploy/deploy.sh` (tests → push → restart) and `./deploy/backup.sh` (memory, instance and database to `~/Backups/audax`).

## Tests

```bash
uv run pytest
```

## License

MIT, see [LICENSE](LICENSE).
