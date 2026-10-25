"""Poll closed windows and persist bounded detector run state."""

import argparse
import logging
import time

from aegisops.application.request_signal_scheduler import run_one_due_window
from aegisops.config import Settings, require_dev_runtime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aegisops.request_signal_detector")
POLL_SECONDS = 10
BATCH_SIZE = 6
BACKLOG_POLL_SECONDS = 1


def poll_batch(settings: Settings) -> int:
    """Drain bounded backlog without waiting between each completed window."""
    processed = 0
    for _ in range(BATCH_SIZE):
        tick = run_one_due_window(settings)
        if tick is None:
            break
        processed += 1
        logger.info(
            "detector window=%s attempt=%s status=%s error=%s",
            tick.window_start, tick.attempt_count, tick.status, tick.error_code,
        )
    # A separate budget prevents original-window backlog from starving late work.
    for _ in range(BATCH_SIZE):
        tick = run_one_due_window(settings, compensation=True)
        if tick is None:
            break
        processed += 1
        logger.info("late window=%s attempt=%s status=%s error=%s",
                    tick.window_start, tick.attempt_count, tick.status, tick.error_code)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    while True:
        processed = 0
        try:
            processed = poll_batch(settings)
        except Exception as error:
            logger.error("detector poll failed code=%s", type(error).__name__)
        if args.once:
            break
        time.sleep(BACKLOG_POLL_SECONDS if processed >= BATCH_SIZE else POLL_SECONDS)


if __name__ == "__main__":
    main()
