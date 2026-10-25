"""Audited, optimistic requeue for an explicitly selected blocked Lab detector run."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from aegisops.application.request_signal_scheduler import requeue_blocked_run
from aegisops.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    operation = requeue_blocked_run(Settings(), args.run_id,
                                   expected_generation=args.generation,
                                   operator_id=args.operator, reason=args.reason)
    path = Path(__file__).resolve().parents[1] / "eval" / (
        f"detector-requeue-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    )
    report = {"run_id": str(args.run_id), "operation": operation}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
