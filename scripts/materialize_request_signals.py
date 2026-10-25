"""One-shot, read-only-to-Incident monitor for a closed 10-minute request window."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from aegisops.application.request_derived_signals import (
    get_request_signal_window,
    materialize_closed_window,
)
from aegisops.config import Settings, require_dev_runtime

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-start", required=True, help="UTC start at a 10-minute boundary")
    args = parser.parse_args()
    start = datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
    settings = Settings()
    require_dev_runtime(settings)
    signals = materialize_closed_window(settings, start)
    window = get_request_signal_window(settings, start)
    assert window is not None
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "window_start": start.astimezone(UTC).isoformat(),
        "detector_version": "request-failure-window-v1",
        "signals": [signal.model_dump(mode="json") for signal in signals],
        "window": window.model_dump(mode="json"),
        "interpretation": (
            "Exact verified request members; root cause and aggregate alert binding "
            "remain undetermined."
        ),
    }
    path = ROOT / "eval" / f"request-signals-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "signals": [(item.service, item.failure_code, item.event_count) for item in signals],
        "late_event_count": window.late_event_count,
        "report": str(path),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
