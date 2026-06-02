from loguru import logger
import sys
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {thread.name} | {name}:{function}:{line} - {message}"

logger.remove()
logger.add(
    sys.stdout,
    format=fmt,
    level=os.getenv("LOG_LEVEL", "INFO"),
    colorize=True,
)
logger.add(
    os.path.join(LOG_DIR, "bot.log"),
    format=fmt,
    level=os.getenv("LOG_LEVEL", "INFO"),
    rotation="1 day",
    retention="7 days",
    compression="zip",
    encoding="utf-8",
    enqueue=True,
)
