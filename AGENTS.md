# AGENTS.md

Notes for coding agents (Codex, Claude Code, Gemini CLI, Cursor, and others)
and for humans working on chatwarden. `CLAUDE.md` only points here.

Human-facing docs: `README.md` / `README.en.md` (overview, demo),
`docs/ARCHITECTURE.md` (interfaces and module map), `CONTRIBUTING.md` (DCO,
hard boundary), `SECURITY.md` (reporting vulnerabilities). This file adds what
an agent needs on top of those.

## 1. Hard rules

A PR that breaks one of these is closed. If a task seems to need breaking one,
stop and ask in the issue or PR instead.

1. **No decryption code, ever.** chatwarden only reads message stores that are
   already plain, readable SQLite. Do not add code, docs, comments, tests,
   issues or links that decrypt any chat client's database or media, extract
   keys, scan process memory, or speak a reverse-engineered private protocol,
   and do not name tools that do. New data paths go through the existing
   interfaces (message source, message sink, image codec, model gateway; see
   `docs/ARCHITECTURE.md`). The optional `*_local.py` hooks
   (`db_backend_local`, `image_codec_local`, `emoticon_local`, `deploy_local`)
   are loaded only if a deployment provides them; never commit them or write
   examples of them, and keep the import hooks.
2. **No real chat data.** Test fixtures, examples and docs use invented text
   only. Never paste names, nicknames, group titles, ids (`wxid_*`,
   `*@chatroom`) or logs from a real chat, including ones you see in issues or
   screenshots. Use obviously fake ids such as `wxid_demo_alice` and
   `20000000001@chatroom`.
3. **No secrets.** `.env`, `data/`, `store/` and logs are git-ignored. The tree
   is gitleaks-clean; keep it that way and don't grow `.gitleaksignore`.
4. **Keep the container contract.** Deployments reach the bot with
   `docker exec wechat-atspi`. Don't rename that container, change its network
   mode, change the default of `CW_SMOKE_CONTAINER` in `bot/run_smoke_suite.py`,
   or change the `wechat-atspi` paths in `config.py` without discussing it in
   an issue first.
5. **Branch, sign, PR.** Work on a branch and open a PR against `main`; never
   push to `main`. Every commit carries a DCO sign-off (`git commit -s`).
   Maintainers merge, and only maintainers create tags or releases.

Not wanted, so don't propose them: merging the same-named methods of the group
and private workers (`worker.py` / `private_worker.py`) into a shared base, and
splitting up the large `GroupWorker` methods (`__init__`, `_handle_text`,
`_answer`).

## 2. Getting started

```sh
git clone https://github.com/Nightchamp/chatwarden.git
cd chatwarden
git checkout -b <your-branch>

pip install silk-python zstandard          # imported by voice/db code
sudo apt-get install -y ffmpeg             # voice conversion (optional for tests)
# Optional, for one smoke test: python3-gi gir1.2-atspi-2.0

python3 -m unittest tests.test_gateway     # expect: Ran 13 tests ... OK
sh tests/run_smokes.sh                     # expect: pass=68 fail=0 skip=1
sh tests/e2e_demo.sh                       # needs writable /etc/chatwarden and
                                           # /var/lib/chatwarden (container/CI)
```

Python 3.12 is what the Docker image uses; 3.13 and 3.14 also work. Everything runs
offline: no chat client, no model, no network. Run a subset with
`sh tests/run_smokes.sh <regex>`. CI (`.github/workflows/ci.yml`) runs all
three on every push and PR; keep it green.

Environment notes:

- The skipped smoke is `v3_v3201_atspi_send_common_smoke.py`, which needs
  PyGObject; CI installs it and runs it.
- A few smokes write to the default `/var/lib/chatwarden`, so run the suite as
  a user who can create it (CI does). Moving them to temp dirs is welcome.
- On Windows, also `pip install tzdata`; the two smokes that assert POSIX file
  modes (`v3_v3109_*`, `v3_v3170_*`) fail there by design.

## 3. Architecture in brief

Two processes talk over local HTTP with a bearer key in
`/etc/chatwarden/secrets/gateway.key`:

- **bot** (`bot/bot.py`, package `bot/chatwarden/`) watches a plain SQLite
  message store, decides whether and what to reply, and sends through a sink.
- **gateway** (`gateway/server.py`, stdlib only) turns the bot's `/v1/execute`
  requests into calls to any OpenAI-compatible API.

Flow: `db_backend` → `db_reader`/`msgfmt` → `runtime` (main loop, per-chat
checkpoints, `wal_watcher`) → `worker.GroupWorker` or `private_worker` →
`llm_client` → gateway → model; replies go out through `reply_dispatch` →
`sinks`. The source, sink and gateway HTTP contract in `docs/ARCHITECTURE.md`
are the only supported extension points. `tools/replay_jsonl.py` builds a store
from a JSONL transcript (used by the demo), `tools/fake_llm.py` is a stand-in
model, `bot/groupsctl.py` edits `groups.json`. Configuration is environment
variables (`.env.example`) plus a hot-reloaded `groups.json`.

## 4. How core and the workers are wired

`core.py` re-exports the topic modules (`config`, `prompts`, `groups_config`,
`history_summary`, `chat_memory`, `human_style`, `reach`, `imagegen`, ...),
and `GroupWorker` is assembled from feature mixins (`worker_proactive`,
`worker_quote`, `worker_followup`, `worker_media`, `worker_commands`,
`worker_jobs`, `worker_reply`) plus `worker_text`.

Moved code reaches anything in `core` as `core.NAME` at call time, through a
bottom-of-module `from . import core`. This late binding is deliberate: the
smoke tests monkeypatch `core.X` in hundreds of places, and some build workers
with `GroupWorker.__new__` and set attributes by hand. So don't turn
`core.NAME` calls into direct imports, don't remove re-exports, and keep
attribute names that tests set. If you move code, keep it a pure move and
check the smoke count is unchanged.

## 5. Conventions

- Standard library first. Runtime dependencies are only `silk-python`,
  `zstandard` and the `ffmpeg` binary; justify any new one in the PR.
- Small, local changes; one topic per PR, no incidental moves or reformatting.
  Many historical names (`_vNNN_` methods, `v3194_*` tables, `[v3.30-...]` log
  tags) are referenced by tests and ops tooling; leave them.
- Tests are standalone scripts `bot/v3_v<number>_<topic>_smoke.py` that stub
  every external edge themselves. New behaviour gets a new smoke file; pick
  the next free number.
- Behaviour changes go behind a setting with the current behaviour as the
  default, unless the PR argues otherwise. Say in the PR whether runtime
  behaviour changes and how to check it on a live deployment.
- User-facing strings and prompts are Chinese; code, comments, commit
  messages and docs are English.
- New integration points go behind the existing interfaces, selected by env
  var (`CW_DB_BACKEND`, `CW_SINK`, `CW_LLM_*`), never hard-wired.
- Commit messages: `area: what changed` in the imperative, a short body saying
  why, then the `Signed-off-by:` line.

## 6. Open work

Pick one per PR.

1. **Logs are unbounded and untimestamped.** `core.log()` prints to stdout
   without a timestamp, and `docker-compose.yml` sets no log rotation. Add
   timestamps (keep the `[bot]` prefix and existing tags, which ops tooling
   greps) and a `logging:` block with `max-size`/`max-file` for both services.
2. **No check after a model backend update.** A backend update can silently
   break vision, voice or structured tasks. Add a small `tools/` canary that
   sends one request of each task kind (`formal`, `vision`,
   `voice_transcript`, a structured kind) through the gateway and fails
   loudly, runnable by cron. It must work against `tools/fake_llm.py` in CI.
3. **Smokes that write to `/var/lib/chatwarden`.** Point them at temp
   directories so the suite runs as an unprivileged user.

## 7. Agent-specific notes

- **Codex** reads `AGENTS.md` automatically.
- **Claude Code** reads `CLAUDE.md`, which imports this file.
- **Gemini CLI** reads `GEMINI.md` by default; point it here with
  `"contextFileName": "AGENTS.md"` in a local `.gemini/settings.json` (don't
  commit it).
- **Anything else:** open this file first.
