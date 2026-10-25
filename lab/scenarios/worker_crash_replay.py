"""Crash after DB commit but before broker ack, then prove redelivery is idempotent."""

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from lab.scenarios.delivery_reliability import await_processed, facts, send_alert, verify_counts

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "lab" / "scenarios" / "worker_crash.compose.yaml"
RABBIT = "http://127.0.0.1:15672/api/queues/%2F/alert.received.v1"


def compose(*args: str, crash_message_id: str | None = None) -> None:
    files = ["-f", "compose.yaml"]
    env = os.environ.copy()
    if crash_message_id:
        files += ["-f", str(OVERLAY)]
        env["AEGISOPS_CRASH_AFTER_COMMIT_MESSAGE_ID"] = crash_message_id
    subprocess.run(["docker", "compose", *files, *args], cwd=ROOT, env=env, check=True)


def await_crash() -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}:{{.State.ExitCode}}",
                "aegisops-worker-1",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if state == "exited:77":
            return
        time.sleep(0.5)
    raise RuntimeError("worker did not crash at the commit-before-ack boundary")


def queue_depth() -> tuple[int, int]:
    response = httpx.get(RABBIT, auth=("aegisops", "aegisops_dev_only"), timeout=5)
    response.raise_for_status()
    data = response.json()
    return data.get("messages_ready", 0), data.get("messages_unacknowledged", 0)


def await_queue_empty() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if queue_depth() == (0, 0):
            return
        time.sleep(0.5)
    raise RuntimeError(f"redelivered broker message was not acknowledged: {queue_depth()}")


def main() -> None:
    fingerprint = uuid4().hex
    compose("stop", "worker")
    try:
        send_alert(
            fingerprint,
            f"crash-probe-{uuid4().hex}",
            "CrashBoundaryProbe",
            datetime.now(UTC).isoformat(),
        )
        pending = facts([fingerprint])
        if len(pending) != 1 or pending[0][3] is None or pending[0][4] is not None:
            raise RuntimeError("crash probe did not leave a pending Outbox message")
        message_id = str(pending[0][3])
        compose(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "worker",
            crash_message_id=message_id,
        )
        await_crash()
        delivered = await_processed([fingerprint])[0]
        verify_counts(str(delivered[1]), message_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and queue_depth()[0] < 1:
            time.sleep(0.5)
        if queue_depth()[0] < 1:
            raise RuntimeError("broker did not requeue the unacknowledged message after crash")
    finally:
        compose("up", "-d", "--no-deps", "--force-recreate", "worker")
    await_queue_empty()
    verify_counts(str(delivered[1]), message_id)
    print("worker crash after DB commit, broker redelivery, and one database effect verified")


if __name__ == "__main__":
    main()
