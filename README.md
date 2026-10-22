# Audax

*Latin: bold, daring.*

People easily go blind from their own uncertainty, or live in a bubble with a narrow picture of their paths. Audax is a personal agent that helps you reach outside both. It doesn't automate your life; it tries to **understand** it: what you work on, what you're after, what you keep running into. It finds what fits your interests, goals and projects, and tries things you didn't ask for. It suggests. It never acts on your behalf and never writes messages for you to send.

It runs on a Raspberry Pi, talks to you in a single Telegram chat, and uses Claude through Claude Code on your own Claude subscription. It refuses to start if an API key is set.

## How it works: a small office

Four roles, each owning one question. They talk through an internal post, and only one of them talks to you.

| Role | Its question | Slow skills | Fast skill |
|---|---|---|---|
| **Assistant** | What do you say, and how do I answer? | night pass: tidies what it knows about you, folds the conversation into a summary | the conversation, every message that reaches you |
| **Director** | What matters to you, and why, and what might matter that you can't see yet? | daily review; reflection (a routine it sets itself) | triage: is this finding worth anything to you? |
| **Researcher** | How do we find what exists, and what do we miss? | planning: missions, watched sources, maps of each search space | search missions |
| **Manager** | How should it feel: when, how much, in what tone? | nightly UX review of the conversation, the schedule | dispatcher: when should this reach you? |

Director and Manager are explorers. Director plans in **steps** with owners and dates, runs **experiments** (to improve what is there, or to explore outside your current goals), holds **beliefs** about you with their evidence, and **asks** when an answer would help more than guessing. Manager does the same for the experience. The roles judge by principles, not hard-coded rules: your attention is scarce, your own words weigh most, people change their minds, and asking is always fine.

### Three layers

1. **Code:** the forms and their checks, the engines (a calendar that wakes whoever owns what is due, a budget, rules that drop duplicates before Claude sees them, a last check before Telegram) and the tools. The roles can't change it.
2. **Items** in SQLite, created and changed by the roles through the office's tools and checked against their form: aims (#S), steps (#T), experiments (#E), beliefs (#U), questions (#Q), rules (#G) and routines (#N), plus findings, posts, bookings and runs. Watched sources and search missions are routines with a purpose and a health. Nothing runs forever: everything has a date it is looked at again.
3. **Texts** in a memory folder: the story of you, the situation, the policy for tone and pacing, each role's working memory, the search maps, shared lessons. They refer to items by id, change a section at a time, and each has an owner and a size. Your profile is not a text anyone writes: it is a view generated from the beliefs, with their sources.

### From a finding to you

```
source → rules (code) → triage (Director) → dispatcher (Manager) → schedule → Assistant writes it
```

- **A wide, cheap net:** structured sources (job boards, applicant tracking systems, Devpost, feeds, watched pages, news topics), a company watchlist, and search missions whose query portfolios learn what pays off. A source that only fails pauses itself.
- **Silence is the default.** Findings are sorted in batches; good ones that aren't worth interrupting for go to a shelf with a best-before date. Every send is a logged decision you can ask about.
- **One honest voice.** Only the message itself reaches you. Deliveries are written with a short brief and say why they are for you. The chat reads what the office is actually doing before it talks about it, and a promise is always a booking or a step that wakes it.
- **Build mode:** ask in the chat for a change to how Audax works. Claude Code builds it on a branch in a sandboxed worktree, the tests run, and you approve with a button.

The office page, served on the Pi to your tailnet only, shows it all as an illustrated office seen from above, with the raw data under `/data/`. It can only read.

Design docs, in Swedish: [MODEL](docs/MODEL.md) (how the office works) · [OPERATIONS](docs/OPERATIONS.md) (what runs, what it costs, what happens when something fails) · [SENSES](docs/SENSES.md) · [MAIL](docs/MAIL.md) · [KONTORET](docs/KONTORET.md) (the office page) · [BACKUP](docs/BACKUP.md) · older notes in [docs/archive](docs/archive/)

## Engine and instance

This repository is the **engine**. It contains nothing about any particular person. Everything personal lives in the **instance**, which is never part of this repo:

| | Where | |
|---|---|---|
| Your settings (incl. your name) | `instance/` (own git repo, keep it private) | created from `config/*.example.yaml` on first start; the example sources and missions become routines in the database |
| What Audax knows about you: beliefs, goals, plans | `data/` (SQLite) | |
| The texts: the story of you, the situation, policy, working memories, search maps | `workspace/memory/` (own git repo) | created from `templates/memory/` |
| Secrets | `.env` | from `.env.example` |

## Running it

Requirements: a Raspberry Pi with 64-bit OS (or any Linux/macOS machine), a Claude Pro or Max subscription, and a Telegram bot from @BotFather.

```bash
git clone <this repo> ~/Projekt/audax && cd ~/Projekt/audax
./deploy/setup-pi.sh                # uv, Claude Code, dependencies, instance, systemd service
claude                              # log in once with your Claude subscription, then /exit
nano .env                           # TELEGRAM_TOKEN (+ calendars, GITHUB_USER/TOKEN, Gmail as needed)
nano instance/config/settings.yaml  # owner: your name
uv run python -m agent.smoketest
sudo systemctl start agent
```

Message `/start` to your bot, put the chat id it replies with into `.env`, and restart. Then send `/start` again: Audax starts by getting to know you with a short interview.

Audax takes a loose share of your subscription's weekly quota (10 % to begin with); the conversation and what you ask for never wait, the roles' heavier work does when the share is used.

From a development machine: put `PI_HOST=user@host` in `.deploy.env`, then `./deploy/deploy.sh` (tests → push → restart) and `./deploy/backup.sh` (memory, instance and database to `~/Backups/audax`).

## Tests

```bash
uv run pytest
```

## License

MIT, see [LICENSE](LICENSE).
