"""Application entry point for the E civil-servant learning assistant."""

from __future__ import annotations

import sys
from queue import Queue

from core.config import AppConfig
from core.logger import GuiLogMessage, get_logger, setup_logging
from core.selectors import SelectorManager


def main() -> None:
    """Configure logging and launch the local HTML-based PySide6 window."""

    config = AppConfig.load()
    config.ensure_directories()

    # Core modules publish log records to this queue; the GUI drains it on a timer.
    log_queue: Queue[GuiLogMessage] = Queue()
    setup_logging(config, gui_queue=log_queue)
    logger = get_logger(__name__)

    selectors = SelectorManager()
    logger.info("%s 專案已初始化。", config.app_name)
    logger.info("已載入 %d 個網站 Selector。", len(selectors.all()))

    try:
        from PySide6.QtWidgets import QApplication

        from gui.web_window import WebMainWindow
    except ModuleNotFoundError as exc:
        logger.error("GUI 執行環境缺少套件：%s", exc.name)
        print(f"{config.app_name} 無法啟動。")
        print("請執行：pip install -r requirements.txt")
        raise SystemExit(1) from exc

    app = QApplication(sys.argv)
    app.setApplicationName(config.app_name)
    window = WebMainWindow(config=config, log_queue=log_queue)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
