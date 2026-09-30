"""
Entry point for the Ukrainian voice assistant.

Usage
-----
  uv run python main.py

Prerequisites
-------------
  1. uv sync
  2. Create a .env file:
        OPENAI_API_KEY=sk-...
  3. Run; say "привіт" to wake the assistant.
"""
import logging
import sys

from assistant import Assistant
from config import PID_FILE
from instance_lock import AlreadyRunningError, SingleInstanceLock


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Suppress overly verbose third-party loggers.
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()

    lock = SingleInstanceLock(PID_FILE)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        print(
            f"Асистент уже запущено (PID {exc.pid}) — другий екземпляр не стартує, "
            "щоб мікрофон і голос не дублювались. Спочатку зупини попередній процес "
            f"(Ctrl-C у тому терміналі, або `kill {exc.pid}`)."
        )
        sys.exit(1)

    try:
        assistant = Assistant()
        try:
            assistant.run()
        except KeyboardInterrupt:
            print("\nCtrl-C received — shutting down…")
            assistant.stop()
        except Exception as exc:
            logging.getLogger(__name__).critical("Fatal error: %s", exc, exc_info=True)
            sys.exit(1)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
