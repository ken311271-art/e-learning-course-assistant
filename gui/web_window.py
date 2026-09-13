"""HTML-based PySide6 window for the E-learning assistant.

The browser automation remains in :class:`BrowserWorker`.  This module only
translates GUI actions to worker commands and serializes worker results for the
local HTML interface through Qt WebChannel.
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow, QMessageBox

from core.automation import AutomationResult, AutomationStatus
from core.browser import BrowserState, BrowserStatus
from core.config import AppConfig
from core.course import CourseInfo
from core.enrollment import EnrollmentCourse, EnrollmentResult
from core.login import LoginResult, LoginStatus
from core.logger import GuiLogMessage, get_logger
from core.survey import SurveyResult
from gui.login_window import BrowserWorker, EnrollmentFormData, LoginFormData


def _json_value(value: Any) -> Any:
    """Convert dataclasses and enums into values accepted by ``json.dumps``."""

    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


class WebBridge(QObject):
    """Expose the existing background worker to the local HTML interface."""

    event = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        log_queue: Queue[GuiLogMessage] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._log_queue = log_queue
        self._logger = get_logger(__name__)
        self._worker = BrowserWorker(config)
        self._busy = False
        self._busy_action = ""
        self._browser_running = False
        self._logged_in = False
        self._study_courses: list[CourseInfo] = []
        self._assessment_courses: list[CourseInfo] = []
        self._enrollment_courses: list[EnrollmentCourse] = []
        self._connect_worker()
        self._start_log_timer()

    def _connect_worker(self) -> None:
        """Connect every worker result to one structured HTML event."""

        self._worker.state_changed.connect(self._on_browser_state)
        self._worker.login_result.connect(self._on_login_result)
        self._worker.course_scan_result.connect(self._on_course_scan)
        self._worker.unanswered_assessment_scan_result.connect(self._on_assessment_scan)
        self._worker.automation_result.connect(self._on_automation_result)
        self._worker.survey_result.connect(self._on_survey_result)
        self._worker.enrollment_search_result.connect(self._on_enrollment_search)
        self._worker.enrollment_submit_result.connect(self._on_enrollment_submit)
        self._worker.task_failed.connect(self._on_task_failed)
        self._worker.task_finished.connect(self._on_task_finished)

    def _start_log_timer(self) -> None:
        """Poll the thread-safe logging queue without blocking the GUI."""

        if self._log_queue is None:
            return
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(250)
        self._log_timer.timeout.connect(self._drain_log_queue)
        self._log_timer.start()

    def _emit(self, event_type: str, **payload: Any) -> None:
        """Send one JSON event to JavaScript."""

        message = {"type": event_type, "payload": _json_value(payload)}
        self.event.emit(json.dumps(message, ensure_ascii=False))

    def _begin(self, action: str, message: str) -> bool:
        """Mark a long-running action busy and reject accidental double clicks."""

        if self._busy:
            self._emit("error", message="目前仍有工作執行中，請稍候。")
            return False
        self._busy = True
        self._busy_action = action
        self._emit("busy", busy=True, action=action, message=message)
        return True

    def _require_index(self, index: int, items: list[Any], message: str) -> Any | None:
        """Validate a selected HTML table row before queueing browser work."""

        if index < 0 or index >= len(items):
            self._emit("error", message=message)
            return None
        return items[index]

    @staticmethod
    def _survey_is_unfilled(status: str) -> bool:
        """Return whether a scanned course explicitly reports an unfilled survey."""

        normalized = "".join(status.split())
        return any(marker in normalized for marker in ("未填", "未填寫", "尚未填"))

    @staticmethod
    def _is_assessment_eligible(course: CourseInfo) -> bool:
        """Use the course scan's reading-time result as the assessment gate."""

        return (
            course.required_reading_seconds > 0
            and course.studied_reading_seconds >= course.required_reading_seconds
        )

    @Slot()
    def initialize(self) -> None:
        """Return initial application state after WebChannel is connected."""

        self._emit(
            "bootstrap",
            app_name=self._config.app_name,
            browser_running=self._browser_running,
            logged_in=self._logged_in,
            busy=self._busy,
        )

    @Slot()
    def startBrowser(self) -> None:  # noqa: N802 - WebChannel API uses JavaScript naming.
        """Start the persistent Chrome browser and detect the saved session."""

        if self._begin("startBrowser", "正在啟動瀏覽器並偵測登入狀態..."):
            self._worker.start_browser()

    @Slot()
    def checkLogin(self) -> None:  # noqa: N802
        """Check the current session and enter the personal area when valid."""

        if self._begin("checkLogin", "正在偵測登入狀態..."):
            self._worker.check_login()

    @Slot(str, str, bool)
    def login(self, account: str, password: str, remember: bool) -> None:
        """Validate and queue the eCPA credential login flow."""

        account = account.strip()
        if not account or not password:
            self._emit("error", message="請先輸入人事服務網帳號與密碼。")
            return
        if self._begin("login", "正在執行登入流程..."):
            self._worker.login(LoginFormData(account, password, remember))

    @Slot()
    def closeBrowser(self) -> None:  # noqa: N802
        """Close only the Playwright browser while keeping the GUI open."""

        if self._begin("closeBrowser", "正在關閉瀏覽器..."):
            self._worker.close_browser()

    @Slot()
    def scanCourses(self) -> None:  # noqa: N802
        """Scan unfinished courses for the study page."""

        if self._begin("scanCourses", "正在跨頁掃描未完成課程..."):
            self._worker.scan_courses()

    @Slot(int)
    def enterStudyCourse(self, index: int) -> None:  # noqa: N802
        """Enter the course selected on the study page."""

        course = self._require_index(index, self._study_courses, "請先選擇一門課程。")
        if course is not None and self._begin("enterCourse", f"正在進入課程：{course.title}"):
            self._worker.enter_course(course)

    @Slot()
    def inspectPlayer(self) -> None:  # noqa: N802
        """Inspect the currently open player and chapter structure."""

        if self._begin("inspectPlayer", "正在偵測目前課程頁..."):
            self._worker.inspect_player()

    @Slot(str)
    def runPlayback(self, indexes_json: str) -> None:  # noqa: N802
        """Run playback for checked courses that still need reading time."""

        try:
            indexes = [int(value) for value in json.loads(indexes_json)]
        except (TypeError, ValueError, json.JSONDecodeError):
            self._emit("error", message="上課課程選取資料格式錯誤。")
            return

        courses = [
            self._study_courses[index]
            for index in indexes
            if 0 <= index < len(self._study_courses)
            and self._study_courses[index].remaining_reading_seconds > 0
        ]
        if not courses:
            self._emit("error", message="請至少勾選一門仍有剩餘閱讀時數的課程。")
            return
        if self._begin("runPlayback", f"正在執行 {len(courses)} 門課程的上課排程..."):
            self._worker.run_playback_schedule(courses)

    @Slot()
    def skipCourse(self) -> None:  # noqa: N802
        """Request skipping the current course without blocking the GUI."""

        if self._busy_action != "runPlayback":
            self._emit("error", message="目前沒有執行中的上課排程。")
            return
        self._worker.skip_current_course()

    @Slot()
    def stopPlayback(self) -> None:  # noqa: N802
        """Stop playback and let the worker return to the personal course list."""

        if self._busy_action != "runPlayback":
            self._emit("error", message="目前沒有執行中的上課排程。")
            return
        self._worker.stop_current_playback()

    @Slot()
    def processSurveys(self) -> None:  # noqa: N802
        """Process courses that the study scan marks qualified and unfilled."""

        if not self._study_courses:
            self._emit("error", message="請先到上課頁執行「掃描課程」。")
            return
        courses = [
            course
            for course in self._study_courses
            if course.required_reading_seconds > 0
            and course.studied_reading_seconds >= course.required_reading_seconds
            and self._survey_is_unfilled(course.survey_status)
        ]
        if not courses:
            self._emit("error", message="沒有閱讀時數已達標且問卷未填的課程。")
            return
        if self._begin("processSurveys", f"正在處理 {len(courses)} 門已達標未填問卷..."):
            self._worker.scan_and_fill_surveys(courses)

    @Slot()
    def scanAssessments(self) -> None:  # noqa: N802
        """Refresh assessment candidates from the most recent course scan only."""

        if not self._study_courses:
            self._emit("error", message="請先到上課頁執行「掃描課程」。")
            return
        self._on_course_scan(self._study_courses)

    @Slot(int)
    def enterAssessmentCourse(self, index: int) -> None:  # noqa: N802
        """Open the selected qualified course directly at its assessment page."""

        course = self._require_index(index, self._assessment_courses, "請先選擇一門課程。")
        if course is not None and self._begin("enterAssessment", f"正在開啟測驗題目頁：{course.title}"):
            self._worker.open_assessment_attempt(course)

    @Slot(int)
    def autoAnswerAssessment(self, index: int) -> None:  # noqa: N802
        """Query roddayeye exam bank and auto-fill answers into the assessment."""

        course = self._require_index(index, self._assessment_courses, "請先選擇一門測驗課程。")
        if course is not None and self._begin("autoAnswerAssessment", f"正在自題庫查詢「{course.title}」解答並自動填答..."):
            self._worker.auto_answer_assessment(course)

    @Slot()
    def fillAssessmentAnswers(self) -> None:  # noqa: N802
        """Read AI answer strings from clipboard and fill them without submission."""

        clipboard = QGuiApplication.clipboard()
        answer_text = clipboard.text().strip() if clipboard is not None else ""
        if not answer_text:
            self._emit("error", message="剪貼簿沒有答案字串。")
            return
        if self._begin("fillAssessmentAnswers", "正在填入剪貼簿答案；不會送出測驗..."):
            self._worker.fill_assessment_answers(answer_text)

    @Slot()
    def submitAssessment(self) -> None:  # noqa: N802
        """Submit the current assessment from the HTML GUI."""

        if self._begin("submitAssessment", "正在送出測驗..."):
            self._worker.submit_assessment()

    @Slot(str)
    def searchEnrollment(self, condition_lines: str) -> None:  # noqa: N802
        """Search enrollment courses using enabled HTML condition rows."""

        if not condition_lines.strip():
            self._emit("error", message="請先加入至少一筆啟用中的選課條件。")
            return
        if self._begin("searchEnrollment", "正在搜尋選課中心..."):
            self._worker.search_enrollment_courses(
                EnrollmentFormData(condition_lines, "", "")
            )

    @Slot(str)
    def enrollSelected(self, indexes_json: str) -> None:  # noqa: N802
        """Enroll checked search results from the HTML table."""

        try:
            indexes = [int(value) for value in json.loads(indexes_json)]
        except (TypeError, ValueError, json.JSONDecodeError):
            self._emit("error", message="報名課程選取資料格式錯誤。")
            return
        courses = [
            self._enrollment_courses[index]
            for index in indexes
            if 0 <= index < len(self._enrollment_courses)
        ]
        if not courses:
            self._emit("error", message="請先勾選至少一門要報名的課程。")
            return
        if self._begin("enrollSelected", f"正在報名 {len(courses)} 門課程..."):
            self._worker.enroll_selected_courses(courses)

    @Slot(object)
    def _on_browser_state(self, state: BrowserState) -> None:
        """Synchronize browser lifecycle fields and HTML button availability."""

        self._browser_running = state.status != BrowserStatus.STOPPED
        if state.status == BrowserStatus.STOPPED:
            self._logged_in = False
            self._busy = False
            self._busy_action = ""
        elif "尚未登入" in state.message:
            self._logged_in = False
        self._emit(
            "browser_state",
            state=state,
            browser_running=self._browser_running,
            logged_in=self._logged_in,
            busy=self._busy,
            busy_action=self._busy_action,
        )

    @Slot(object)
    def _on_login_result(self, result: LoginResult) -> None:
        """Report successful saved-session detection or credential login."""

        self._logged_in = result.status == LoginStatus.LOGGED_IN
        self._emit("login_result", result=result, logged_in=self._logged_in)

    @Slot(object)
    def _on_course_scan(self, courses: list[CourseInfo]) -> None:
        """Store course results and derive assessment candidates from reading time."""

        self._study_courses = list(courses)
        self._assessment_courses = [
            replace(
                course,
                can_attend=True,
                assessment_status="閱讀時數已達標",
                inspection_note="依上課掃描的閱讀時數判斷；未逐門檢查測驗狀態。",
            )
            for course in self._study_courses
            if self._is_assessment_eligible(course)
        ]
        self._emit(
            "course_scan",
            courses=self._study_courses,
            assessment_courses=self._assessment_courses,
        )

    @Slot(object)
    def _on_assessment_scan(self, courses: list[CourseInfo]) -> None:
        """Store read-only assessment status results."""

        self._assessment_courses = list(courses)
        self._emit("assessment_scan", courses=self._assessment_courses)

    @Slot(object)
    def _on_automation_result(self, result: AutomationResult) -> None:
        """Forward player details and copy a requested assessment page locally."""

        notice: tuple[str, str] | None = None
        if result.status == AutomationStatus.ASSESSMENT_OPENED:
            copied = bool(result.assessment_text.strip())
            result = self._copy_assessment_text(result)
            if copied and "複製到剪貼簿失敗" not in result.message:
                notice = ("題目已複製", "已複製題目，請至 AI 貼入並複製答案。")
        elif result.status == AutomationStatus.ASSESSMENT_ANSWERS_FILLED:
            notice = ("答案已填入", "已填入，請送出答案。")

        if result.status in (AutomationStatus.COURSE_COMPLETED, AutomationStatus.COURSE_SKIPPED) and result.course_info is not None:
            for idx, c in enumerate(self._study_courses):
                if c.title == result.course_title or (c.course_url and c.course_url == result.course_info.course_url):
                    self._study_courses[idx] = result.course_info
                    break

        self._emit("automation_result", result=result)
        if notice is not None:
            self._emit("notice", title=notice[0], message=notice[1])

    def _copy_assessment_text(self, result: AutomationResult) -> AutomationResult:
        """Copy extracted assessment text from the worker into the OS clipboard."""

        text = result.assessment_text.strip()
        if not text:
            return replace(
                result,
                message="已開啟測驗題目頁，但沒有可複製的題目文字。",
            )
        try:
            clipboard = QGuiApplication.clipboard()
            if clipboard is None:
                raise RuntimeError("Windows 剪貼簿不可用")
            clipboard.setText(text)
        except Exception as exc:
            self._logger.exception("測驗題目無法寫入剪貼簿。")
            return replace(
                result,
                assessment_text="",
                message=f"已開啟測驗題目頁，但複製到剪貼簿失敗：{exc}",
            )
        return replace(result, assessment_text="")

    @Slot(object)
    def _on_survey_result(self, result: SurveyResult) -> None:
        """Forward one survey progress or summary result."""

        self._emit("survey_result", result=result)

    @Slot(object)
    def _on_enrollment_search(self, courses: list[EnrollmentCourse]) -> None:
        """Store enrollment results so checked HTML indexes remain stable."""

        self._enrollment_courses = list(courses)
        self._emit("enrollment_search", courses=self._enrollment_courses)

    @Slot(object)
    def _on_enrollment_submit(self, results: list[EnrollmentResult]) -> None:
        """Forward enrollment outcomes for result-row status updates."""

        self._emit("enrollment_submit", results=results)

    @Slot(str)
    def _on_task_failed(self, message: str) -> None:
        """Display recoverable errors in HTML without closing the application."""

        self._emit("error", message=message)

    @Slot()
    def _on_task_finished(self) -> None:
        """Re-enable commands after one worker task completes."""

        self._busy = False
        self._busy_action = ""
        self._emit("busy", busy=False, action="", message="")

    @Slot()
    def _drain_log_queue(self) -> None:
        """Move every pending logger message to the HTML log drawer."""

        if self._log_queue is None:
            return
        while True:
            try:
                message = self._log_queue.get_nowait()
            except Empty:
                break
            self._emit(
                "log",
                message=message.message,
                level=message.level.value.lower(),
                created_at=message.created_at.strftime("%H:%M:%S"),
            )

    def shutdown(self) -> None:
        """Stop timers and release browser resources during window shutdown."""

        if hasattr(self, "_log_timer"):
            self._log_timer.stop()
        self._worker.stop()


class DiagnosticWebPage(QWebEnginePage):
    """Log JavaScript console messages to the existing application log."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = get_logger(__name__)

    def javaScriptConsoleMessage(  # noqa: N802
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        message: str,
        line_number: int,
        source_id: str,
    ) -> None:
        """Preserve JavaScript failures with source and line information."""

        self._logger.warning(
            "HTML GUI JavaScript：%s（%s:%d，level=%s）",
            message,
            source_id,
            line_number,
            level,
        )


class WebMainWindow(QMainWindow):
    """Desktop shell that hosts the fully local HTML interface."""

    def __init__(
        self,
        config: AppConfig,
        log_queue: Queue[GuiLogMessage] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._logger = get_logger(__name__)
        self._setup_window()

        self._view = QWebEngineView(self)
        self._page = DiagnosticWebPage(self._view)
        self._view.setPage(self._page)
        self._channel = QWebChannel(self._view.page())
        self._bridge = WebBridge(config, log_queue, self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        self._view.loadFinished.connect(self._on_load_finished)
        self.setCentralWidget(self._view)

        html_path = self._resolve_html_path()
        if not html_path.exists():
            raise FileNotFoundError(f"找不到 HTML 介面：{html_path}")
        self._view.load(QUrl.fromLocalFile(str(html_path)))

    def _resolve_html_path(self) -> Path:
        """Locate HTML assets in source runs and PyInstaller bundles."""

        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            bundled_path = Path(sys._MEIPASS) / "resources" / "ui" / "index.html"
            if bundled_path.exists():
                return bundled_path
        return self._config.resources_dir / "ui" / "index.html"

    def _setup_window(self) -> None:
        """Apply DPI-friendly dimensions while respecting the current screen."""

        self.setWindowTitle(self._config.app_name)
        self.setMinimumSize(1000, 680)
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(self._config.window.width, self._config.window.height)
            return
        area = screen.availableGeometry()
        width = min(max(1000, self._config.window.width), max(1000, area.width() - 80))
        height = min(max(680, self._config.window.height), max(680, area.height() - 80))
        self.resize(width, height)

    @Slot(bool)
    def _on_load_finished(self, loaded: bool) -> None:
        """Report local UI loading failures with a visible native dialog."""

        if loaded:
            self._logger.info("HTML GUI 已載入完成：%s", self._view.url().toString())
            self._view.page().toHtml(self._on_html_source)
            QTimer.singleShot(800, self._probe_html_layout)
            return
        self._logger.error("HTML GUI 載入失敗。")
        QMessageBox.critical(self, "介面載入失敗", "無法載入本機 HTML 介面，請查看 logs/app.log。")

    def _probe_html_layout(self) -> None:
        """Collect non-sensitive DOM metrics for startup diagnostics."""

        script = """
            (() => {
                const visitedPages = [];
                const navigation = Array.from(document.querySelectorAll('.nav-item'));
                navigation.forEach((button) => {
                    button.click();
                    const active = document.querySelector('.page.active');
                    visitedPages.push(active ? active.id : '');
                });
                const loginNavigation = document.querySelector('[data-page="login"]');
                if (loginNavigation) loginNavigation.click();
                const activePage = document.querySelector('.page.active');
                return JSON.stringify({
                    readyState: document.readyState,
                    pageCount: document.querySelectorAll('.page').length,
                    navigationCount: navigation.length,
                    visitedPages,
                    activePage: activePage ? activePage.id : '',
                    viewportWidth: window.innerWidth,
                    viewportHeight: window.innerHeight,
                    bodyScrollWidth: document.body.scrollWidth,
                    bodyScrollHeight: document.body.scrollHeight,
                    bridgeConnected: Boolean(window.qt && window.qt.webChannelTransport),
                    startBrowserEnabled: !document.getElementById('start-browser-button').disabled,
                    studyScanDisabled: document.getElementById('scan-courses-button').disabled,
                    enrollmentResultsHidden: document.getElementById('enrollment-results-view')
                        .classList.contains('hidden')
                });
            })()
        """
        self._view.page().runJavaScript(script, self._on_html_layout_probe)

    def _on_html_layout_probe(self, result: object) -> None:
        """Write startup DOM metrics to the regular application log."""

        self._logger.info("HTML GUI DOM 檢查：%r", result)

    def _on_html_source(self, html: str) -> None:
        """Verify that WebEngine received the expected local document."""

        self._logger.info(
            "HTML GUI 原始碼檢查：長度=%d，包含五分頁=%s",
            len(html),
            'id="page-assessment"' in html,
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Close Playwright cleanly before the desktop process exits."""

        self._bridge.shutdown()
        event.accept()
