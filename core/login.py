"""E 等公務員登入流程模組。

本模組負責第一階段的登入流程：點選 ECPA、填入人事服務網帳密、登入後回到
E 等公務員首頁並進入個人專區。所有網站元素定位都透過 SelectorManager 取得，
流程中不直接寫死 selector。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.config import AppConfig
from core.logger import get_logger
from core.selectors import Selector, SelectorManager, SelectorStrategy

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page
else:
    Locator = Any
    Page = Any


class LoginStatus(str, Enum):
    """登入流程狀態。"""

    NOT_STARTED = "not_started"
    CHECKING_SESSION = "checking_session"
    ENTERING_CREDENTIALS = "entering_credentials"
    LOGGED_IN = "logged_in"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LoginCredentials:
    """人事服務網登入資料。"""

    account: str
    password: str
    remember_credentials: bool = False


@dataclass(slots=True)
class LoginResult:
    """登入流程結果，提供 GUI 更新狀態。"""

    status: LoginStatus
    message: str
    current_url: str = ""
    page_title: str = ""


class LoginError(RuntimeError):
    """登入流程發生可提示錯誤時使用的例外。"""


class LoginService:
    """封裝 ECPA 登入流程。

    此類別只做登入，不處理課程掃描或播放控制，讓第二階段 automation 模組能獨立擴充。
    """

    def __init__(
        self,
        page: Page,
        config: AppConfig,
        selectors: SelectorManager | None = None,
    ) -> None:
        self._page = page
        self._config = config
        self._selectors = selectors or SelectorManager()
        self._logger = get_logger(__name__)

    def login(self, credentials: LoginCredentials) -> LoginResult:
        """執行完整登入流程。

        全流程使用 Playwright 的 locator 與等待 API，不使用固定 sleep。
        """

        try:
            self._logger.info("開始檢查既有 Session。")
            if self.is_logged_in():
                return self.open_personal_area("已偵測到既有登入，已進入個人專區")

            if not credentials.account or not credentials.password:
                raise LoginError("尚未登入，請輸入帳號與密碼。")

            self._logger.info("尚未登入，開始 ECPA 登入流程。")
            self._open_login_dialog()
            self._handle_busy_message_if_present()
            self._select_ecpa_provider()
            self._fill_credentials(credentials)
            self._click("login.submit")

            self._logger.info("已點擊登入送出，等待頁面載入。")
            self._page.wait_for_load_state("domcontentloaded", timeout=30_000)
            self._logger.info("登入送出後目前網址：%s", self._page.url)
            self._ensure_login_succeeded()
            self._open_home()
            return self.open_personal_area("登入成功，已進入個人專區")
        except LoginError:
            raise
        except Exception as exc:
            self._logger.exception("登入流程失敗。")
            raise LoginError(f"登入流程失敗：{exc}") from exc

    def _ensure_login_succeeded(self) -> None:
        """確認 eCPA 登入是否已離開帳密頁。

        eCPA 送出帳密後可能需要數秒才會轉回 E 等公務員。這裡用 Playwright 的
        wait_for_url 等待轉跳，不用固定 sleep，避免太早誤判登入失敗。
        """

        self._logger.info("等待 eCPA 登入後轉跳。")
        try:
            self._page.wait_for_url(lambda url: "ecpa.dgpa.gov.tw" not in url, timeout=30_000)
            self._logger.info("已離開 eCPA 登入頁：%s", self._page.url)
            return
        except Exception:
            self._logger.info("等待 eCPA 轉跳逾時，目前網址：%s", self._page.url)

        if self._is_visible("login.account", timeout_ms=1_000):
            raise LoginError("eCPA 尚未登入成功，請確認帳號密碼或頁面上的驗證訊息。")

        self._logger.info("未看到帳密欄位，繼續交由後續首頁檢查判斷登入狀態。")

    def is_logged_in(self) -> bool:
        """判斷目前 Session 是否仍有效。

        優先用目前頁面與個人專區頁判斷。首頁偶爾不會即時呈現登入狀態，
        但有效 Session 仍可直接進入 ``learn_dashboard.php``。
        """

        if self._has_logged_in_marker():
            return True

        dashboard_url = self._config.base_url.replace("index.php", "user/learn_dashboard.php")
        try:
            self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            return self._has_logged_in_marker()
        except Exception:
            return False

    def _has_logged_in_marker(self) -> bool:
        """Return True when the current page contains logged-in-only markers."""

        try:
            return bool(
                self._page.evaluate(
                    """
                    () => {
                        const text = document.body ? document.body.innerText : "";
                        const hrefs = Array.from(document.querySelectorAll("a"))
                            .map((node) => node.getAttribute("href") || "")
                            .join(" ");
                        return (
                            text.includes("登出") ||
                            text.includes("個人專區") ||
                            text.includes("我的課程")
                        ) && !hrefs.includes("co_login_dialog.php");
                    }
                    """
                )
            )
        except Exception:
            return False

    def open_personal_area(self, message: str = "已進入個人專區") -> LoginResult:
        """進入個人專區。

        首頁同時存在桌機版與手機版的「個人專區」連結，其中部分是 hidden。
        直接前往個人專區網址比文字點擊更穩定，也符合登入完成後的目標頁。
        """

        dashboard_url = self._config.base_url.replace("index.php", "user/learn_dashboard.php")
        self._logger.info("前往個人專區：%s", dashboard_url)
        self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30_000)
        self._logger.info("已進入個人專區，目前網址：%s", self._page.url)
        return self._result(LoginStatus.LOGGED_IN, message)

    def _open_home(self) -> None:
        """開啟 E 等公務員首頁。"""

        self._page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=30_000)

    def _open_login_dialog(self) -> None:
        """直接開啟 E 等公務員登入對話頁。"""

        login_url = self._config.base_url.replace("index.php", "co_login_dialog.php")
        self._logger.info("開啟登入對話頁：%s", login_url)
        self._page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)

    def _handle_busy_message_if_present(self) -> None:
        """處理可能出現的「目前人數太多」提示。"""

        self._logger.info("檢查是否出現目前人數太多提示。")
        if self._try_click("login.busy_confirm", timeout_ms=3_000):
            self._logger.info("已確認目前人數太多提示。")
        else:
            self._logger.info("未出現目前人數太多提示，繼續登入流程。")

    def _select_ecpa_provider(self) -> None:
        """選擇左上第一個人事服務網 ECPA 登入方式。"""

        self._logger.info("準備選擇人事服務網 ECPA。")
        if self._try_click("login.ecpa_provider", timeout_ms=10_000):
            self._logger.info("已選擇人事服務網 ECPA。")
            return
        raise LoginError("找不到人事服務網 ECPA 登入入口。")

    def _select_account_password_login(self) -> None:
        """選擇帳號密碼登入。"""

        self._logger.info("準備選擇帳號密碼登入。")
        if self._try_click("login.account_password_mode", timeout_ms=10_000):
            self._logger.info("已選擇帳號密碼登入。")
            return
        self._logger.info("未找到帳號密碼登入選項，可能已直接進入帳密頁。")

    def _fill_credentials(self, credentials: LoginCredentials) -> None:
        """填入人事服務網帳號與密碼。"""

        self._logger.info("正在填入登入資訊。")
        self._logger.info("等待 eCPA 帳號欄位。")
        account = self._wait_for("login.account")
        self._logger.info("等待 eCPA 密碼欄位。")
        password = self._wait_for("login.password")
        self._logger.info("填入 eCPA 帳號。")
        account.fill(credentials.account)
        self._logger.info("填入 eCPA 密碼。")
        password.fill(credentials.password)

    def _click(self, selector_key: str) -> None:
        """等待指定元素可見後點擊。"""

        self._logger.info("準備點擊元素：%s", selector_key)
        locator = self._wait_for(selector_key)
        locator.click(timeout=self._selectors.get(selector_key).timeout_ms)
        self._logger.info("已點擊元素：%s", selector_key)

    def _wait_for(self, selector_key: str) -> Locator:
        """等待 selector 對應元素出現並回傳 Locator。"""

        selector = self._selectors.get(selector_key)
        self._logger.info("等待元素可見：%s", selector_key)
        locator = self._locator(selector)
        locator.wait_for(state="visible", timeout=selector.timeout_ms)
        self._logger.info("元素已可見：%s", selector_key)
        return locator

    def _is_visible(self, selector_key: str, timeout_ms: int = 3_000) -> bool:
        """判斷指定 selector 是否可見。"""

        selector = self._selectors.get(selector_key)
        try:
            self._locator(selector).wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _try_click(self, selector_key: str, timeout_ms: int | None = None) -> bool:
        """嘗試點擊元素，找不到時回傳 False。"""

        selector = self._selectors.get(selector_key)
        locator = self._locator(selector)
        try:
            locator.wait_for(state="visible", timeout=timeout_ms or selector.timeout_ms)
            locator.click(timeout=timeout_ms or selector.timeout_ms)
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            return True
        except Exception as exc:
            self._logger.info("未能點擊 %s：%s", selector_key, exc)
            return False

    def _locator(self, selector: Selector) -> Locator:
        """依 SelectorStrategy 建立 Playwright Locator。"""

        if selector.strategy == SelectorStrategy.TEXT:
            return self._page.get_by_text(selector.value, exact=False).first
        if selector.strategy == SelectorStrategy.ROLE:
            if selector.role_name is None:
                raise LoginError(f"Selector {selector.key} 缺少 role_name。")
            return self._page.get_by_role(selector.role_name, name=selector.value).first
        if selector.strategy == SelectorStrategy.LABEL:
            return self._page.get_by_label(selector.value).first
        if selector.strategy == SelectorStrategy.PLACEHOLDER:
            return self._page.get_by_placeholder(selector.value).first
        return self._page.locator(selector.value).first

    def _result(self, status: LoginStatus, message: str) -> LoginResult:
        """建立登入結果。"""

        return LoginResult(
            status=status,
            message=message,
            current_url=self._page.url,
            page_title=self._page.title(),
        )
