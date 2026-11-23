#!/bin/sh
# End-to-end check on one machine, no network: replay source -> bot ->
# gateway -> stand-in model -> JSONL sink. Needs write access to
# /etc/chatwarden/secrets and /var/lib/chatwarden (run in the container).
set -e
cd "$(dirname "$0")/.."
mkdir -p /etc/chatwarden/secrets /var/lib/chatwarden
STORE=$(mktemp -d); OUT=$STORE/outbox.jsonl
python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" > /etc/chatwarden/secrets/gateway.key
cp examples/groups.example.json /var/lib/chatwarden/groups.json
python3 tools/replay_jsonl.py init "$STORE/db" --self wxid_demo_bot --self-name 小卫
python3 tools/replay_jsonl.py say "$STORE/db" --chat 20000000001@chatroom --chat-name 演示群 \
  --sender wxid_demo_alice --sender-name Alice "今天天气不错"
python3 tools/fake_llm.py > "$STORE/fake.log" 2>&1 &
PIDS=$!
CW_LLM_BASE_URL=http://127.0.0.1:8650/v1 CW_LLM_API_KEY=demo CW_LLM_MODEL=fake \
  python3 gateway/server.py > "$STORE/gateway.log" 2>&1 &
PIDS="$PIDS $!"
sleep 1
CW_PLAIN_DB_DIR="$STORE/db" CW_ACCOUNT_ID=wxid_demo_bot CW_BOT_WXID=wxid_demo_bot \
CW_OWNER_WXID= CW_SINK=jsonl CW_SINK_JSONL="$OUT" CW_DECISION_SHADOW=0 \
CW_MEMORY_DOCS_SCHEDULER=0 python3 bot/bot.py > "$STORE/bot.log" 2>&1 &
PIDS="$PIDS $!"
trap 'kill $PIDS 2>/dev/null' EXIT
sleep 8
python3 tools/replay_jsonl.py say "$STORE/db" --chat 20000000001@chatroom \
  --sender wxid_demo_bob --sender-name Bob "@小卫 你好，介绍一下你自己"
i=0
while [ $i -lt 30 ]; do
  if [ -s "$OUT" ]; then echo "E2E PASS"; cat "$OUT"; exit 0; fi
  i=$((i+1)); sleep 1
done
echo "E2E FAIL"; tail -20 "$STORE/bot.log"; exit 1
