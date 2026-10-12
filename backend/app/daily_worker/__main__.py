"""``python -m app.daily_worker`` — run the Daily Content worker process."""

from __future__ import annotations

from app.daily_worker.runner import run

if __name__ == "__main__":
    run()
