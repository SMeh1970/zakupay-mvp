"""Background task consumer. Run as a separate Render worker service."""

from __future__ import annotations

import logging
import signal
import time

from automation_pipeline import process_saved_automation_job
from job_queue import claim_task, complete_task, fail_task


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("zakupay.worker")
stopping = False


def _stop(signum, _frame):
    global stopping
    stopping = True
    logger.info("received signal %s; stopping after current task", signum)


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def handle(task: dict) -> dict:
    if task["task_type"] == "match_saved_job":
        return process_saved_automation_job(int(task["payload"]["job_id"]))
    raise ValueError(f"Unknown task type: {task['task_type']}")


def main() -> None:
    logger.info("worker started")
    while not stopping:
        task = claim_task()
        if not task:
            time.sleep(2)
            continue
        logger.info("processing task=%s type=%s", task["id"], task["task_type"])
        try:
            result = handle(task)
        except Exception as exc:
            logger.exception("task=%s failed", task["id"])
            fail_task(task, f"{type(exc).__name__}: {exc}")
        else:
            complete_task(task["id"], result)
            logger.info("task=%s completed", task["id"])
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
