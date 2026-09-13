"""Playwright 瀏覽器控制模組。

本模組只負責瀏覽器生命週期與頁面狀態，不處理登入細節。登入流程會放在
core.login，未來課程自動化會放在 automation 相關模組，避免功能互相耦合。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config import AppConfig
from core.logger import get_logger

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Playwright
else:
    BrowserContext = Any
    Page = Any
    Playwright = Any


class BrowserStatus(str, Enum):
    """瀏覽器目前狀態。

    GUI 可直接使用此狀態顯示「未啟動、啟動中、已就緒、錯誤」等訊息。
    """

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    NAVIGATING = "navigating"
    ERROR = "error"


@dataclass(slots=True)
class BrowserState:
    """提供給 GUI 顯示的瀏覽器狀態資料。"""

    status: BrowserStatus = BrowserStatus.STOPPED
    current_url: str = ""
    page_title: str = ""
    message: str = "瀏覽器尚未啟動"


class BrowserControllerError(RuntimeError):
    """瀏覽器控制層發生錯誤時使用的例外。"""


class BrowserNotStartedError(BrowserControllerError):
    """瀏覽器尚未啟動，但外部要求操作頁面時使用的例外。"""


class BrowserController:
    """封裝 Playwright Persistent Context。

    Persistent Context 會把 cookies、local storage 等登入狀態保存在 user_data_dir。
    下一次啟動時會重用同一份資料，讓有效 Session 不必重新登入。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._logger = get_logger(__name__)
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._state = BrowserState()

    @property
    def state(self) -> BrowserState:
        """回傳目前瀏覽器狀態。

        回傳 dataclass 物件，讓 GUI 可以用固定欄位更新畫面。
        """

        return self._state

    @property
    def page(self) -> Page:
        """取得目前頁面。

        若瀏覽器尚未啟動，拋出明確例外，讓 GUI 可提示使用者先啟動瀏覽器。
        """

        if not self.is_running or self._page is None:
            raise BrowserNotStartedError("瀏覽器尚未啟動。")
        return self._page

    @property
    def is_running(self) -> bool:
        """判斷 Persistent Context 是否已啟動。"""

        if self._context is None or self._page is None:
            return False

        try:
            _ = self._page.url
            return not self._page.is_closed()
        except Exception:
            self._mark_browser_disconnected("瀏覽器已被手動關閉。")
            return False

    def set_active_page(self, page: Page) -> BrowserState:
        """Keep the browser controller pointed at a popup opened by the user flow."""

        try:
            if page.is_closed():
                raise BrowserControllerError("測驗頁已被關閉，請重新開啟題目頁。")
        except BrowserControllerError:
            raise
        except Exception as exc:
            raise BrowserControllerError(f"無法切換到測驗頁：{exc}") from exc
        self._page = page
        return self.refresh_state("已切換到目前測驗頁。")

    def start(self) -> BrowserState:
        """啟動 Chromium Persistent Context。

        此方法不使用固定 sleep；頁面載入交由 goto() 的 wait_until 與 timeout 控制。
        """

        if self.is_running:
            self._logger.info("瀏覽器已啟動，略過重複啟動。")
            return self.refresh_state("瀏覽器已啟動")

        self._set_state(BrowserStatus.STARTING, message="正在啟動瀏覽器")
        self._config.ensure_directories()

        try:
            self._prepare_bundled_playwright_browsers()
            sync_playwright = _load_sync_playwright()
            self._playwright = sync_playwright().start()
            self._context = self._launch_persistent_context()
            self._context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                """
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._logger.info("瀏覽器已啟動，Session 資料夾：%s", self._config.browser.user_data_dir)
            return self.refresh_state("瀏覽器已就緒")
        except ImportError as exc:
            self._set_state(BrowserStatus.ERROR, message="尚未安裝 Playwright")
            self._logger.exception("尚未安裝 Playwright。")
            self.close()
            raise BrowserControllerError("尚未安裝 Playwright，請先安裝 requirements.txt。") from exc
        except Exception as exc:
            self._set_state(BrowserStatus.ERROR, message=f"瀏覽器啟動失敗：{exc}")
            self._logger.exception("瀏覽器啟動失敗。")
            self.close()
            raise BrowserControllerError("瀏覽器啟動失敗，請確認 Playwright 瀏覽器已安裝。") from exc

    def _launch_persistent_context(self) -> BrowserContext:
        """Launch Chrome first, then fall back to bundled Chromium in offline builds."""

        launch_options: dict[str, Any] = {
            "user_data_dir": str(self._config.browser.user_data_dir),
            "headless": self._config.browser.headless,
            "slow_mo": self._config.browser.slow_mo_ms,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=IsolateOrigins,site-per-process",
                "--mute-audio",  # [自訂設定] 全域靜音：播放影片時不發出聲音，但不影響時數累積
            ],
            # Use the actual screen size in headful Chrome. A fixed viewport
            # makes long assessment pages cramped despite a large window.
            "viewport": None,
            "locale": "zh-TW",
            "timezone_id": "Asia/Taipei",
        }
        if self._config.browser.channel:
            try:
                return self._playwright.chromium.launch_persistent_context(
                    channel=self._config.browser.channel,
                    **launch_options,
                )
            except Exception:
                self._logger.exception("啟動 Chrome 失敗，改用內附 Chromium。")
        return self._playwright.chromium.launch_persistent_context(**launch_options)

    def _prepare_bundled_playwright_browsers(self) -> None:
        """Point Playwright to bundled browser files when running from a packaged folder."""

        executable_dir = Path(sys.executable).resolve().parent
        bundled_path = executable_dir / "ms-playwright"
        if getattr(sys, "frozen", False) and bundled_path.exists():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_path))

    def open_home(self) -> BrowserState:
        """開啟 E 等公務員首頁。

        使用 wait_until='domcontentloaded' 等待 DOM 可操作，不使用固定 sleep。
        """

        page = self.page
        self._set_state(BrowserStatus.NAVIGATING, message="正在開啟 E 等公務員網站")

        try:
            page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=30_000)
            self._logger.info("已開啟首頁：%s", self._config.base_url)
            return self.refresh_state("已開啟 E 等公務員網站")
        except Exception as exc:
            if exc.__class__.__name__ != "TimeoutError":
                self._set_state(BrowserStatus.ERROR, message=f"開啟首頁失敗：{exc}")
                self._logger.exception("開啟首頁失敗。")
                raise BrowserControllerError("開啟首頁失敗，請檢查網路或網站狀態。") from exc

            self._set_state(BrowserStatus.ERROR, message="開啟首頁逾時，可重新執行")
            self._logger.exception("開啟首頁逾時。")
            raise BrowserControllerError("開啟首頁逾時，請稍後重新執行。") from exc

    def refresh_state(self, message: str = "") -> BrowserState:
        """重新讀取目前網址與頁面標題。

        讀取標題可能因頁面關閉而失敗，因此以例外處理保護主流程。
        """

        if not self.is_running or self._page is None:
            self._set_state(BrowserStatus.STOPPED, message=message or "瀏覽器尚未啟動")
            return self._state

        try:
            self._state = BrowserState(
                status=BrowserStatus.READY,
                current_url=self._page.url,
                page_title=self._page.title(),
                message=message or "瀏覽器已就緒",
            )
        except Exception as exc:
            self._set_state(BrowserStatus.ERROR, message=f"讀取頁面狀態失敗：{exc}")
            self._logger.exception("讀取頁面狀態失敗。")
        return self._state

    def _mark_browser_disconnected(self, message: str) -> None:
        """Reset local references after the user closes the browser manually."""

        self._logger.info(message)
        self._context = None
        self._page = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            self._logger.exception("手動關閉瀏覽器後停止 Playwright 時發生錯誤。")
        finally:
            self._playwright = None
            self._set_state(BrowserStatus.STOPPED, message=message)

    def close(self) -> None:
        """安全關閉瀏覽器與 Playwright。

        close() 可重複呼叫；發生錯誤只記錄 log，不讓應用程式直接崩潰。
        """

        try:
            if self._context is not None:
                self._context.close()
                self._logger.info("瀏覽器 Context 已關閉。")
        except Exception:
            self._logger.exception("關閉瀏覽器 Context 時發生錯誤。")
        finally:
            self._context = None
            self._page = None

        try:
            if self._playwright is not None:
                self._playwright.stop()
                self._logger.info("Playwright 已停止。")
        except Exception:
            self._logger.exception("停止 Playwright 時發生錯誤。")
        finally:
            self._playwright = None
            self._set_state(BrowserStatus.STOPPED, message="瀏覽器已關閉")

    def _set_state(
        self,
        status: BrowserStatus,
        *,
        message: str,
        current_url: str = "",
        page_title: str = "",
    ) -> None:
        """更新內部狀態。

        集中更新可避免不同方法漏填欄位，未來若要發送 GUI Signal 也可從這裡擴充。
        """

        self._state = BrowserState(
            status=status,
            current_url=current_url,
            page_title=page_title,
            message=message,
        )


def _load_sync_playwright() -> Any:
    """延遲載入 Playwright。

    這讓尚未安裝 Playwright 的開發環境仍可啟動 GUI；只有使用者按下「啟動瀏覽器」
    時才會提示缺少相依套件。
    """

    from playwright.sync_api import sync_playwright

    return sync_playwright
