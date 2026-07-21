"""應用程式設定。

此模組集中管理路徑、網址、視窗尺寸與 Playwright Persistent Context 所需資料夾。
後續模組只依賴此設定物件，避免把網站網址或資料夾位置散落在各處。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class WindowConfig:
    """GUI 視窗設定。

    使用獨立 dataclass 是為了讓主設定保持清楚，未來可再加入字體、主題或 DPI 選項。
    """

    width: int = 1200
    height: int = 800
    minimum_width: int = 980
    minimum_height: int = 620


@dataclass(slots=True)
class BrowserConfig:
    """Playwright 瀏覽器設定。

    user_data_dir 會給 Persistent Context 使用，用來保存登入後的 Session。
    """

    headless: bool = False
    channel: str | None = "chrome"
    slow_mo_ms: int = 0
    viewport_width: int = 1280
    viewport_height: int = 900
    user_data_dir: Path = Path("user_data")
    chrome_user_data_dir: Path = Path("user_data_chrome")


@dataclass(slots=True)
class AppConfig:
    """應用程式總設定。

    所有模組應透過 AppConfig 取得共用設定，避免直接寫死路徑與網址。
    """

    app_name: str = "E 等公務員學習助手"
    base_url: str = "https://elearn.hrd.gov.tw/mooc/index.php"
    project_root: Path = field(
        default_factory=lambda: (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1]
        )
    )
    log_dir: Path = Path("logs")
    resources_dir: Path = Path("resources")
    window: WindowConfig = field(default_factory=WindowConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)

    @classmethod
    def load(cls) -> "AppConfig":
        """載入設定。

        目前先使用內建預設值；未來若加入 JSON/TOML 設定檔，可在此方法內集中處理。
        """

        config = cls()
        config.log_dir = config.project_root / config.log_dir
        config.resources_dir = config.project_root / config.resources_dir
        config.browser.user_data_dir = config.project_root / config.browser.user_data_dir
        config.browser.chrome_user_data_dir = config.project_root / config.browser.chrome_user_data_dir
        if config.browser.channel == "chrome":
            config.browser.user_data_dir = config.browser.chrome_user_data_dir
        return config

    def ensure_directories(self) -> None:
        """建立應用程式需要的資料夾。

        exist_ok=True 可避免重複啟動程式時因資料夾已存在而發生錯誤。
        """

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.resources_dir.mkdir(parents=True, exist_ok=True)
        self.browser.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.browser.chrome_user_data_dir.mkdir(parents=True, exist_ok=True)
