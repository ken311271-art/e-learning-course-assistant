"""Survey automation for courses that already meet reading-time rules.

This module is intentionally separate from login, course scanning, and playback
automation.  It only decides whether a course survey is ready, opens the survey
page, applies the fixed answer policy, and submits the form.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from core.automation import CourseAutomation
from core.config import AppConfig
from core.course import CourseInfo
from core.logger import get_logger
from core.selectors import SelectorManager

if TYPE_CHECKING:
    from playwright.sync_api import Dialog, Frame, Page
else:
    Dialog = Any
    Frame = Any
    Page = Any


class SurveyStatus(str, Enum):
    """Normalized status for one course survey action."""

    SCANNED = "scanned"
    SKIPPED = "skipped"
    FILLED = "filled"
    FAILED = "failed"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class SurveyResult:
    """Result emitted to the GUI while surveys are processed."""

    status: SurveyStatus
    message: str
    course_title: str = ""
    scanned_count: int = 0
    eligible_count: int = 0
    filled_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0


class SurveyError(RuntimeError):
    """Raised when survey automation cannot continue safely."""


class SurveyAutomation:
    """Fill fixed course-survey answers for eligible courses."""

    UNFILLED_MARKERS = ("未填", "未填寫", "尚未填")
    FILLED_MARKERS = ("已填", "已填寫", "已完成")

    def __init__(
        self,
        page: Page,
        config: AppConfig,
        selectors: SelectorManager | None = None,
    ) -> None:
        self._page = page
        self._config = config
        self._selectors = selectors or SelectorManager()
        self._course_automation = CourseAutomation(page=page, config=config, selectors=self._selectors)
        self._logger = get_logger(__name__)

    def process_courses(
        self,
        courses: list[CourseInfo],
        emit_result: Callable[[SurveyResult], None] | None = None,
    ) -> list[SurveyResult]:
        """Scan courses, fill eligible surveys, and return a compact summary."""

        if not courses:
            raise SurveyError("目前沒有可掃描的課程，請先確認已登入。")

        results: list[SurveyResult] = []
        eligible_count = 0
        filled_count = 0
        skipped_count = 0
        failed_count = 0

        for course in courses:
            result = self._process_one_course(course)
            results.append(result)
            self._emit(emit_result, result)

            if result.status == SurveyStatus.SCANNED:
                eligible_count += 1
            elif result.status == SurveyStatus.FILLED:
                eligible_count += 1
                filled_count += 1
            elif result.status == SurveyStatus.SKIPPED:
                skipped_count += 1
            elif result.status == SurveyStatus.FAILED:
                failed_count += 1

        summary = SurveyResult(
            status=SurveyStatus.FINISHED,
            message=(
                f"問卷處理完成：掃描 {len(courses)} 門，"
                f"可填 {eligible_count} 門，已送出 {filled_count} 門，"
                f"略過 {skipped_count} 門，失敗 {failed_count} 門。"
            ),
            scanned_count=len(courses),
            eligible_count=eligible_count,
            filled_count=filled_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
        )
        results.append(summary)
        self._emit(emit_result, summary)
        return results

    def _process_one_course(self, course: CourseInfo) -> SurveyResult:
        """Process a single course and convert recoverable errors into results."""

        try:
            if self._survey_is_filled(course.survey_status):
                return SurveyResult(
                    status=SurveyStatus.SKIPPED,
                    course_title=course.title,
                    message=f"{course.title}：問卷已填，略過。",
                )

            study_time = self._course_automation.read_course_study_time(course)
            current_survey_status = study_time.survey_status or course.survey_status
            if self._survey_is_filled(current_survey_status):
                return SurveyResult(
                    status=SurveyStatus.SKIPPED,
                    course_title=course.title,
                    message=f"{course.title}：重新檢查時問卷已填，略過。",
                )
            if study_time.required_seconds <= 0:
                return SurveyResult(
                    status=SurveyStatus.SKIPPED,
                    course_title=course.title,
                    message=f"{course.title}：找不到閱讀時數門檻，略過。",
                )
            if study_time.studied_seconds < study_time.required_seconds:
                return SurveyResult(
                    status=SurveyStatus.SKIPPED,
                    course_title=course.title,
                    message=(
                        f"{course.title}：閱讀時數未達標 "
                        f"({self._format_seconds(study_time.studied_seconds)} / "
                        f"{self._format_seconds(study_time.required_seconds)})，略過。"
                    ),
                )

            if not self._survey_is_unfilled(current_survey_status):
                return SurveyResult(
                    status=SurveyStatus.SKIPPED,
                    course_title=course.title,
                    message=f"{course.title}：問卷狀態不是明確的未填，略過。",
                )

            self._logger.info("準備填寫問卷：%s", course.title)

            self._course_automation.enter_course(course)
            self._sync_to_active_page()
            self._open_questionnaire_list()
            self._open_fill_form()
            self._fill_and_submit_form()
            return SurveyResult(
                status=SurveyStatus.FILLED,
                course_title=course.title,
                message=f"{course.title}：問卷已自動填寫並送出。",
            )
        except Exception as exc:
            self._logger.exception("問卷處理失敗：%s", course.title)
            return SurveyResult(
                status=SurveyStatus.FAILED,
                course_title=course.title,
                message=f"{course.title}：問卷處理失敗：{exc}",
            )

    def _sync_to_active_page(self) -> None:
        """Keep this module pointed at the newest page after course entry."""

        pages = self._page.context.pages
        if pages:
            self._page = pages[-1]
            self._course_automation = CourseAutomation(
                page=self._page,
                config=self._config,
                selectors=self._selectors,
            )

    def _open_questionnaire_list(self) -> None:
        """Open the questionnaire list inside the course player's s_main frame."""

        selector = self._selectors.get("survey.entry_link").value
        clicked = False
        for frame in self._page.frames:
            try:
                locator = frame.locator(selector).first
                if locator.count() == 0:
                    continue
                locator.click(timeout=10_000)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            raise SurveyError("找不到課程內的「問卷/評價」入口。")

        self._wait_for_frame(
            lambda frame: "questionnaire_list.php" in frame.url
            or self._frame_contains_text(frame, "填寫問卷"),
            "問卷列表頁",
        )

    def _open_fill_form(self) -> None:
        """Click the fill-survey entry in the questionnaire list."""

        clicked = False
        for frame in self._page.frames:
            try:
                clicked = bool(
                    frame.evaluate(
                        """
                        async () => {
                            const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                            const nodes = Array.from(document.querySelectorAll("a, button, input, div, span"))
                                .filter((node) => clean(node.innerText || node.value || node.textContent).includes("填寫問卷"))
                                .map((node) => ({
                                    node,
                                    clickable: node.closest("[onclick], a, button, input") ||
                                        node.querySelector("[onclick], a, button, input") ||
                                        null,
                                }))
                                .filter((item) => item.clickable)
                                .sort((left, right) => {
                                    const leftOwn = left.node.getAttribute("onclick") ? 0 : 1;
                                    const rightOwn = right.node.getAttribute("onclick") ? 0 : 1;
                                    return leftOwn - rightOwn;
                                });
                            for (const item of nodes) {
                                const clickable = item.clickable;
                                const onclick = clickable.getAttribute("onclick") || "";
                                if (clickable.scrollIntoView) {
                                    clickable.scrollIntoView({block: "center", inline: "center"});
                                }
                                clickable.dispatchEvent(new MouseEvent("mouseover", {bubbles: true}));
                                clickable.dispatchEvent(new MouseEvent("mousedown", {bubbles: true}));
                                clickable.dispatchEvent(new MouseEvent("mouseup", {bubbles: true}));
                                clickable.click();

                                const match = onclick.match(/togo\\(\\s*['"]([^'"]+)['"]\\s*,\\s*(true|false)/);
                                if (match && typeof window.togo === "function") {
                                    window.togo(match[1], match[2] === "true", clickable);
                                }
                                await new Promise((resolve) => window.setTimeout(resolve, 300));
                                return true;
                            }
                            return false;
                        }
                        """
                    )
                )
                if clicked:
                    break
            except Exception:
                continue

        if not clicked:
            raise SurveyError("找不到「填寫問卷」按鈕，可能已填寫或尚未開放。")

        self._page.wait_for_timeout(500)
        self._sync_to_active_page()
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        self._wait_for_frame(
            lambda frame: self._frame_has_survey_form(frame),
            "問卷作答頁",
        )

    def _fill_and_submit_form(self) -> None:
        """Apply fixed answers and submit, accepting the two confirm dialogs."""

        form_frame = self._find_frame(self._frame_has_survey_form)
        if form_frame is None:
            raise SurveyError("找不到問卷作答表單。")

        dialog_handler = self._accept_dialog
        self._page.on("dialog", dialog_handler)
        try:
            filled = bool(
                form_frame.evaluate(
                    """
                    () => {
                        const mark = (element) => {
                            element.checked = true;
                            element.dispatchEvent(new Event("input", {bubbles: true}));
                            element.dispatchEvent(new Event("change", {bubbles: true}));
                        };

                        let changed = 0;
                        for (const checkbox of document.querySelectorAll("input[type='checkbox'][value='6']")) {
                            mark(checkbox);
                            changed += 1;
                        }

                        const pickedRadioNames = new Set();
                        for (const radio of document.querySelectorAll("input[type='radio'][value='5']")) {
                            const key = radio.name || radio.id || radio.value;
                            if (pickedRadioNames.has(key)) {
                                continue;
                            }
                            pickedRadioNames.add(key);
                            mark(radio);
                            changed += 1;
                        }

                        for (const textarea of document.querySelectorAll("textarea")) {
                            textarea.value = "";
                            textarea.dispatchEvent(new Event("input", {bubbles: true}));
                            textarea.dispatchEvent(new Event("change", {bubbles: true}));
                        }

                        window.__hrdOriginalConfirm = window.confirm;
                        window.confirm = () => true;
                        return changed > 0;
                    }
                    """
                )
            )
            if not filled:
                raise SurveyError("問卷表單內找不到可填寫的固定選項。")

            submit_selector = self._selectors.get("survey.submit_button").value
            submit = form_frame.locator(submit_selector).first
            submit.click(timeout=10_000)
            self._wait_for_submit_result()
            self._handle_post_submit_flow()
        finally:
            try:
                form_frame.evaluate(
                    """
                    () => {
                        if (window.__hrdOriginalConfirm) {
                            window.confirm = window.__hrdOriginalConfirm;
                            delete window.__hrdOriginalConfirm;
                        }
                    }
                    """
                )
            except Exception:
                pass
            try:
                self._page.remove_listener("dialog", dialog_handler)
            except Exception:
                pass

    def _wait_for_submit_result(self) -> None:
        """Wait for the form submit request to settle without assuming one URL."""

        try:
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        self._wait_for_frame(
            lambda frame: self._frame_contains_text(frame, "已填")
            or self._frame_contains_text(frame, "完成")
            or self._frame_contains_text(frame, "成功")
            or "questionnaire_list.php" in frame.url,
            "問卷送出結果",
            timeout_ms=20_000,
        )

    def _handle_post_submit_flow(self) -> None:
        """Handle the questionnaire page shown after a successful submit."""

        self._sync_to_active_page()
        self._click_optional_rating_button()
        self._return_to_learning_dashboard()

    def _click_optional_rating_button(self) -> None:
        """Click the post-survey rating button when the platform shows it."""

        rating_input_selector = self._selectors.get("survey.rating_input").value
        rating_button_selector = self._selectors.get("survey.rating_button").value
        for frame in self._page.frames:
            try:
                clicked = bool(
                    frame.evaluate(
                        """
                        async ([ratingInputSelector, ratingButtonSelector]) => {
                            const bodyText = document.body ? document.body.innerText : "";
                            if (!bodyText.includes("請您為課程評價")) {
                                return false;
                            }
                            const ratingInput = document.querySelector(ratingInputSelector);
                            if (ratingInput) {
                                ratingInput.value = "1";
                                ratingInput.dispatchEvent(new Event("input", {bubbles: true}));
                                ratingInput.dispatchEvent(new Event("change", {bubbles: true}));
                            }
                            const button = document.querySelector(ratingButtonSelector);
                            if (!button) {
                                return false;
                            }
                            if (button.scrollIntoView) {
                                button.scrollIntoView({block: "center", inline: "center"});
                            }
                            button.dispatchEvent(new MouseEvent("mouseover", {bubbles: true}));
                            button.dispatchEvent(new MouseEvent("mousedown", {bubbles: true}));
                            button.dispatchEvent(new MouseEvent("mouseup", {bubbles: true}));
                            button.click();
                            await new Promise((resolve) => window.setTimeout(resolve, 500));
                            return true;
                        }
                        """,
                        [rating_input_selector, rating_button_selector],
                    )
                )
                if clicked:
                    self._logger.info("已點擊問卷送出後的課程評價按鈕。")
                    return
            except Exception:
                continue

    def _return_to_learning_dashboard(self) -> None:
        """Return to the personal course dashboard after survey submission."""

        dashboard_url = self._config.base_url.replace("index.php", "user/learn_dashboard.php?tab=1")
        self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30_000)
        try:
            self._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

    def _find_frame(self, predicate: Callable[[Frame], bool]) -> Frame | None:
        """Return the first frame matching a predicate."""

        for frame in self._page.frames:
            try:
                if predicate(frame):
                    return frame
            except Exception:
                continue
        return None

    def _wait_for_frame(
        self,
        predicate: Callable[[Frame], bool],
        label: str,
        timeout_ms: int = 20_000,
    ) -> None:
        """Wait until any current frame matches a condition."""

        deadline = self._page.evaluate("() => Date.now()") + timeout_ms
        while self._page.evaluate("() => Date.now()") < deadline:
            if self._find_frame(predicate) is not None:
                return
            self._page.wait_for_timeout(250)
        raise SurveyError(f"等待{label}逾時。")

    @staticmethod
    def _frame_contains_text(frame: Frame, text: str) -> bool:
        """Return whether a frame body contains the expected text."""

        return bool(
            frame.evaluate(
                """
                (expected) => {
                    const bodyText = document.body ? document.body.innerText : "";
                    return bodyText.includes(expected);
                }
                """,
                text,
            )
        )

    @staticmethod
    def _frame_has_survey_form(frame: Frame) -> bool:
        """Return True when a frame looks like the questionnaire answer form."""

        return bool(
            frame.evaluate(
                """
                () => Boolean(
                    document.querySelector("input[type='submit'][value*='確定繳交'], input.cssBtn[value*='確定']")
                    && (
                        document.querySelector("input[type='checkbox'][value='6']")
                        || document.querySelector("input[type='radio'][value='5']")
                    )
                )
                """
            )
        )

    @staticmethod
    def _survey_is_filled(status_text: str) -> bool:
        """Return whether dashboard text says the survey is already filled."""

        return any(marker in status_text for marker in SurveyAutomation.FILLED_MARKERS)

    @staticmethod
    def _survey_is_unfilled(status_text: str) -> bool:
        """Return whether dashboard text says the survey is not filled."""

        return any(marker in status_text for marker in SurveyAutomation.UNFILLED_MARKERS)

    @staticmethod
    def _format_seconds(seconds: int) -> str:
        """Format seconds as HH:MM:SS for user-facing messages."""

        hours, remainder = divmod(max(0, seconds), 3600)
        minutes, second = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{second:02d}"

    @staticmethod
    def _accept_dialog(dialog: Dialog) -> None:
        """Accept browser confirm dialogs raised by the survey submit button."""

        try:
            dialog.accept()
        except Exception:
            pass

    @staticmethod
    def _emit(
        emit_result: Callable[[SurveyResult], None] | None,
        result: SurveyResult,
    ) -> None:
        """Emit a result to the GUI when a callback is available."""

        if emit_result is not None:
            emit_result(result)
