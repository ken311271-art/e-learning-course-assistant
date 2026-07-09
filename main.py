"""E 等公務員學習助手的程式進入點。

目前第一階段先建立專案骨架與核心模組；後續 GUI 與登入流程會逐步接上。
"""

from __future__ import annotations

import sys
from queue import Queue

from core.config import AppConfig
from core.logger import GuiLogMessage, get_logger, setup_logging
from core.selectors import SelectorManager


def main() -> None:
    """啟動應用程式。

    若 PySide6 尚未安裝，會保留命令列提示，不讓程式直接以堆疊錯誤結束。
    """

    config = AppConfig.load()
    config.ensure_directories()

    log_queue: Queue[GuiLogMessage] = Queue()
    setup_logging(config, gui_queue=log_queue)

    logger = get_logger(__name__)
    selectors = SelectorManager()

    logger.info("%s 專案已初始化。", config.app_name)
    logger.info("已載入 %d 個網站 Selector。", len(selectors.all()))

    try:
        from PySide6.QtWidgets import QApplication

        from gui.login_window import LoginWindow
    except ModuleNotFoundError as exc:
        logger.error("GUI 相依套件尚未安裝：%s", exc.name)
        print(f"{config.app_name} 專案已初始化。")
        print("尚未安裝 GUI 相依套件，請先執行：pip install -r requirements.txt")
        raise SystemExit(1) from exc

    app = QApplication(sys.argv)
    app.setApplicationName(config.app_name)

    window = LoginWindow(config=config, log_queue=log_queue)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
