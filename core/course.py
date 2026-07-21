"""Course scanning service for the E learning dashboard.

This module is intentionally read-only.  It only opens the personal course
dashboard and extracts course metadata that the GUI or future automation layer
can consume.  Starting, playing, skipping, or completing courses should live in
a separate automation module later.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
from typing import TYPE_CHECKING, Any

from core.config import AppConfig
from core.logger import get_logger
from core.selectors import SelectorManager

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any


class CourseStatus(str, Enum):
    """Normalized course state used by the GUI and future automation code."""

    UNKNOWN = "unknown"
    UNFINISHED = "unfinished"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class CourseInfo:
    """A single course item extracted from the personal dashboard."""

    title: str
    course_url: str
    status: CourseStatus = CourseStatus.UNKNOWN
    course_type: str = ""
    certification_hours: str = ""
    exam_score: str = ""
    survey_status: str = ""
    action_text: str = ""
    raw_text: str = ""
    can_attend: bool | None = None
    assessment_status: str = ""
    inspection_note: str = ""
    required_reading_seconds: int = 0
    studied_reading_seconds: int = 0
    remaining_reading_seconds: int = 0


class CourseScanError(RuntimeError):
    """Raised when the course dashboard cannot be scanned safely."""


class CourseScanner:
    """Read the logged-in user's course list from the dashboard.

    The scanner accepts an existing Playwright page from ``BrowserController`` so
    it can reuse the persistent session created during login.  It does not own
    or close the browser.
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

    def scan_unfinished_courses(self) -> list[CourseInfo]:
        """Open the unfinished-course tab and return all visible course items."""

        dashboard_url = self._config.base_url.replace("index.php", "user/learn_dashboard.php?tab=1")
        self._logger.info("開始掃描未完成課程：%s", dashboard_url)

        try:
            self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30_000)
            self._wait_until_course_area_ready()
            courses = self._extract_courses_from_all_pages(default_status=CourseStatus.UNFINISHED)
        except CourseScanError:
            raise
        except Exception as exc:
            self._logger.exception("掃描課程時發生未預期錯誤。")
            raise CourseScanError(f"掃描課程失敗：{exc}") from exc

        self._logger.info("掃描完成，共找到 %d 門未完成課程。", len(courses))
        return courses

    def scan_unanswered_assessment_courses(self) -> list[CourseInfo]:
        """Enter each course and return accessible courses with unanswered tests.

        The dashboard score is not authoritative: some cards show ``0 分`` while
        the course detail page shows ``測驗：--``.  This workflow therefore
        verifies the course player and opens only its assessment *list*.  It
        never opens an assessment attempt, reads question text, selects an
        answer, or submits a form.
        """

        courses = self.scan_unfinished_courses()
        unanswered: list[CourseInfo] = []
        for index, course in enumerate(courses, start=1):
            self._logger.info("檢查第 %d/%d 門課程：%s", index, len(courses), course.title)
            inspected = self._inspect_course_access_and_assessment(course)
            self._logger.info(
                "課程檢查結果：%s；可上課=%s；測驗=%s；原因=%s",
                inspected.title,
                inspected.can_attend,
                inspected.assessment_status or "不明",
                inspected.inspection_note or "-",
            )
            if inspected.can_attend and self._is_unanswered_assessment(inspected.assessment_status):
                unanswered.append(inspected)

        dashboard_url = self._config.base_url.replace("index.php", "user/learn_dashboard.php?tab=1")
        try:
            self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30_000)
            self._wait_until_course_area_ready()
        except Exception:
            self._logger.exception("完成檢查後無法返回課程清單。")

        self._logger.info("未答測驗掃描完成，共找到 %d 門課程。", len(unanswered))
        return unanswered

    def _inspect_course_access_and_assessment(self, course: CourseInfo) -> CourseInfo:
        """Verify course access, then inspect only the assessment-list status."""

        if not course.course_url:
            return replace(
                course,
                can_attend=False,
                inspection_note="缺少課程網址",
            )

        original_page = self._page
        active_page = original_page
        original_pages = tuple(original_page.context.pages)
        detail_status = course.exam_score
        try:
            original_page.goto(course.course_url, wait_until="domcontentloaded", timeout=30_000)
            self._wait_for_network_to_settle()
            detail_body = original_page.locator(self._selectors.get("course.detail_body").value)
            detail_body.wait_for(state="attached", timeout=15_000)
            detail_text = detail_body.inner_text(timeout=15_000)
            detail_status = self._extract_detail_assessment_status(detail_text) or course.exam_score

            entry_selector = self._selectors.get("automation.course_entry_button").value
            entries = original_page.locator(entry_selector)
            if entries.count() == 0:
                return replace(
                    course,
                    can_attend=False,
                    assessment_status=detail_status,
                    inspection_note="課程詳細頁沒有可用的上課按鈕",
                )

            entry = entries.nth(0)
            entry.wait_for(state="visible", timeout=15_000)
            try:
                with original_page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
                    entry.click(timeout=20_000)
            except Exception:
                self._logger.info("上課按鈕未造成一般頁面跳轉，改檢查同頁或新分頁播放器。")

            current_pages = tuple(original_page.context.pages)
            newly_opened = [page for page in current_pages if page not in original_pages]
            active_page = newly_opened[-1] if newly_opened else original_page
            try:
                active_page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except Exception:
                pass

            can_attend, access_note = self._inspect_player_access(active_page)
            if not can_attend:
                return replace(
                    course,
                    can_attend=False,
                    assessment_status=detail_status,
                    inspection_note=access_note,
                )

            assessment_status, assessment_note = self._inspect_assessment_list(
                active_page,
                detail_status,
            )
            return replace(
                course,
                can_attend=True,
                assessment_status=assessment_status,
                inspection_note=assessment_note,
            )
        except Exception as exc:
            self._logger.exception("逐門檢查課程失敗：%s", course.title)
            return replace(
                course,
                can_attend=False,
                assessment_status=detail_status,
                inspection_note=f"檢查失敗：{exc}",
            )
        finally:
            if active_page is not original_page and active_page not in original_pages:
                try:
                    active_page.close(run_before_unload=False)
                except Exception:
                    self._logger.info("檢查後未能關閉臨時課程分頁：%s", course.title)

    def _inspect_player_access(self, page: Page) -> tuple[bool, str]:
        """Return whether the opened page is a usable course player shell."""

        denied_markers = (
            "您非本門課的學生",
            "非本門課的學生",
            "無權限進入",
            "無法進入課程",
            "課程尚未開放",
        )
        combined_text: list[str] = []
        frame_urls: list[str] = []
        for frame in page.frames:
            frame_urls.append(frame.url or "")
            try:
                text = frame.locator("body").inner_text(timeout=2_000)
                combined_text.append(text[:1_500])
            except Exception:
                continue

        player_text = " ".join(combined_text)
        denied = next((marker for marker in denied_markers if marker in player_text), "")
        if denied:
            return False, f"網站拒絕進入：{denied}"

        has_player_frame = any(
            marker in url
            for url in frame_urls
            for marker in ("/learn/mooc_sysbar.php", "/learn/path/", "/online/online.php")
        )
        has_learning_menu = "開始上課" in player_text and "測驗/考試" in player_text
        if has_player_frame or has_learning_menu:
            return True, "已成功進入課程播放器"
        return False, "點擊上課按鈕後找不到課程播放器結構"

    def _inspect_assessment_list(self, page: Page, detail_status: str) -> tuple[str, str]:
        """Open the assessment list and read status labels without opening questions."""

        selector = self._selectors.get("assessment.entry_link").value
        assessment_entry = None
        for frame in page.frames:
            try:
                entries = frame.locator(selector)
                if entries.count() > 0:
                    assessment_entry = entries.nth(0)
                    break
            except Exception:
                continue

        if assessment_entry is None:
            return "沒有測驗入口", "播放器內沒有測驗/考試入口"

        target_frame = page.frame(name="s_main")
        assessment_entry.click(timeout=15_000)
        if target_frame is not None:
            try:
                target_frame.wait_for_url("**/learn/exam/exam_list.php**", timeout=15_000)
            except Exception:
                self._logger.info("測驗清單網址未匹配，改以目前 frame 內容判斷。")

        assessment_frame = target_frame
        if assessment_frame is None or "exam" not in assessment_frame.url.lower():
            assessment_frame = next(
                (frame for frame in page.frames if "/learn/exam/" in (frame.url or "")),
                assessment_frame,
            )
        if assessment_frame is None:
            return detail_status or "狀態不明", "點擊測驗入口後找不到測驗清單 frame"

        body_selector = self._selectors.get("assessment.list_body").value
        body = assessment_frame.locator(body_selector)
        body.wait_for(state="attached", timeout=15_000)
        list_text = body.inner_text(timeout=15_000)
        status = self._classify_assessment_list(list_text, detail_status)
        return status, "已進入測驗清單並完成狀態檢查；未開啟任何題目"

    @staticmethod
    def _extract_detail_assessment_status(text: str) -> str:
        """Extract the assessment status near the course-detail status block."""

        normalized = text.replace("\r", "")
        status_block_index = normalized.find("我的課程狀態")
        status_block = normalized[status_block_index : status_block_index + 800] if status_block_index >= 0 else normalized
        match = re.search(
            r"測驗\s*[：:]\s*(.*?)(?=\s+問卷\s*[：:]|\s+通過狀態\s*[：:]|$)",
            status_block,
            flags=re.DOTALL,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _classify_assessment_list(list_text: str, detail_status: str) -> str:
        """Classify list-page status without inspecting assessment questions."""

        normalized = " ".join(list_text.split())
        if re.search(r"查無.*測驗|目前無.*測驗|沒有.*測驗|尚無.*測驗", normalized):
            return "沒有可作答測驗"
        if re.search(r"未作答|尚未作答|未測驗|尚未測驗|尚未應試|開始測驗|進入測驗|我要應試", normalized):
            return "未作答"
        if re.search(r"已作答|已測驗|測驗完成|通過|及格", normalized):
            return "已完成"
        if detail_status.strip() in {"--", "－", "未作答", "尚未測驗", "未測驗"}:
            return "未作答"
        return detail_status or "狀態不明"

    @staticmethod
    def _is_unanswered_assessment(exam_score: str) -> bool:
        """Identify explicit unanswered markers from the dashboard score field."""

        normalized = "".join(exam_score.split()).lower()
        if not normalized:
            return False

        answered_markers = ("已作答", "已測驗", "通過", "不及格", "未通過")
        if any(marker in normalized for marker in answered_markers):
            return False

        unanswered_markers = (
            "未作答",
            "尚未作答",
            "未測驗",
            "尚未測驗",
            "未考試",
            "尚未考試",
            "無成績",
            "未有成績",
            "未完成",
            "有可作答測驗",
            "--",
        )
        return any(marker in normalized for marker in unanswered_markers)

    def _extract_courses_from_all_pages(self, default_status: CourseStatus) -> list[CourseInfo]:
        """Extract courses from every dashboard pagination page."""

        courses: list[CourseInfo] = []
        seen_keys: set[tuple[str, str]] = set()
        max_pages = 30

        for page_index in range(1, max_pages + 1):
            page_courses = self._extract_courses_from_page(default_status=default_status)
            self._logger.info("第 %d 頁掃描到 %d 門課程。", page_index, len(page_courses))
            for course in page_courses:
                key = (course.title, course.course_url)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                courses.append(course)

            if not self._go_to_next_course_page():
                break
            self._wait_until_course_area_ready()
        else:
            self._logger.warning("課程分頁超過 %d 頁，停止掃描避免無限迴圈。", max_pages)

        return courses

    def _wait_until_course_area_ready(self) -> None:
        """Wait until the dashboard's AJAX course list has settled.

        The dashboard first renders a generic system-message block, then fills
        ``.course-list-block`` items through JavaScript.  Waiting for the empty
        message alone is therefore too early and can produce a false zero-count
        result, especially immediately after login.
        """

        item_selector = self._selectors.get("course.list_item").value
        container_selector = self._selectors.get("course.list_container").value
        empty_selector = self._selectors.get("course.empty_message").value

        try:
            self._page.locator(container_selector).first.wait_for(state="attached", timeout=30_000)
            self._wait_for_network_to_settle()
            self._page.wait_for_function(
                """
                ([itemSelector, emptySelector]) => {
                    const itemCount = document.querySelectorAll(itemSelector).length;
                    if (itemCount > 0) {
                        return true;
                    }

                    const ajaxIdle = !window.jQuery || window.jQuery.active === 0;
                    const bodyText = document.body ? document.body.innerText : "";
                    const emptyNode = document.querySelector(emptySelector);
                    const hasRealEmptyMessage =
                        Boolean(emptyNode && emptyNode.innerText.trim()) &&
                        /查無|沒有|無符合|目前無|尚無/.test(bodyText);

                    return ajaxIdle && hasRealEmptyMessage;
                }
                """,
                arg=[item_selector, empty_selector],
                timeout=30_000,
            )
        except Exception as exc:
            raise CourseScanError("找不到課程清單區塊，可能尚未登入或頁面結構已變更。") from exc

    def _wait_for_network_to_settle(self) -> None:
        """Wait for common post-load AJAX activity without using fixed sleep."""

        try:
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            self._logger.info("課程頁仍有背景連線，改由 DOM 條件繼續判斷。")

    def _go_to_next_course_page(self) -> bool:
        """Click the dashboard next-page control when it is available."""

        next_locator = self._page.locator("#pageToolbar a[title='下一頁']").first
        if next_locator.count() == 0:
            self._logger.info("找不到課程分頁下一頁按鈕，視為最後一頁。")
            return False

        try:
            class_name = next_locator.get_attribute("class") or ""
            if "disabled" in class_name:
                self._logger.info("課程分頁已到最後一頁。")
                return False

            before_signature = self._course_page_signature()
            before_page = self._current_dashboard_page_number()
            next_locator.click()
            self._page.wait_for_function(
                """
                ([oldSignature, oldPage]) => {
                    const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                    const titles = Array.from(document.querySelectorAll(".course-list-block .course-list-block-info-name"))
                        .map((node) => clean(node.textContent || node.innerText))
                        .join("|");
                    const pageValue = document.querySelector("#pageToolbar input.paginate-number")?.value || "";
                    const ajaxIdle = !window.jQuery || window.jQuery.active === 0;
                    return ajaxIdle && (titles !== oldSignature || pageValue !== oldPage);
                }
                """,
                arg=[before_signature, before_page],
                timeout=15_000,
            )
            self._wait_for_network_to_settle()
            return True
        except Exception:
            self._logger.exception("切換課程分頁失敗。")
            return False

    def _course_page_signature(self) -> str:
        """Return a compact signature of the currently visible course cards."""

        return self._page.evaluate(
            """
            () => Array.from(document.querySelectorAll(".course-list-block .course-list-block-info-name"))
                .map((node) => (node.textContent || node.innerText || "").replace(/\\s+/g, " ").trim())
                .join("|")
            """
        )

    def _current_dashboard_page_number(self) -> str:
        """Return the current dashboard pagination input value."""

        return self._page.evaluate(
            """() => document.querySelector("#pageToolbar input.paginate-number")?.value || "" """
        )

    def _extract_courses_from_page(self, default_status: CourseStatus) -> list[CourseInfo]:
        """Extract structured course data inside the browser DOM."""

        item_selector = self._selectors.get("course.list_item").value
        title_selector = self._selectors.get("course.title").value
        type_selector = self._selectors.get("course.type").value
        pass_selector = self._selectors.get("course.pass_info").value
        action_selector = self._selectors.get("course.action").value

        rows: list[dict[str, str]] = self._page.evaluate(
            """
            ([itemSelector, titleSelector, typeSelector, passSelector, actionSelector]) => {
                const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const pickText = (root, selector) => {
                    const node = root.querySelector(selector);
                    return clean(node ? node.textContent : "");
                };
                const pickTitle = (root) => {
                    const titleNode = root.querySelector(titleSelector);
                    if (!titleNode) {
                        return "";
                    }
                    const titledChild = titleNode.querySelector("[title]");
                    return clean((titledChild && titledChild.getAttribute("title")) || titleNode.textContent);
                };
                const pickCourseUrl = (root) => {
                    const link = root.querySelector("a[href*='/info/'], a[href]");
                    return link ? link.href : "";
                };
                return Array.from(document.querySelectorAll(itemSelector)).map((item) => ({
                    title: pickTitle(item),
                    course_url: pickCourseUrl(item),
                    course_type: pickText(item, typeSelector),
                    pass_text: pickText(item, passSelector),
                    action_text: pickText(item, actionSelector),
                    raw_text: clean(item.textContent),
                }));
            }
            """,
            [item_selector, title_selector, type_selector, pass_selector, action_selector],
        )

        courses: list[CourseInfo] = []
        for row in rows:
            title = row.get("title", "").strip()
            if not title:
                continue
            pass_text = row.get("pass_text", "")
            courses.append(
                CourseInfo(
                    title=title,
                    course_url=row.get("course_url", ""),
                    status=default_status,
                    course_type=row.get("course_type", ""),
                    certification_hours=self._extract_field(pass_text, "認證時數"),
                    exam_score=self._extract_field(pass_text, "測驗分數"),
                    survey_status=self._extract_field(pass_text, "問卷狀態"),
                    action_text=row.get("action_text", ""),
                    raw_text=row.get("raw_text", ""),
                )
            )
        return courses

    @staticmethod
    def _extract_field(text: str, label: str) -> str:
        """Return a compact field value from the dashboard's combined text."""

        normalized = " ".join(text.split())
        marker = f"{label} :"
        if marker not in normalized:
            return ""

        remainder = normalized.split(marker, 1)[1].strip()
        next_labels = ("測驗分數 :", "問卷狀態 :", "認證時數 :")
        end_positions = [
            remainder.find(next_label)
            for next_label in next_labels
            if next_label != marker and remainder.find(next_label) > 0
        ]
        if end_positions:
            remainder = remainder[: min(end_positions)]
        return remainder.strip()
