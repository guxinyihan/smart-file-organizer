"""Logging is configured only for execution, never as an import side effect."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(state_dir: Path) -> logging.Logger:
    logger = logging.getLogger("smartsort")
    logger.setLevel(logging.INFO)
    target = state_dir / "smartsort.log"
    if not any(getattr(h, "baseFilename", "") == str(target.resolve()) for h in logger.handlers):
        state_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger
