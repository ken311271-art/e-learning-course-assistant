"""Login window and background browser worker.

The GUI only collects user input and displays state.  All Playwright work runs
inside a dedicated Python thread so the PySide6 window stays responsive while
the browser logs in or scans courses.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from queue import Empty, Queue
from threading import Event, Thread
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QHeaderView,
    QPlainTextEdit,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.browser import BrowserController, BrowserControllerError, BrowserState, BrowserStatus
from core.automation import (
    AutomationError,
    AutomationResult,
    AutomationStatus,
    CourseAutomation,
    EXTRA_PLAYBACK_SECONDS_PER_COURSE,
    PlaybackScheduleConfig,
    PlaybackScheduler,
)
from core.config import AppConfig
from core.course import CourseInfo, CourseScanError, CourseScanner
from core.enrollment import (
    EnrollmentAssistant,
    EnrollmentCourse,
    EnrollmentError,
    EnrollmentResult,
    EnrollmentSearchConfig,
)
from core.login import LoginCredentials, LoginError, LoginResult, LoginService
from core.logger import GuiLogMessage, get_logger
from core.survey import SurveyAutomation, SurveyError, SurveyResult, SurveyStatus


@dataclass(slots=True)
class LoginFormData:
    """Credential data copied from the GUI before sending work to the thread."""

    account: str
    password: str
    remember_credentials: bool


@dataclass(slots=True)
class EnrollmentFormData:
    """Batch enrollment search settings copied from the GUI."""

    keywords: str
    hours_minimum: str
    hours_maximum: str


def _format_minutes_seconds(total_seconds: int) -> str:
    """Format a duration for logs as total minutes and seconds."""

    minutes, seconds = divmod(max(0, int(total_seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"


class BrowserWorker(QObject):
    """Thread-backed worker that owns the Playwright browser controller."""

    state_changed = Signal(object)
    login_result = Signal(object)
    course_scan_result = Signal(object)
    unanswered_assessment_scan_result = Signal(object)
    automation_result = Signal(object)
    survey_result = Signal(object)
    enrollment_search_result = Signal(object)
    enrollment_submit_result = Signal(object)
    task_failed = Signal(str)
    task_finished = Signal()

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._browser: BrowserController | None = None
        self._logger = get_logger(__name__)
        self._commands: Queue[tuple[str, object | None]] = Queue()
        self._stop_event = Event()
        self._playback_stop_event = Event()
        self._skip_course_event = Event()
        self._thread = Thread(target=self._run_loop, name="BrowserWorkerThread", daemon=True)
        self._thread.start()

    @Slot()
    def start_browser(self) -> None:
        """Queue browser startup."""

        self._commands.put(("start_browser", None))

    @Slot()
    def check_login(self) -> None:
        """Queue checking whether the current browser session is logged in."""

        self._commands.put(("check_login", None))

    @Slot(object)
    def login(self, form_data: LoginFormData) -> None:
        """Queue the eCPA login flow."""

        self._commands.put(("login", form_data))

    @Slot()
    def scan_courses(self) -> None:
        """Queue read-only unfinished-course scanning."""

        self._commands.put(("scan_courses", None))

    @Slot()
    def scan_unanswered_assessments(self) -> None:
        """Queue read-only scanning for explicitly unanswered assessments."""

        self._commands.put(("scan_unanswered_assessments", None))

    @Slot(object)
    def enter_course(self, course: CourseInfo) -> None:
        """Queue entering a selected course."""

        self._commands.put(("enter_course", course))

    @Slot()
    def inspect_player(self) -> None:
        """Queue inspecting the current course player page."""

        self._commands.put(("inspect_player", None))

    @Slot()
    @Slot(object)
    def run_playback_schedule(self, courses: list[CourseInfo] | None = None) -> None:
        """Queue playback for the GUI-selected courses."""

        self._commands.put(("run_playback_schedule", courses))

    @Slot()
    @Slot(object)
    def scan_and_fill_surveys(self, courses: list[CourseInfo] | None = None) -> None:
        """Queue survey submission for courses selected by reading-time state."""

        self._commands.put(("scan_and_fill_surveys", courses))

    @Slot(object)
    def search_enrollment_courses(self, form_data: EnrollmentFormData) -> None:
        """Queue searching registration-center courses."""

        self._commands.put(("search_enrollment_courses", form_data))

    @Slot(object)
    def enroll_selected_courses(self, courses: list[EnrollmentCourse]) -> None:
        """Queue enrolling the selected registration-center courses."""

        self._commands.put(("enroll_selected_courses", courses))

    @Slot()
    def skip_current_course(self) -> None:
        """Skip the course currently being played by the scheduler."""

        self._skip_course_event.set()
        self.automation_result.emit(
            AutomationResult(
                status=AutomationStatus.PLAYBACK_WAITING,
                message="已收到跳過課程指令，準備切換下一門課。",
            )
        )

    @Slot()
    def stop_current_playback(self) -> None:
        """Stop only the active playback schedule and keep the worker alive."""

        self._playback_stop_event.set()
        self.automation_result.emit(
            AutomationResult(
                status=AutomationStatus.PLAYBACK_WAITING,
                message="已收到停止指令，正在返回個人課程清單。",
            )
        )

    @Slot()
    def close_browser(self) -> None:
        """Queue browser shutdown."""

        self._commands.put(("close_browser", None))

    def stop(self, timeout_seconds: float = 3.0) -> None:
        """Stop the worker thread and close browser resources."""

        self._stop_event.set()
        self._playback_stop_event.set()
        self._commands.put(("close_browser", None))
        self._commands.put(("stop", None))
        self._thread.join(timeout_seconds)

    def _run_loop(self) -> None:
        """Process browser commands one at a time in the worker thread."""

        while not self._stop_event.is_set():
            command, payload = self._commands.get()
            if command == "stop":
                break
            if command == "start_browser":
                self._run_task(self._start_browser_impl)
            elif command == "check_login":
                self._run_task(self._check_login_impl)
            elif command == "login":
                self._run_task(lambda: self._login_impl(payload))
            elif command == "scan_courses":
                self._run_task(self._scan_courses_impl)
            elif command == "scan_unanswered_assessments":
                self._run_task(self._scan_unanswered_assessments_impl)
            elif command == "enter_course":
                self._run_task(lambda: self._enter_course_impl(payload))
            elif command == "inspect_player":
                self._run_task(self._inspect_player_impl)
            elif command == "run_playback_schedule":
                self._run_task(lambda: self._run_playback_schedule_impl(payload))
            elif command == "scan_and_fill_surveys":
                self._run_task(lambda: self._scan_and_fill_surveys_impl(payload))
            elif command == "search_enrollment_courses":
                self._run_task(lambda: self._search_enrollment_courses_impl(payload))
            elif command == "enroll_selected_courses":
                self._run_task(lambda: self._enroll_selected_courses_impl(payload))
            elif command == "close_browser":
                self._close_browser_impl()

    def _run_task(self, task: Callable[[], None]) -> None:
        """Run one task and report failures back to the GUI instead of exiting."""

        try:
            task()
        except (BrowserControllerError, LoginError, CourseScanError, AutomationError, EnrollmentError, SurveyError) as exc:
            self._logger.exception("背景工作失敗。")
            self.task_failed.emit(str(exc))
        except BaseException as exc:
            self._logger.exception("背景工作發生未預期錯誤。")
            self.task_failed.emit(f"背景工作發生未預期錯誤：{exc}")
        finally:
            self.task_finished.emit()

    def _start_browser_impl(self) -> None:
        """Start the persistent browser and open the E learning homepage."""

        if self._browser is None:
            self._browser = BrowserController(self._config)
        self.state_changed.emit(self._browser.start())
        self.state_changed.emit(self._browser.open_home())
        self._check_login_impl(show_not_logged_in=False)

    def _check_login_impl(self, show_not_logged_in: bool = True) -> None:
        """Detect whether the persistent browser session is already logged in."""

        if self._browser is None or not self._browser.is_running:
            raise LoginError("請先啟動瀏覽器。")

        service = LoginService(page=self._browser.page, config=self._config)
        if service.is_logged_in():
            result = service.open_personal_area("已偵測到既有登入，已進入個人專區")
            self.login_result.emit(result)
            self.state_changed.emit(self._browser.refresh_state(result.message))
            return

        message = "尚未登入，請輸入帳號與密碼後按登入。"
        if show_not_logged_in:
            self.state_changed.emit(self._browser.refresh_state(message))
        else:
            self.state_changed.emit(self._browser.refresh_state(message))

    def _login_impl(self, payload: object | None) -> None:
        """Run the login service with user-provided credentials."""

        if not isinstance(payload, LoginFormData):
            raise LoginError("登入資料格式錯誤。")
        if self._browser is None or not self._browser.is_running:
            raise LoginError("請先啟動瀏覽器。")

        service = LoginService(page=self._browser.page, config=self._config)
        result = service.login(
            LoginCredentials(
                account=payload.account,
                password=payload.password,
                remember_credentials=payload.remember_credentials,
            )
        )
        self.login_result.emit(result)
        self.state_changed.emit(self._browser.refresh_state(result.message))

    def _scan_courses_impl(self) -> None:
        """Scan unfinished courses and enrich them with reading-time data."""

        if self._browser is None or not self._browser.is_running:
            raise CourseScanError("請先啟動瀏覽器並完成登入。")

        scanner = CourseScanner(page=self._browser.page, config=self._config)
        courses = scanner.scan_unfinished_courses()
        automation = CourseAutomation(page=self._browser.page, config=self._config)
        enriched_courses: list[CourseInfo] = []
        for index, course in enumerate(courses, start=1):
            try:
                study_time = automation.read_course_study_time(course)
                enriched = replace(
                    course,
                    required_reading_seconds=study_time.required_seconds,
                    studied_reading_seconds=study_time.studied_seconds,
                    remaining_reading_seconds=study_time.remaining_seconds,
                    survey_status=study_time.survey_status or course.survey_status,
                )
                enriched_courses.append(enriched)
                self._logger.info(
                    "閱讀時數 %d/%d：%s；門檻=%s；已閱讀=%s；剩餘=%s",
                    index,
                    len(courses),
                    course.title,
                    _format_minutes_seconds(study_time.required_seconds),
                    _format_minutes_seconds(study_time.studied_seconds),
                    _format_minutes_seconds(study_time.remaining_seconds),
                )
            except Exception as exc:
                self._logger.warning("讀取課程閱讀時數失敗，保留課程資料：%s；%s", course.title, exc)
                enriched_courses.append(course)
        courses = enriched_courses
        self.course_scan_result.emit(courses)
        self.state_changed.emit(self._browser.refresh_state("課程掃描完成。"))

    def _scan_unanswered_assessments_impl(self) -> None:
        """Scan dashboard metadata for courses with unanswered assessments."""

        if self._browser is None or not self._browser.is_running:
            raise CourseScanError("請先啟動瀏覽器並完成登入。")

        scanner = CourseScanner(page=self._browser.page, config=self._config)
        courses = scanner.scan_unanswered_assessment_courses()
        self.unanswered_assessment_scan_result.emit(courses)
        self.state_changed.emit(self._browser.refresh_state("未答測驗掃描完成。"))

    def _enter_course_impl(self, payload: object | None) -> None:
        """Enter the selected course player shell."""

        if not isinstance(payload, CourseInfo):
            raise AutomationError("請先選擇一門課程。")
        if self._browser is None or not self._browser.is_running:
            raise AutomationError("請先啟動瀏覽器並完成登入。")

        automation = CourseAutomation(page=self._browser.page, config=self._config)
        result = automation.enter_course(payload)
        if payload.required_reading_seconds > 0:
            result = replace(
                result,
                remaining_seconds=(
                    payload.remaining_reading_seconds + EXTRA_PLAYBACK_SECONDS_PER_COURSE
                ),
            )
        self.automation_result.emit(result)
        self.state_changed.emit(self._browser.refresh_state(result.message))

    def _inspect_player_impl(self) -> None:
        """Inspect the current course player state."""

        if self._browser is None or not self._browser.is_running:
            raise AutomationError("請先啟動瀏覽器並進入課程。")

        automation = CourseAutomation(page=self._browser.page, config=self._config)
        result = automation.inspect_player()
        self.automation_result.emit(result)
        self.state_changed.emit(self._browser.refresh_state(result.message))

    def _run_playback_schedule_impl(self, payload: object | None) -> None:
        """Run playback for explicit GUI courses or scan for legacy callers."""

        if self._browser is None or not self._browser.is_running:
            raise AutomationError("請先啟動瀏覽器並完成登入。")

        schedule_path = self._config.resources_dir / "course_schedule.json"
        schedule = PlaybackScheduleConfig.load(schedule_path)
        if payload is None:
            courses = CourseScanner(page=self._browser.page, config=self._config).scan_unfinished_courses()
        elif isinstance(payload, list) and all(isinstance(item, CourseInfo) for item in payload):
            courses = list(payload)
            schedule = replace(schedule, selected_course_titles=())
        else:
            raise AutomationError("上課課程選取資料格式錯誤。")
        if not courses:
            raise AutomationError("請至少勾選一門仍有剩餘閱讀時數的課程。")

        self._playback_stop_event.clear()
        self._skip_course_event.clear()
        scheduler = PlaybackScheduler(
            page=self._browser.page,
            config=self._config,
            schedule=schedule,
            stop_event=self._playback_stop_event,
            skip_course_event=self._skip_course_event,
        )
        results = scheduler.run(courses, emit_result=self.automation_result.emit)
        final_message = results[-1].message if results else "上課排程已結束。"
        self.state_changed.emit(self._browser.refresh_state(final_message))

    def _scan_and_fill_surveys_impl(self, payload: object | None) -> None:
        """Submit surveys for reading-time-qualified courses from the study scan."""

        if self._browser is None or not self._browser.is_running:
            raise SurveyError("請先啟動瀏覽器並完成登入。")

        login_service = LoginService(page=self._browser.page, config=self._config)
        if not login_service.is_logged_in():
            raise SurveyError("請先登入後再掃描問卷。")

        if payload is None:
            courses = CourseScanner(page=self._browser.page, config=self._config).scan_unfinished_courses()
        elif isinstance(payload, list) and all(isinstance(item, CourseInfo) for item in payload):
            courses = list(payload)
        else:
            raise SurveyError("問卷課程資料格式錯誤。")
        if not courses:
            raise SurveyError("上課掃描結果中沒有閱讀時數已達標且問卷未填的課程。")

        automation = SurveyAutomation(page=self._browser.page, config=self._config)
        automation.process_courses(courses, emit_result=self.survey_result.emit)
        self.state_changed.emit(self._browser.refresh_state("問卷掃描與填寫完成。"))

    def _search_enrollment_courses_impl(self, payload: object | None) -> None:
        """Search registration-center courses and return rows to the GUI."""

        if not isinstance(payload, EnrollmentFormData):
            raise EnrollmentError("選課設定格式錯誤。")
        if self._browser is None or not self._browser.is_running:
            raise EnrollmentError("請先啟動瀏覽器並完成登入。")

        login_service = LoginService(page=self._browser.page, config=self._config)
        if not login_service.is_logged_in():
            raise EnrollmentError("請先登入後再開啟選課系統。")

        assistant = EnrollmentAssistant(page=self._browser.page, config=self._config)
        courses = assistant.search_courses(
            EnrollmentSearchConfig(
                keywords=payload.keywords,
                hours_minimum=payload.hours_minimum,
                hours_maximum=payload.hours_maximum,
            )
        )
        self.enrollment_search_result.emit(courses)
        self.state_changed.emit(self._browser.refresh_state(f"選課搜尋完成：找到 {len(courses)} 門可報名課程。"))

    def _enroll_selected_courses_impl(self, payload: object | None) -> None:
        """Enroll selected courses from the GUI table."""

        if not isinstance(payload, list) or not all(isinstance(item, EnrollmentCourse) for item in payload):
            raise EnrollmentError("報名課程資料格式錯誤。")
        if self._browser is None or not self._browser.is_running:
            raise EnrollmentError("請先啟動瀏覽器並完成登入。")

        assistant = EnrollmentAssistant(page=self._browser.page, config=self._config)
        results = assistant.enroll_courses(payload)
        self.enrollment_submit_result.emit(results)
        success_count = sum(1 for result in results if result.success)
        self.state_changed.emit(self._browser.refresh_state(f"報名處理完成：成功 {success_count} / {len(results)}。"))

    def _close_browser_impl(self) -> None:
        """Close the browser if it is running."""

        if self._browser is not None:
            self._browser.close()
            self.state_changed.emit(self._browser.state)


class LoginWindow(QMainWindow):
    """Main application window for login status and course scanning."""

    request_start_browser = Signal()
    request_check_login = Signal()
    request_login = Signal(object)
    request_scan_courses = Signal()
    request_scan_unanswered_assessments = Signal()
    request_enter_course = Signal(object)
    request_inspect_player = Signal()
    request_run_playback_schedule = Signal()
    request_scan_and_fill_surveys = Signal()
    request_search_enrollment_courses = Signal(object)
    request_enroll_selected_courses = Signal(object)
    request_skip_course = Signal()
    request_close_browser = Signal()

    def __init__(self, config: AppConfig, log_queue: Queue[GuiLogMessage] | None = None) -> None:
        super().__init__()
        self._config = config
        self._log_queue = log_queue
        self._logger = get_logger(__name__)
        self._browser_worker = BrowserWorker(config)
        self._is_logged_in = False
        self._browser_running = False
        self._courses: list[CourseInfo] = []
        self._enrollment_courses: list[EnrollmentCourse] = []

        self._setup_window()
        self._build_ui()
        self._connect_browser_worker()
        self._start_log_timer()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Close browser resources when the window is closed."""

        self.request_close_browser.emit()
        self._browser_worker.stop()
        super().closeEvent(event)

    def _setup_window(self) -> None:
        """Configure basic window size, title, and dark theme."""

        self.setWindowTitle(self._config.app_name)
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        target_width = min(self._config.window.width, available.width()) if available else self._config.window.width
        target_height = min(self._config.window.height, available.height()) if available else self._config.window.height
        minimum_width = min(self._config.window.minimum_width, target_width)
        minimum_height = min(self._config.window.minimum_height, target_height)
        self.setMinimumSize(minimum_width, minimum_height)
        self.resize(target_width, target_height)
        self.setStyleSheet(_dark_stylesheet())

    def _build_ui(self) -> None:
        """Create the full login, status, course, and log layout."""

        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(14)

        title_label = QLabel(self._config.app_name)
        title_label.setObjectName("TitleLabel")
        root_layout.addWidget(title_label)

        tabs = QTabWidget()
        tabs.addTab(self._create_learning_tab(), "學習助手")
        tabs.addTab(self._create_enrollment_tab(), "選課系統")
        root_layout.addWidget(tabs, 4)

        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("LogBox")
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("執行 Log")
        self.log_box.setMinimumHeight(90)
        self.log_box.setMaximumHeight(130)
        root_layout.addWidget(self.log_box, 1)

        self.setCentralWidget(central)

    def _create_learning_tab(self) -> QWidget:
        """Create the login, playback, and course status tab."""

        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setSpacing(14)
        content_layout.addWidget(self._create_login_panel(), 1)
        content_layout.addWidget(self._create_status_panel(), 2)
        scroll_area.setWidget(content)
        tab_layout.addWidget(scroll_area)
        return tab

    def _create_enrollment_tab(self) -> QWidget:
        """Create the batch enrollment page."""

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(14)

        input_group = QGroupBox("新增選課條件")
        form_layout = QGridLayout(input_group)
        form_layout.setHorizontalSpacing(14)
        form_layout.setVerticalSpacing(12)

        self.enrollment_course_input = QLineEdit()
        self.enrollment_course_input.setMinimumHeight(40)
        self.enrollment_course_input.setPlaceholderText("例如：人權、Excel、溝通技巧")

        self.enrollment_count_input = QLineEdit("1")
        self.enrollment_count_input.setMinimumHeight(40)
        self.enrollment_count_input.setPlaceholderText("堂數")

        self.enrollment_hours_min_input = QLineEdit("1")
        self.enrollment_hours_min_input.setMinimumHeight(40)
        self.enrollment_hours_min_input.setPlaceholderText("最小")
        self.enrollment_hours_max_input = QLineEdit("3")
        self.enrollment_hours_max_input.setMinimumHeight(40)
        self.enrollment_hours_max_input.setPlaceholderText("最大")

        self.enrollment_add_condition_button = QPushButton("加入清單")
        self.enrollment_add_condition_button.setMinimumHeight(38)
        self.enrollment_add_condition_button.clicked.connect(self._on_add_enrollment_condition_clicked)

        self.enrollment_remove_condition_button = QPushButton("刪除條件")
        self.enrollment_remove_condition_button.setMinimumHeight(38)
        self.enrollment_remove_condition_button.clicked.connect(self._on_remove_enrollment_condition_clicked)

        self.enrollment_search_button = QPushButton("搜尋課程")
        self.enrollment_search_button.setObjectName("PrimaryButton")
        self.enrollment_search_button.setMinimumHeight(38)
        self.enrollment_search_button.setEnabled(False)
        self.enrollment_search_button.clicked.connect(self._on_search_enrollment_clicked)

        self.enrollment_submit_button = QPushButton("報名勾選課程")
        self.enrollment_submit_button.setMinimumHeight(38)
        self.enrollment_submit_button.setEnabled(False)
        self.enrollment_submit_button.clicked.connect(self._on_enroll_selected_clicked)

        self.enrollment_condition_table = QTableWidget(0, 5)
        self.enrollment_condition_table.setObjectName("ConditionTable")
        self.enrollment_condition_table.setHorizontalHeaderLabels(("啟用", "課程", "堂數", "最小時數", "最大時數"))
        self.enrollment_condition_table.verticalHeader().setVisible(False)
        self.enrollment_condition_table.verticalHeader().setDefaultSectionSize(34)
        self.enrollment_condition_table.setAlternatingRowColors(True)
        self.enrollment_condition_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.enrollment_condition_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.enrollment_condition_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.enrollment_condition_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_condition_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.enrollment_condition_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_condition_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_condition_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_condition_table.setMinimumHeight(260)
        self.enrollment_condition_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.enrollment_result_table = QTableWidget(0, 6)
        self.enrollment_result_table.setObjectName("CourseResultBox")
        self.enrollment_result_table.setHorizontalHeaderLabels(("選取", "關鍵字", "課程名稱", "時數", "狀態", "課程ID"))
        self.enrollment_result_table.verticalHeader().setVisible(False)
        self.enrollment_result_table.verticalHeader().setDefaultSectionSize(36)
        self.enrollment_result_table.setAlternatingRowColors(True)
        self.enrollment_result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.enrollment_result_table.setMinimumHeight(260)
        self.enrollment_result_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.enrollment_result_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.enrollment_result_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.enrollment_result_table.itemChanged.connect(self._on_enrollment_result_item_changed)
        self.enrollment_result_table.itemSelectionChanged.connect(self._refresh_enrollment_submit_button)
        self.enrollment_result_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.enrollment_result_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_result_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.enrollment_result_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)

        hours_widget = QWidget()
        hours_layout = QHBoxLayout(hours_widget)
        hours_layout.setContentsMargins(0, 0, 0, 0)
        hours_layout.setSpacing(8)
        hours_layout.addWidget(self.enrollment_hours_min_input)
        hours_layout.addWidget(QLabel("到"))
        hours_layout.addWidget(self.enrollment_hours_max_input)

        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(8)
        action_grid.setVerticalSpacing(8)
        action_grid.addWidget(self.enrollment_add_condition_button, 0, 0)
        action_grid.addWidget(self.enrollment_remove_condition_button, 0, 1)
        action_grid.addWidget(self.enrollment_search_button, 1, 0)
        action_grid.addWidget(self.enrollment_submit_button, 1, 1)
        action_grid.setColumnStretch(0, 1)
        action_grid.setColumnStretch(1, 1)

        form_layout.addWidget(QLabel("課程"), 0, 0)
        form_layout.addWidget(self.enrollment_course_input, 0, 1, 1, 3)
        form_layout.addWidget(QLabel("堂數"), 1, 0)
        form_layout.addWidget(self.enrollment_count_input, 1, 1)
        form_layout.addWidget(QLabel("認證時數"), 1, 2)
        form_layout.addWidget(hours_widget, 1, 3)
        form_layout.addLayout(action_grid, 0, 4, 2, 1)
        form_layout.setRowStretch(0, 1)
        form_layout.setRowStretch(1, 1)
        form_layout.setColumnStretch(1, 2)
        form_layout.setColumnStretch(3, 1)
        form_layout.setColumnMinimumWidth(0, 52)
        form_layout.setColumnMinimumWidth(2, 78)
        form_layout.setColumnMinimumWidth(4, 300)

        self.enrollment_condition_group = QGroupBox("選課條件清單")
        condition_layout = QVBoxLayout(self.enrollment_condition_group)
        condition_layout.setContentsMargins(14, 22, 14, 14)
        condition_layout.setSpacing(10)
        condition_layout.addWidget(self.enrollment_condition_table)

        self.enrollment_result_group = QGroupBox("搜尋結果")
        self.enrollment_result_group.setVisible(False)
        result_layout = QVBoxLayout(self.enrollment_result_group)
        result_layout.setContentsMargins(14, 22, 14, 14)
        result_layout.setSpacing(10)
        result_layout.addWidget(self.enrollment_result_table)

        layout.addWidget(input_group)
        layout.addWidget(self.enrollment_condition_group, 3)
        layout.addWidget(self.enrollment_result_group, 2)
        return tab

    def _create_login_panel(self) -> QGroupBox:
        """Create the account, password, and command-button panel."""

        group = QGroupBox("登入資訊")
        layout = QVBoxLayout(group)
        layout.setSpacing(12)

        self.account_input = QLineEdit()
        self.account_input.setPlaceholderText("人事服務網帳號")
        self.account_input.setClearButtonEnabled(True)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("人事服務網密碼")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setClearButtonEnabled(True)

        self.remember_checkbox = QCheckBox("記住帳號密碼")

        self.start_browser_button = QPushButton("啟動瀏覽器")
        self.start_browser_button.setObjectName("PrimaryButton")
        self.start_browser_button.clicked.connect(self._on_start_browser_clicked)

        self.check_login_button = QPushButton("偵測登入狀態")
        self.check_login_button.setEnabled(False)
        self.check_login_button.clicked.connect(self._on_check_login_clicked)

        self.login_button = QPushButton("登入")
        self.login_button.setEnabled(False)
        self.login_button.clicked.connect(self._on_login_clicked)

        self.scan_courses_button = QPushButton("掃描課程")
        self.scan_courses_button.setEnabled(False)
        self.scan_courses_button.clicked.connect(self._on_scan_courses_clicked)

        self.scan_unanswered_assessments_button = QPushButton("逐門檢查未答測驗")
        self.scan_unanswered_assessments_button.setEnabled(False)
        self.scan_unanswered_assessments_button.clicked.connect(
            self._on_scan_unanswered_assessments_clicked
        )

        self.enter_course_button = QPushButton("進入選取課程")
        self.enter_course_button.setEnabled(False)
        self.enter_course_button.clicked.connect(self._on_enter_course_clicked)

        self.inspect_player_button = QPushButton("偵測課程頁")
        self.inspect_player_button.setEnabled(False)
        self.inspect_player_button.clicked.connect(self._on_inspect_player_clicked)

        self.playback_schedule_button = QPushButton("開始上課")
        self.playback_schedule_button.setObjectName("PrimaryButton")
        self.playback_schedule_button.setEnabled(False)
        self.playback_schedule_button.clicked.connect(self._on_playback_schedule_clicked)

        self.survey_button = QPushButton("掃描並填寫問卷")
        self.survey_button.setEnabled(False)
        self.survey_button.clicked.connect(self._on_scan_and_fill_surveys_clicked)

        self.skip_course_button = QPushButton("跳過課程")
        self.skip_course_button.setEnabled(False)
        self.skip_course_button.clicked.connect(self._on_skip_course_clicked)

        layout.addWidget(QLabel("人事服務網帳號"))
        layout.addWidget(self.account_input)
        layout.addWidget(QLabel("人事服務網密碼"))
        layout.addWidget(self.password_input)
        layout.addWidget(self.remember_checkbox)
        layout.addSpacing(8)
        button_grid = QGridLayout()
        button_grid.setHorizontalSpacing(8)
        button_grid.setVerticalSpacing(8)
        button_grid.addWidget(self.start_browser_button, 0, 0)
        button_grid.addWidget(self.check_login_button, 0, 1)
        button_grid.addWidget(self.login_button, 1, 0)
        button_grid.addWidget(self.scan_courses_button, 1, 1)
        button_grid.addWidget(self.playback_schedule_button, 2, 0)
        button_grid.addWidget(self.survey_button, 2, 1)
        button_grid.addWidget(self.scan_unanswered_assessments_button, 3, 0)
        button_grid.addWidget(self.enter_course_button, 3, 1)
        button_grid.addWidget(self.skip_course_button, 4, 0, 1, 2)
        layout.addLayout(button_grid)
        self.inspect_player_button.setVisible(False)
        layout.addStretch(1)
        return group

    def _create_status_panel(self) -> QGroupBox:
        """Create status fields and the scanned-course result box."""

        group = QGroupBox("目前狀態")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(10)

        self.login_status_value = self._create_value_label("尚未登入")
        self.url_value = self._create_value_label("-")
        self.page_value = self._create_value_label("-")
        self.status_value = self._create_value_label("瀏覽器尚未啟動")
        self.course_count_value = self._create_value_label("0")

        rows = (
            ("登入狀態", self.login_status_value),
            ("目前網址", self.url_value),
            ("目前頁面", self.page_value),
            ("目前狀態", self.status_value),
            ("目前課程數", self.course_count_value),
        )
        for row, (label_text, value_label) in enumerate(rows):
            label = QLabel(label_text)
            label.setObjectName("FieldLabel")
            layout.addWidget(label, row, 0, alignment=Qt.AlignmentFlag.AlignTop)
            layout.addWidget(value_label, row, 1)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("Separator")
        layout.addWidget(separator, len(rows), 0, 1, 2)

        self.course_result_box = QPlainTextEdit()
        self.course_result_box.setObjectName("CourseResultBox")
        self.course_result_box.setReadOnly(True)
        self.course_result_box.setPlaceholderText("掃描後會在這裡顯示課程清單")
        layout.addWidget(self.course_result_box, len(rows) + 1, 0, 1, 2)

        self.course_selector = QComboBox()
        self.course_selector.setEnabled(False)
        self.course_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.course_selector, len(rows) + 2, 0, 1, 2)

        layout.setColumnStretch(1, 1)
        return group

    def _create_value_label(self, text: str) -> QLabel:
        """Create a selectable value label for status fields."""

        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return label

    def _connect_browser_worker(self) -> None:
        """Connect GUI requests to worker slots and worker results to GUI slots."""

        self.request_start_browser.connect(self._browser_worker.start_browser)
        self.request_check_login.connect(self._browser_worker.check_login)
        self.request_login.connect(self._browser_worker.login)
        self.request_scan_courses.connect(self._browser_worker.scan_courses)
        self.request_scan_unanswered_assessments.connect(
            self._browser_worker.scan_unanswered_assessments
        )
        self.request_enter_course.connect(self._browser_worker.enter_course)
        self.request_inspect_player.connect(self._browser_worker.inspect_player)
        self.request_run_playback_schedule.connect(self._browser_worker.run_playback_schedule)
        self.request_scan_and_fill_surveys.connect(self._browser_worker.scan_and_fill_surveys)
        self.request_search_enrollment_courses.connect(self._browser_worker.search_enrollment_courses)
        self.request_enroll_selected_courses.connect(self._browser_worker.enroll_selected_courses)
        self.request_skip_course.connect(self._browser_worker.skip_current_course)
        self.request_close_browser.connect(self._browser_worker.close_browser)
        self._browser_worker.state_changed.connect(self._update_browser_state)
        self._browser_worker.login_result.connect(self._update_login_result)
        self._browser_worker.course_scan_result.connect(self._update_course_scan_result)
        self._browser_worker.unanswered_assessment_scan_result.connect(
            self._update_unanswered_assessment_scan_result
        )
        self._browser_worker.automation_result.connect(self._update_automation_result)
        self._browser_worker.survey_result.connect(self._update_survey_result)
        self._browser_worker.enrollment_search_result.connect(self._update_enrollment_search_result)
        self._browser_worker.enrollment_submit_result.connect(self._update_enrollment_submit_result)
        self._browser_worker.task_failed.connect(self._show_task_error)
        self._browser_worker.task_finished.connect(self._set_idle_buttons)

    def _start_log_timer(self) -> None:
        """Start polling the logging queue for GUI log messages."""

        if self._log_queue is None:
            return

        self._log_timer = QTimer(self)
        self._log_timer.setInterval(250)
        self._log_timer.timeout.connect(self._drain_log_queue)
        self._log_timer.start()

    @Slot()
    def _on_start_browser_clicked(self) -> None:
        """Handle the start-browser button."""

        self._set_busy_buttons()
        self.status_value.setText("正在啟動瀏覽器")
        self._append_log("正在啟動瀏覽器...")
        self.request_start_browser.emit()

    @Slot()
    def _on_check_login_clicked(self) -> None:
        """Queue checking the current session login state."""

        self._set_busy_buttons()
        self.login_status_value.setText("偵測中")
        self.status_value.setText("正在偵測登入狀態")
        self._append_log("正在偵測登入狀態...")
        self.request_check_login.emit()

    @Slot()
    def _on_login_clicked(self) -> None:
        """Validate the form and queue the login flow."""

        form_data = LoginFormData(
            account=self.account_input.text().strip(),
            password=self.password_input.text(),
            remember_credentials=self.remember_checkbox.isChecked(),
        )
        if not form_data.account or not form_data.password:
            QMessageBox.warning(self, "登入資訊不足", "請先輸入帳號與密碼。")
            return

        self._set_busy_buttons()
        self.login_status_value.setText("登入中")
        self.status_value.setText("正在執行登入流程")
        self._append_log("正在執行登入流程...")
        self.request_login.emit(form_data)

    @Slot()
    def _on_scan_courses_clicked(self) -> None:
        """Queue read-only course scanning."""

        self._set_busy_buttons()
        self.course_result_box.clear()
        self.course_selector.clear()
        self.course_selector.setEnabled(False)
        self.enter_course_button.setEnabled(False)
        self.course_count_value.setText("掃描中")
        self.status_value.setText("正在掃描課程")
        self._append_log("正在掃描未完成課程...")
        self.request_scan_courses.emit()

    @Slot()
    def _on_scan_unanswered_assessments_clicked(self) -> None:
        """Queue read-only scanning for unanswered assessment metadata."""

        self._set_busy_buttons()
        self.course_result_box.clear()
        self.course_selector.clear()
        self.course_selector.setEnabled(False)
        self.enter_course_button.setEnabled(False)
        self.course_count_value.setText("掃描中")
        self.status_value.setText("正在掃描未答測驗")
        self._append_log("正在掃描課程清單中的未答測驗狀態...")
        self.request_scan_unanswered_assessments.emit()

    @Slot()
    def _on_enter_course_clicked(self) -> None:
        """Queue entering the course selected in the dropdown."""

        index = self.course_selector.currentIndex()
        if index < 0 or index >= len(self._courses):
            QMessageBox.warning(self, "尚未選擇課程", "請先掃描課程並選擇一門課。")
            return

        course = self._courses[index]
        self._set_busy_buttons()
        self.status_value.setText(f"正在進入課程：{course.title}")
        self._append_log(f"正在進入課程：{course.title}")
        self.request_enter_course.emit(course)

    @Slot()
    def _on_inspect_player_clicked(self) -> None:
        """Queue a read-only inspection of the current course page."""

        self._set_busy_buttons()
        self.status_value.setText("正在偵測課程頁")
        self._append_log("正在偵測課程頁...")
        self.request_inspect_player.emit()

    @Slot()
    def _on_playback_schedule_clicked(self) -> None:
        """Queue configured course playback."""

        self._set_busy_buttons()
        self.status_value.setText("正在依設定檔執行排程播放")
        self._append_log("正在依 resources/course_schedule.json 執行排程播放...")
        self.skip_course_button.setEnabled(True)
        self.request_run_playback_schedule.emit()

    @Slot()
    def _on_scan_and_fill_surveys_clicked(self) -> None:
        """Queue scanning and fixed-answer survey submission."""

        self._set_busy_buttons()
        self.course_result_box.clear()
        self.status_value.setText("正在掃描可填寫問卷")
        self._append_log("正在掃描已達閱讀時數且問卷未填的課程...")
        self.request_scan_and_fill_surveys.emit()

    @Slot()
    def _on_skip_course_clicked(self) -> None:
        """Ask the scheduler to skip the current course."""

        self.status_value.setText("已送出跳過課程指令")
        self._append_log("已送出跳過課程指令，系統會切換到下一門課。")
        self.skip_course_button.setEnabled(False)
        self.request_skip_course.emit()

    @Slot()
    def _on_add_enrollment_condition_clicked(self) -> None:
        """Add the current quick-search fields into the condition list."""

        keyword = self.enrollment_course_input.text().strip()
        if not keyword:
            QMessageBox.warning(self, "選課資料不足", "請先輸入課程關鍵字。")
            return

        count = self.enrollment_count_input.text().strip() or "1"
        hours_minimum = self.enrollment_hours_min_input.text().strip() or "1"
        hours_maximum = self.enrollment_hours_max_input.text().strip() or "3"
        row = self.enrollment_condition_table.rowCount()
        self.enrollment_condition_table.insertRow(row)

        check_item = QTableWidgetItem("")
        check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check_item.setCheckState(Qt.CheckState.Checked)
        self.enrollment_condition_table.setItem(row, 0, check_item)
        self.enrollment_condition_table.setItem(row, 1, self._readonly_table_item(keyword))
        self.enrollment_condition_table.setItem(row, 2, self._readonly_table_item(count))
        self.enrollment_condition_table.setItem(row, 3, self._readonly_table_item(hours_minimum))
        self.enrollment_condition_table.setItem(row, 4, self._readonly_table_item(hours_maximum))
        self.enrollment_condition_group.setVisible(True)
        self.enrollment_result_group.setVisible(False)
        self.enrollment_course_input.clear()
        self.enrollment_course_input.setFocus()
        self._append_log(f"已加入選課條件：{keyword}，{count} 堂。")

    @Slot()
    def _on_remove_enrollment_condition_clicked(self) -> None:
        """Remove selected rows from the enrollment condition list."""

        selected_rows = sorted(
            {index.row() for index in self.enrollment_condition_table.selectedIndexes()},
            reverse=True,
        )
        if not selected_rows:
            QMessageBox.information(self, "尚未選擇條件", "請先選取要刪除的條件列。")
            return
        for row in selected_rows:
            self.enrollment_condition_table.removeRow(row)
        self._append_log(f"已刪除 {len(selected_rows)} 筆選課條件。")

    @Slot()
    def _on_search_enrollment_clicked(self) -> None:
        """Search registration-center courses from GUI settings."""

        if self.enrollment_condition_table.rowCount() == 0 and self.enrollment_course_input.text().strip():
            self._on_add_enrollment_condition_clicked()

        condition_lines = self._enrollment_condition_lines()
        form_data = EnrollmentFormData(
            keywords="\n".join(condition_lines),
            hours_minimum="",
            hours_maximum="",
        )
        if not form_data.keywords:
            QMessageBox.warning(self, "選課資料不足", "請先加入至少一筆啟用中的選課條件。")
            return

        self._set_busy_buttons()
        self._enrollment_courses.clear()
        self.enrollment_result_table.setRowCount(0)
        self.enrollment_submit_button.setEnabled(False)
        self.enrollment_condition_group.setVisible(False)
        self.enrollment_result_group.setVisible(True)
        self.status_value.setText("正在搜尋選課中心")
        self._append_log("正在選課中心搜尋可報名課程...")
        self.request_search_enrollment_courses.emit(form_data)

    def _enrollment_condition_lines(self) -> list[str]:
        """Convert enabled condition rows to keyword,count,min,max lines."""

        lines: list[str] = []
        for row in range(self.enrollment_condition_table.rowCount()):
            check_item = self.enrollment_condition_table.item(row, 0)
            if check_item is None or check_item.checkState() != Qt.CheckState.Checked:
                continue
            keyword = self._table_text(self.enrollment_condition_table, row, 1)
            count = self._table_text(self.enrollment_condition_table, row, 2) or "1"
            hours_minimum = self._table_text(self.enrollment_condition_table, row, 3) or "1"
            hours_maximum = self._table_text(self.enrollment_condition_table, row, 4) or "3"
            if keyword:
                lines.append(f"{keyword},{count},{hours_minimum},{hours_maximum}")
        return lines

    def _table_text(self, table: QTableWidget, row: int, column: int) -> str:
        """Read trimmed table text safely."""

        item = table.item(row, column)
        return item.text().strip() if item is not None else ""

    def _readonly_table_item(self, text: str) -> QTableWidgetItem:
        """Create a selectable table item that cannot be edited accidentally."""

        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        return item

    @Slot()
    def _on_enroll_selected_clicked(self) -> None:
        """Enroll checked courses from the registration table."""

        selected = self._checked_enrollment_courses()
        if not selected:
            QMessageBox.warning(self, "尚未選擇課程", "請先勾選至少一門要報名的課程。")
            return
        if QMessageBox.question(self, "確認報名", f"確定要報名 {len(selected)} 門課程？") != QMessageBox.StandardButton.Yes:
            return

        self._set_busy_buttons()
        self.status_value.setText("正在報名勾選課程")
        self._append_log(f"正在報名 {len(selected)} 門勾選課程...")
        self.request_enroll_selected_courses.emit(selected)

    @Slot(object)
    def _on_enrollment_result_item_changed(self, item: QTableWidgetItem) -> None:
        """Refresh the enroll button whenever a result checkbox changes."""

        if item.column() == 0:
            self._refresh_enrollment_submit_button()

    @Slot(object)
    def _update_browser_state(self, state: BrowserState) -> None:
        """Refresh browser URL, page title, and status fields."""

        self.url_value.setText(state.current_url or "-")
        self.page_value.setText(state.page_title or "-")
        self.status_value.setText(state.message)
        self._browser_running = state.status != BrowserStatus.STOPPED
        if state.status == BrowserStatus.STOPPED:
            self._reset_session_ui()
        self._append_log(state.message)

    @Slot(object)
    def _update_login_result(self, result: LoginResult) -> None:
        """Refresh GUI fields after successful login."""

        self._is_logged_in = True
        self.login_status_value.setText("已登入")
        self.url_value.setText(result.current_url or "-")
        self.page_value.setText(result.page_title or "-")
        self.status_value.setText(result.message)
        self._append_log(result.message)

    @Slot(object)
    def _update_course_scan_result(self, courses: list[CourseInfo]) -> None:
        """Display scanned unfinished courses in the status panel and log."""

        self._courses = list(courses)
        self.course_selector.clear()
        self.course_count_value.setText(str(len(courses)))
        if not courses:
            self.course_result_box.setPlainText("目前沒有未完成課程。")
            self.course_selector.setEnabled(False)
            self.enter_course_button.setEnabled(False)
            self._append_log("掃描完成：目前沒有未完成課程。")
            return

        lines: list[str] = []
        for index, course in enumerate(courses, start=1):
            details = " | ".join(
                value
                for value in (
                    course.course_type,
                    f"認證時數 {course.certification_hours}" if course.certification_hours else "",
                    f"測驗 {course.exam_score}" if course.exam_score else "",
                    f"問卷 {course.survey_status}" if course.survey_status else "",
                )
                if value
            )
            line = f"{index}. {course.title}"
            if details:
                line = f"{line}\n   {details}"
            if course.course_url:
                line = f"{line}\n   {course.course_url}"
            lines.append(line)
            self.course_selector.addItem(f"{index}. {course.title}")

        self.course_selector.setEnabled(True)
        self.enter_course_button.setEnabled(True)
        self.course_result_box.setPlainText("\n\n".join(lines))
        self._append_log(f"掃描完成：找到 {len(courses)} 門未完成課程。")

    @Slot(object)
    def _update_unanswered_assessment_scan_result(self, courses: list[CourseInfo]) -> None:
        """Display courses explicitly marked as having unanswered assessments."""

        self._courses = list(courses)
        self.course_selector.clear()
        self.course_count_value.setText(str(len(courses)))
        if not courses:
            self.course_result_box.setPlainText("目前沒有明確標示為未答測驗的課程。")
            self.course_selector.setEnabled(False)
            self.enter_course_button.setEnabled(False)
            self._append_log("未答測驗掃描完成：沒有符合的課程。")
            return

        lines: list[str] = []
        for index, course in enumerate(courses, start=1):
            status = course.assessment_status or course.exam_score or "未提供"
            access = "可上課" if course.can_attend else "無法上課"
            note = f"\n   {course.inspection_note}" if course.inspection_note else ""
            lines.append(f"{index}. {course.title}\n   {access} | 測驗狀態：{status}{note}")
            self.course_selector.addItem(f"{index}. {course.title}")

        self.course_selector.setEnabled(True)
        self.enter_course_button.setEnabled(True)
        self.course_result_box.setPlainText("\n\n".join(lines))
        self._append_log(f"未答測驗掃描完成：找到 {len(courses)} 門課程。")

    @Slot(object)
    def _update_enrollment_search_result(self, courses: list[EnrollmentCourse]) -> None:
        """Display registration-center search results in the enrollment table."""

        self._enrollment_courses = list(courses)
        self.enrollment_result_group.setVisible(True)
        self.enrollment_result_table.blockSignals(True)
        self.enrollment_result_table.setRowCount(len(courses))
        for row, course in enumerate(courses):
            check_item = QTableWidgetItem("")
            check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check_item.setCheckState(Qt.CheckState.Checked)
            self.enrollment_result_table.setItem(row, 0, check_item)
            self.enrollment_result_table.setItem(row, 1, self._readonly_table_item(course.keyword))
            self.enrollment_result_table.setItem(row, 2, self._readonly_table_item(course.title))
            self.enrollment_result_table.setItem(row, 3, self._readonly_table_item(course.hours or "-"))
            self.enrollment_result_table.setItem(row, 4, self._readonly_table_item(course.status))
            self.enrollment_result_table.setItem(row, 5, self._readonly_table_item(course.course_id))
        self.enrollment_result_table.blockSignals(False)

        self._refresh_enrollment_submit_button()
        self._append_log(f"選課搜尋完成：找到 {len(courses)} 門可報名課程。")

    @Slot(object)
    def _update_enrollment_submit_result(self, results: list[EnrollmentResult]) -> None:
        """Display enrollment results in the table and log."""

        result_by_id = {result.course.course_id: result for result in results}
        for row, course in enumerate(self._enrollment_courses):
            result = result_by_id.get(course.course_id)
            if result is None:
                continue
            status_item = self.enrollment_result_table.item(row, 4)
            if status_item is None:
                status_item = QTableWidgetItem()
                self.enrollment_result_table.setItem(row, 4, status_item)
            status_item.setText("已報名" if result.success else f"失敗：{result.message}")
            self._append_log(f"{course.title}：{status_item.text()}")
        self._refresh_enrollment_submit_button()

    @Slot(object)
    def _update_survey_result(self, result: SurveyResult) -> None:
        """Display questionnaire automation progress and final summary."""

        self.status_value.setText(result.message)
        if result.status == SurveyStatus.FINISHED:
            self.course_result_box.appendPlainText(
                "\n"
                f"問卷摘要：掃描 {result.scanned_count} 門，"
                f"可填 {result.eligible_count} 門，"
                f"已送出 {result.filled_count} 門，"
                f"略過 {result.skipped_count} 門，"
                f"失敗 {result.failed_count} 門。"
            )
        else:
            self.course_result_box.appendPlainText(result.message)
        self._append_log(result.message)

    def _checked_enrollment_courses(self) -> list[EnrollmentCourse]:
        """Return courses checked in the enrollment table."""

        selected: list[EnrollmentCourse] = []
        for row, course in enumerate(self._enrollment_courses):
            item = self.enrollment_result_table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                selected.append(course)
        return selected

    def _refresh_enrollment_submit_button(self) -> None:
        """Enable enrollment only when logged in and at least one result is checked."""

        self.enrollment_submit_button.setEnabled(self._is_logged_in and bool(self._checked_enrollment_courses()))

    @Slot(object)
    def _update_automation_result(self, result: AutomationResult) -> None:
        """Display the result of entering a course."""

        self.url_value.setText(result.current_url or "-")
        self.page_value.setText(result.page_title or "-")
        self.status_value.setText(result.message)

        frame_count = len(result.frame_urls)
        lines = [
            f"已進入課程：{result.course_title}",
            f"目前章節：{result.chapter_title}" if result.chapter_title else "",
            result.message,
            f"目前網址：{result.current_url}",
            f"偵測到 iframe：{frame_count} 個",
            f"影片元素：{result.video_count} 個",
            f"播放器 iframe：{result.player_frame_url}" if result.player_frame_url else "",
        ]
        self.course_result_box.setPlainText("\n".join(line for line in lines if line))
        self._append_log(f"{result.message} 偵測到 {frame_count} 個 iframe、{result.video_count} 個影片元素。")

        active_statuses = {
            AutomationStatus.ENTERED_COURSE,
            AutomationStatus.CHAPTER_SELECTED,
            AutomationStatus.PLAYBACK_STARTED,
            AutomationStatus.PLAYBACK_WAITING,
        }
        self.skip_course_button.setEnabled(result.status in active_statuses and not self.playback_schedule_button.isEnabled())

    @Slot(str)
    def _show_task_error(self, message: str) -> None:
        """Show recoverable worker errors without closing the application."""

        self.status_value.setText(message)
        self._append_log(f"錯誤：{message}")
        QMessageBox.warning(self, "執行失敗", message)

    @Slot()
    def _set_idle_buttons(self) -> None:
        """Re-enable buttons after a background task finishes."""

        self.start_browser_button.setEnabled(True)
        self.check_login_button.setEnabled(self._browser_running)
        self.login_button.setEnabled(self._browser_running)
        self.scan_courses_button.setEnabled(self._is_logged_in)
        self.scan_unanswered_assessments_button.setEnabled(self._is_logged_in)
        self.enter_course_button.setEnabled(bool(self._courses))
        self.inspect_player_button.setEnabled(self._is_logged_in)
        self.playback_schedule_button.setEnabled(self._is_logged_in)
        self.survey_button.setEnabled(self._is_logged_in)
        self.enrollment_add_condition_button.setEnabled(True)
        self.enrollment_remove_condition_button.setEnabled(True)
        self.enrollment_search_button.setEnabled(self._is_logged_in)
        self._refresh_enrollment_submit_button()
        self.skip_course_button.setEnabled(False)

    def _set_busy_buttons(self) -> None:
        """Disable command buttons while one worker task is running."""

        self.start_browser_button.setEnabled(False)
        self.check_login_button.setEnabled(False)
        self.login_button.setEnabled(False)
        self.scan_courses_button.setEnabled(False)
        self.scan_unanswered_assessments_button.setEnabled(False)
        self.enter_course_button.setEnabled(False)
        self.inspect_player_button.setEnabled(False)
        self.playback_schedule_button.setEnabled(False)
        self.survey_button.setEnabled(False)
        self.enrollment_add_condition_button.setEnabled(False)
        self.enrollment_remove_condition_button.setEnabled(False)
        self.enrollment_search_button.setEnabled(False)
        self.enrollment_submit_button.setEnabled(False)
        self.skip_course_button.setEnabled(False)

    def _reset_session_ui(self) -> None:
        """Reset login and course UI after the browser is closed."""

        self._is_logged_in = False
        self._browser_running = False
        self._courses.clear()
        self.login_status_value.setText("尚未登入")
        self.course_count_value.setText("0")
        self.course_selector.clear()
        self.course_selector.setEnabled(False)
        self.course_result_box.clear()
        self.check_login_button.setEnabled(False)
        self.login_button.setEnabled(False)
        self.scan_courses_button.setEnabled(False)
        self.scan_unanswered_assessments_button.setEnabled(False)
        self.enter_course_button.setEnabled(False)
        self.inspect_player_button.setEnabled(False)
        self.playback_schedule_button.setEnabled(False)
        self.survey_button.setEnabled(False)
        self.enrollment_add_condition_button.setEnabled(True)
        self.enrollment_remove_condition_button.setEnabled(True)
        self.enrollment_search_button.setEnabled(False)
        self.enrollment_submit_button.setEnabled(False)
        self.skip_course_button.setEnabled(False)

    @Slot()
    def _drain_log_queue(self) -> None:
        """Move queued logger messages into the on-screen log box."""

        if self._log_queue is None:
            return

        while True:
            try:
                message = self._log_queue.get_nowait()
            except Empty:
                break
            self._append_log(message.message)

    def _append_log(self, message: str) -> None:
        """Append one line to the GUI log and keep the latest line visible."""

        self.log_box.appendPlainText(message)
        scrollbar = self.log_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


def _dark_stylesheet() -> str:
    """Return a compact VSCode-like dark stylesheet."""

    return """
    QMainWindow, QWidget {
        background: #1e1e1e;
        color: #d4d4d4;
        font-family: "Microsoft JhengHei UI", "Segoe UI", sans-serif;
        font-size: 14px;
    }
    #TitleLabel {
        color: #ffffff;
        font-size: 22px;
        font-weight: 700;
    }
    QGroupBox {
        border: 1px solid #3c3c3c;
        border-radius: 6px;
        margin-top: 12px;
        padding: 14px;
        background: #252526;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 6px;
        color: #cccccc;
    }
    QTabWidget::pane {
        border: 1px solid #3c3c3c;
        background: #202124;
        top: -1px;
    }
    QTabBar::tab {
        background: #2b2d31;
        color: #b8c0cc;
        border: 1px solid #3c3c3c;
        border-bottom: none;
        padding: 9px 18px;
        margin-right: 4px;
        min-width: 96px;
    }
    QTabBar::tab:selected {
        background: #0e639c;
        color: #ffffff;
        border-color: #1788d3;
        font-weight: 700;
    }
    QTabBar::tab:hover:!selected {
        background: #373a40;
        color: #ffffff;
    }
    QLabel#FieldLabel, QLabel#MutedLabel {
        color: #9cdcfe;
    }
    QLabel#MutedLabel {
        color: #9e9e9e;
    }
    QLineEdit, QPlainTextEdit, QTableWidget {
        background: #1b1b1c;
        border: 1px solid #3c3c3c;
        border-radius: 5px;
        color: #eeeeee;
        padding: 9px 10px;
        selection-background-color: #264f78;
    }
    QLineEdit {
        min-height: 22px;
        font-size: 15px;
    }
    QLineEdit::placeholder, QPlainTextEdit::placeholder {
        color: #8f98a3;
    }
    QLineEdit:focus, QPlainTextEdit:focus {
        border: 1px solid #1788d3;
        background: #202124;
    }
    QPushButton {
        background: #3a3d41;
        border: 1px solid #4d4d4d;
        border-radius: 5px;
        color: #ffffff;
        padding: 9px 12px;
    }
    QPushButton:hover {
        background: #45494e;
    }
    QPushButton:disabled {
        background: #2d2d30;
        color: #777777;
    }
    QPushButton#PrimaryButton {
        background: #0e639c;
        border-color: #1177bb;
    }
    QPushButton#PrimaryButton:hover {
        background: #1177bb;
    }
    QCheckBox {
        spacing: 8px;
    }
    QHeaderView::section {
        background: #30343a;
        color: #f2f2f2;
        border: 1px solid #454a52;
        padding: 7px;
        font-weight: 700;
    }
    QTableWidget {
        gridline-color: #34383f;
        alternate-background-color: #22252b;
    }
    QTableWidget::item {
        padding: 6px;
    }
    QTableWidget::item:selected {
        background: #264f78;
        color: #ffffff;
    }
    QFrame#Separator {
        color: #3c3c3c;
    }
    #LogBox, #CourseResultBox, #ConditionTable {
        min-height: 150px;
        font-family: Consolas, "Microsoft JhengHei UI", monospace;
        font-size: 12px;
    }
    #ConditionTable {
        min-height: 130px;
    }
    QScrollBar:vertical {
        background: #1b1b1c;
        width: 14px;
        margin: 0;
    }
    QScrollBar::handle:vertical {
        background: #555b63;
        border-radius: 6px;
        min-height: 28px;
    }
    QScrollBar::handle:vertical:hover {
        background: #6b737d;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
    }
    QScrollBar:horizontal {
        background: #1b1b1c;
        height: 14px;
        margin: 0;
    }
    QScrollBar::handle:horizontal {
        background: #555b63;
        border-radius: 6px;
        min-width: 28px;
    }
    QScrollBar::handle:horizontal:hover {
        background: #6b737d;
    }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
        width: 0;
    }
    """
