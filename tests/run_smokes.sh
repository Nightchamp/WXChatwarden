#!/bin/sh
# Run the offline smoke tests. They need no network, no chat client and no
# model: every external edge is stubbed inside each test.
#   sh tests/run_smokes.sh [name-pattern]
cd "$(dirname "$0")/../bot" || exit 2
export CW_SINK=agent_wechat CW_DECISION_SHADOW=0 CW_MEMORY_DOCS_SCHEDULER=0
export CW_IGNORE_LIVE_OWNER_SETTINGS=1 CW_LLM_ENDPOINT=http://127.0.0.1:9/v1/execute
have_gi=1; python3 -c "import gi" 2>/dev/null || have_gi=0
pass=0; fail=0; skip=0
for f in $(ls *smoke.py | grep -E "${1:-.}"); do
  if [ "$f" = v3_v3201_atspi_send_common_smoke.py ] && [ $have_gi = 0 ]; then
    skip=$((skip+1)); echo "SKIP $f (needs PyGObject)"; continue
  fi
  if python3 -B "$f" > /tmp/cw-smoke.log 2>&1; then pass=$((pass+1))
  else fail=$((fail+1)); echo "FAIL $f"; tail -5 /tmp/cw-smoke.log; fi
done
echo "smokes: pass=$pass fail=$fail skip=$skip"
[ $fail = 0 ]
