#!/usr/bin/env bash
# End-to-end demo: real Redis fault -> detector signal -> operator promotion ->
# autonomous investigation -> root-cause gate -> governed proposal -> OPA ->
# independent approval -> fenced execution -> recovery -> resolution.
#
# Prerequisites (see README): deps installed, docker compose up -d, alembic
# upgraded, API on :18000, console on :18010. Consumes model quota when
# AEGISOPS_LLM_* is configured; without a key the agent stops gracefully and
# the incident remains for manual review.
set -euo pipefail
cd "$(dirname "$0")"

export AEGISOPS_API_URL="${AEGISOPS_API_URL:-http://127.0.0.1:18000}"
export AEGISOPS_CONSOLE_URL="${AEGISOPS_CONSOLE_URL:-http://127.0.0.1:18010}"
PY=.venv/Scripts/python.exe
[ -x "$PY" ] || PY=python

step() { printf '\n\033[1;36m== %s\033[0m\n' "$1"; }

step "0/5 Preflight"
"$PY" - << 'EOF'
import httpx
from aegisops.config import Settings
s = Settings()
with httpx.Client(timeout=10, trust_env=False) as c:
    r = c.get("http://127.0.0.1:18000/api/v1/health/ready")
    assert r.status_code == 200, f"API not ready: {r.status_code} (start it per README)"
    key = s.llm_api_key and s.llm_api_key.get_secret_value()
    print("API ready. Model:", s.llm_model if key else "(none - agent will stop gracefully)")
EOF

step "1/5 Waiting for a fresh 10-minute signal window boundary"
"$PY" - << 'EOF'
import time
from datetime import UTC, datetime
now = datetime.now(UTC)
wait = 600 - (now.minute % 10 * 60 + now.second) + 12
print(f"waiting {int(wait)}s ...")
time.sleep(wait)
EOF

step "2/5 Fault window: stop redis, two failing checkouts, restore"
"$PY" - << 'EOF'
import time, httpx
from lab.scenarios.redis_down import compose
from scripts.evaluate_fault_battery import checkout, wait_for_recovery

with httpx.Client(timeout=20, trust_env=False) as c:
    assert checkout(c) == 200, "baseline checkout failed; is the Lab healthy?"
    compose("stop", "redis")
    try:
        for i in range(2):
            status = checkout(c)
            print(f"checkout {i+1}: {status}")
            assert status != 200, "fault did not fail checkout"
        time.sleep(4)
        print("two payment->redis request-failure facts recorded")
    finally:
        compose("start", "redis")
        wait_for_recovery(c)
        print("redis restored (the incident stays OPEN for the agent)")
EOF

step "3/5 Detector window matures; promote the signal (operator action)"
"$PY" - << 'EOF'
import time
from datetime import UTC, datetime, timedelta
import httpx
from aegisops.config import Settings

s = Settings()
now = datetime.now(UTC)
bucket = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 10)
deadline = max(60.0, (bucket + timedelta(minutes=12, seconds=30)
                      - datetime.now(UTC)).total_seconds())
print(f"waiting up to {int(deadline)}s for the window to mature ...")
end = time.monotonic() + deadline
signal = None
with httpx.Client(timeout=10, trust_env=False) as c:
    while time.monotonic() < end and signal is None:
        rows = c.get("http://127.0.0.1:18000/api/v1/request-failure-signals",
                     headers={"Authorization": f"Bearer {s.alert_webhook_token}"}).json()
        for row in rows:
            if (row["service"] == "payment-service"
                    and row.get("resource") == "redis-main"
                    and datetime.fromisoformat(row["window_start"]) >= bucket):
                signal = row
                break
        if signal is None:
            time.sleep(5)
assert signal, "detector signal did not appear in time"
print("signal:", signal["id"], signal["event_count"], "members /",
      signal["distinct_trace_count"], "traces")
r = httpx.post(
    f"http://127.0.0.1:18000/api/v1/request-failure-signals/{signal['id']}/promote",
    headers={"Authorization": f"Bearer {s.operator_token}"},
    json={"reason": "演示：封闭窗口内多次已核验支付缓存请求失败，送入自主调查"},
    timeout=10)
r.raise_for_status()
print("promoted alert:", r.json()["alert_id"])
EOF

step "4/5 Autonomous investigation -> gate -> governed remediation chain"
"$PY" -m scripts.evaluate_agent_remediation --skip-reject-check

step "5/5 Done - open the incident in the console"
echo "  http://127.0.0.1:18010/?view=ops"
echo "  The newest 'VerifiedRequestFailureWindow' incident carries the full chain:"
echo "  agent trace, verified claim, root-cause gate, approval, attempt, timeline."
