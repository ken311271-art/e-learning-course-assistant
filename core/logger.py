"""應用程式日誌模組。

此模組集中建立 Python logging 設定，讓檔案紀錄、終端輸出與未來 GUI 即時 Log
使用同一套格式。登入、自動化與瀏覽器模組都應透過 get_logger() 取得 logger，
避免各模組自行建立不同格式的紀錄器。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Queue
from typing import Final

from core.config import AppConfig


LOGGER_NAME: Final[str] = "e_learning_assistant"
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


class LogLevel(str, Enum):
    """應用程式支援的日誌層級。

    使用 Enum 可避免其他模組傳入拼錯的層級名稱，也方便未來做 GUI 篩選。
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(slots=True)
class GuiLogMessage:
    """提供給 GUI 顯示的輕量日誌資料。

    GUI 不需要直接處理 logging.LogRecord，因此轉成 dataclass 會比較容易測試與維護。
    """

    created_at: datetime
    level: LogLevel
    message: str
    logger_name: str


class GuiQueueHandler(logging.Handler):
    """把日誌送進 Queue，供 PySide6 Thread 或主視窗安全讀取。

    這裡刻意不直接依賴 PySide6 Signal，讓 core 層維持純 Python；GUI 層之後可以
    用 QTimer 或 Worker Thread 讀取 Queue，再更新畫面。
    """

    def __init__(self, log_queue: Queue[GuiLogMessage]) -> None:
        super().__init__()
        self._log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        """將 logging.LogRecord 轉成 GuiLogMessage。

        emit() 內不拋出例外，避免 GUI Log 顯示失敗時影響主流程。
        """

        try:
            level = LogLevel(record.levelname)
        except ValueError:
            level = LogLevel.INFO

        message = GuiLogMessage(
            created_at=datetime.fromtimestamp(record.created),
            level=level,
            message=self.format(record),
            logger_name=record.name,
        )
        self._log_queue.put(message)


def setup_logging(
    config: AppConfig,
    level: LogLevel = LogLevel.INFO,
    gui_queue: Queue[GuiLogMessage] | None = None,
) -> logging.Logger:
    """初始化全域日誌設定並回傳應用程式 logger。

    重複呼叫時會先清掉既有 handler，避免同一筆 log 被重複寫入多次。
    """

    config.ensure_directories()

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.value)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = _create_file_handler(config.log_dir, formatter)
    console_handler = _create_console_handler(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    if gui_queue is not None:
        gui_handler = GuiQueueHandler(gui_queue)
        gui_handler.setFormatter(formatter)
        logger.addHandler(gui_handler)

    logger.debug("Logging initialized.")
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """取得應用程式 logger 或子 logger。

    傳入 name 時會建立 e_learning_assistant.name 形式的子 logger，方便追蹤來源模組。
    """

    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def _create_file_handler(log_dir: Path, formatter: logging.Formatter) -> RotatingFileHandler:
    """建立可輪替的檔案日誌 handler。

    maxBytes 限制單一檔案大小，backupCount 保留舊檔，避免 logs 資料夾無限制成長。
    """

    log_file = log_dir / "app.log"
    handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)
    return handler


def _create_console_handler(formatter: logging.Formatter) -> logging.StreamHandler:
    """建立終端輸出 handler。

    開發階段可以在 VSCode terminal 直接看到狀態；正式 GUI 仍會以檔案 log 為主。
    """

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)
    return handler
