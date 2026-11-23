# Architecture

```
            ┌────────────── bot process ──────────────┐
 store ───▶ │ db_backend → db_reader → runtime        │
 (SQLite)   │                 │                       │
            │        worker / private_worker          │ ──▶ sink
            │                 │                       │
            └──────── llm_client (HTTP) ──────────────┘
                              │
                     gateway/server.py ──▶ OpenAI-compatible API
```

Two processes: the **bot** (`bot/bot.py`, package `bot/chatwarden/`) and the
**gateway** (`gateway/server.py`). They talk over local HTTP with a shared
bearer key in `/etc/chatwarden/secrets/gateway.key`.

## Bot modules

| Module | Role |
| --- | --- |
| `runtime.py` | Main loop: watches the store, advances per-chat checkpoints, feeds workers. |
| `wal_watcher.py` | inotify/stat watcher that wakes the loop when store files change. It never copies or transforms data. |
| `db_backend.py` | Where stores come from (see below). |
| `db_reader.py`, `msgfmt.py` | Typed reads over the store: messages, contacts, groups, sessions. |
| `worker.py`, `private_worker.py` | Group and private-chat workers: message intake, the reply path, lifecycle. |
| `worker_proactive.py`, `worker_quote.py`, `worker_followup.py`, `worker_media.py`, `worker_commands.py`, `worker_jobs.py`, `worker_reply.py` | Feature mixins of `GroupWorker`: proactive turns, quoted-message context, follow-up windows, media intake, welcome and personality commands, background jobs, guarded sends. `worker_text.py` holds the shared patterns and value types. |
| `reply_dispatch.py` | Bounded reply queues and late-send guards. |
| `llm_client.py` | The only client of the gateway. |
| `sinks.py` | Where replies go (see below). |
| `image_pipeline.py`, `image_codec.py`, `emoticon_pipeline.py`, `voice_pipeline.py` | Media. |
| `core.py` | Shared runtime helpers and state: logging, the bot's database, context windows, model calls, sending. Re-exports the modules in the next row, so `core.NAME` works for all of them. |
| `config.py`, `prompts.py`, `groups_config.py`, `history_summary.py`, `chat_memory.py`, `human_style.py`, `reach.py`, `imagegen.py` | Split out of `core.py` by topic: settings and patterns, prompt text, `groups.json` handling, history summaries, long-term memory, style guidance, research hand-off, image generation. They reach whatever still lives in `core` as `core.NAME`. |
| `owner_settings.py`, `owner_jobs.py` | Owner-only settings and jobs; inactive until `CW_OWNER_WXID` is set. |
| `memory_docs.py`, `state.py` | Long-term memory and the bot's own state database. |

## Interface 1: message source

`db_backend.py` defines the store backend. The bundled `PlainBackend` reads a
directory of ordinary SQLite files (`CW_PLAIN_DB_DIR`). A custom backend is
selected with `CW_DB_BACKEND=package.module:ClassName` and must provide the
same methods: `reload`, `maybe_refresh`, `rel_keys`, `message_shards`,
`media_shards`, `find_rel_key`, `path`, `path_with_status`, `has_wal_tail`,
`direct_query`, `discard`, plus the `account_id` property.

WXChatwarden only ever opens files that are already readable. Producing such
files from any chat client is out of scope: this repository does not do it,
does not document it and does not link to anything that does.

### Store layout

Paths are relative to `CW_PLAIN_DB_DIR`.

| File | Tables used |
| --- | --- |
| `message/message_N.db` | `Name2Id(user_name)`; one `Msg_<md5(chat id)>` table per chat |
| `contact/contact.db` | `contact`, `chat_room`, `chatroom_member` |
| `session/session.db` | `SessionTable` |
| `message/message_resource.db` (optional) | `ChatName2Id`, `MessageResourceInfo` (image lookup) |
| `message/media_N.db` (optional) | `VoiceInfo` (voice blobs) |

Message table columns: `local_id` (monotonic primary key, used as the
checkpoint), `server_id`, `local_type`, `sort_seq`, `real_sender_id` (rowid in
`Name2Id`), `create_time`, `status`, `source`, `message_content`,
`compress_content`, `packed_info_data`. In a group, an incoming text message
stores `"<sender id>:\n<text>"` in `message_content`.

`tools/replay_jsonl.py` contains the exact `CREATE TABLE` statements and is
the reference implementation of a source: it turns a JSONL transcript into a
store, and appending lines simulates incoming messages.

## Interface 2: message sink

`sinks.py`. A sink is any object with:

```python
send_text(chat, text, private=False)
send_image(chat, path, private=False)
send_file(chat, path, private=False)
```

`CW_SINK` selects it: `console` (default), `jsonl`, `agent_wechat`, or
`package.module:ClassName`. `agent_wechat` is the optional UI-automation path
(`bot/atspi_send*.py`) written for the container of the open-source
[thisnick/agent-wechat](https://github.com/thisnick/agent-wechat) project; it
needs that environment and is never used unless selected.

## Interface 3: model gateway

`llm_client.py` posts to `CW_LLM_ENDPOINT` (`/v1/execute`):

```json
{"task_kind": "formal", "request_id": "...", "session_key": "...",
 "prompt": "...", "explicit": true,
 "media": {"mime_type": "image/jpeg", "data_base64": "..."}}
```

and expects `{"ok": true, "text": "...", "structured": {...}, "session_turn": 1, ...}`.
Text kinds: `formal`, `private`, `search`, `research`, `vision`. Structured
kinds (JSON output): `classifier`, `owner_intent`, `proactive`,
`history_metadata`, `personality`, `personality_random`, `welcome`,
`history_summary`, `settings_intent`, `voice_transcript`.

`gateway/server.py` implements this against any OpenAI-compatible
chat-completions API, keeps short rotating sessions, and retries a malformed
JSON answer once. Replace it with your own service if you need tools, search
or routing; the bot only depends on the HTTP contract.

## Media

`image_codec.py` exposes `decode(raw) -> bytes`. The bundled codec accepts
files that are already standard images and raises `ImageNotReadable`
otherwise, in which case vision features for that message are skipped. Voice
is read as SILK from `VoiceInfo` and converted to WAV with ffmpeg.

## Configuration

Environment variables (see `.env.example`) plus `groups.json` in the data
directory, which is hot-reloaded and can be edited with `bot/groupsctl.py`.
