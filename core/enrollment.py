"""Course enrollment helper controlled by the desktop GUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.config import AppConfig
from core.logger import get_logger

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page
else:
    Locator = Any
    Page = Any


@dataclass(frozen=True, slots=True)
class EnrollmentSearchTask:
    """One keyword search task from the GUI input."""

    keyword: str
    count: int
    hours_minimum: str = "1"
    hours_maximum: str = "3"


@dataclass(frozen=True, slots=True)
class EnrollmentSearchConfig:
    """Search settings used by the registration center."""

    keywords: str
    hours_minimum: str = "1"
    hours_maximum: str = "3"

    def tasks(self) -> list[EnrollmentSearchTask]:
        """Parse one 'keyword,count,min_hours,max_hours' condition per line."""

        tasks: list[EnrollmentSearchTask] = []
        for raw_line in self.keywords.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split(",")]
            keyword = parts[0] if parts else ""
            keyword = keyword.strip()
            if not keyword:
                continue
            try:
                count = max(1, int(parts[1] if len(parts) > 1 and parts[1] else "1"))
            except ValueError:
                count = 1
            hours_minimum = parts[2] if len(parts) > 2 and parts[2] else self.hours_minimum or "1"
            hours_maximum = parts[3] if len(parts) > 3 and parts[3] else self.hours_maximum or "3"
            tasks.append(
                EnrollmentSearchTask(
                    keyword=keyword,
                    count=count,
                    hours_minimum=hours_minimum,
                    hours_maximum=hours_maximum,
                )
            )
        return tasks


@dataclass(frozen=True, slots=True)
class EnrollmentCourse:
    """One course that can be selected and enrolled from the GUI."""

    keyword: str
    course_id: str
    title: str
    hours: str
    status: str
    info_url: str


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    """Result of one enrollment attempt."""

    course: EnrollmentCourse
    success: bool
    message: str


class EnrollmentError(RuntimeError):
    """Raised when the enrollment center cannot be searched or submitted."""


class EnrollmentAssistant:
    """Search and enroll courses from E learning registration center."""

    def __init__(self, page: Page, config: AppConfig) -> None:
        self._page = page
        self._config = config
        self._logger = get_logger(__name__)

    def search_courses(self, search_config: EnrollmentSearchConfig) -> list[EnrollmentCourse]:
        """Search courses by keywords and return selectable rows to the GUI."""

        tasks = search_config.tasks()
        if not tasks:
            raise EnrollmentError("請先輸入至少一個選課關鍵字。")

        self._open_registration_center()
        self._ensure_search_form_ready()
        self._ensure_no_limit_checked()

        collected: list[EnrollmentCourse] = []
        for task in tasks:
            self._logger.info("搜尋選課關鍵字：%s", task.keyword)
            self._fill_search_form(task.keyword, task.hours_minimum, task.hours_maximum)
            courses = self._submit_search_and_collect(task.keyword)
            collected.extend(courses[: task.count])

        if not collected:
            self._logger.info("選課搜尋完成，沒有找到可報名課程。")
        return collected

    def enroll_courses(self, courses: list[EnrollmentCourse]) -> list[EnrollmentResult]:
        """Enroll selected courses from the GUI."""

        if not courses:
            raise EnrollmentError("請先勾選至少一門要報名的課程。")

        results: list[EnrollmentResult] = []
        for course in courses:
            try:
                self._logger.info("開始報名課程：%s (%s)", course.title, course.course_id)
                self._page.goto(course.info_url, wait_until="domcontentloaded", timeout=30_000)
                self._safe_wait_networkidle()
                self._click_enroll_button()
                self._confirm_enroll_dialog()
                results.append(EnrollmentResult(course=course, success=True, message="已送出報名"))
            except Exception as exc:
                self._logger.exception("報名課程失敗：%s", course.title)
                results.append(EnrollmentResult(course=course, success=False, message=str(exc)))
        return results

    def _open_registration_center(self) -> None:
        """Open the registration center page."""

        registration_url = self._config.base_url.replace("index.php", "user/registration_center.php")
        self._logger.info("開啟選課中心：%s", registration_url)
        self._page.goto(registration_url, wait_until="domcontentloaded", timeout=30_000)
        self._safe_wait_networkidle()

    def _safe_wait_networkidle(self) -> None:
        """Wait for quiet network when possible, but do not fail on long polling."""

        try:
            self._page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            self._logger.info("頁面仍有背景連線，繼續執行。")

    def _ensure_search_form_ready(self) -> None:
        """Wait until registration center search fields are available."""

        try:
            self._page.locator('input[name="keyword"]').wait_for(state="visible", timeout=15_000)
            self._page.locator("#btn-search").wait_for(state="visible", timeout=15_000)
        except Exception as exc:
            raise EnrollmentError("找不到選課搜尋欄位，請確認已登入並可進入選課中心。") from exc

    def _fill_search_form(self, keyword: str, hours_minimum: str, hours_maximum: str) -> None:
        """Fill search fields and keep the 'no target limit' checkbox selected."""

        self._fill_input('input[name="keyword"]', keyword)
        self._fill_input('input[name="certification_hours_minimum"]', hours_minimum)
        self._fill_input('input[name="certification_hours_maximum"]', hours_maximum)
        self._ensure_no_limit_checked()

    def _fill_input(self, selector: str, value: str) -> None:
        """Fill an input when present; optional fields may be absent on some layouts."""

        locator = self._page.locator(selector)
        if locator.count() == 0:
            return
        locator.first.fill(value)
        locator.first.dispatch_event("change")

    def _ensure_no_limit_checked(self) -> None:
        """Check the 'student target no limit' option when the page provides it."""

        checkbox = self._page.locator('input[name="student_target_no_limit"][value="Y"]')
        if checkbox.count() == 0:
            self._logger.info("選課中心找不到「對象不限」勾選框，略過。")
            return
        if not checkbox.first.is_checked():
            checkbox.first.check(force=True)

    def _submit_search_and_collect(self, keyword: str) -> list[EnrollmentCourse]:
        """Click search and collect stable rows from the result list."""

        old_html = self._result_html()
        self._page.locator("#btn-search").first.click()
        self._wait_for_result_stable(old_html)
        rows = self._parse_courses(keyword)
        self._logger.info("關鍵字「%s」找到 %s 筆未報名課程。", keyword, len(rows))
        return rows

    def _result_html(self) -> str:
        """Return current result container HTML for change detection."""

        return self._page.evaluate(
            "() => document.getElementById('listtype_list_content')?.innerHTML || ''"
        )

    def _wait_for_result_stable(self, old_html: str) -> None:
        """Wait until result rows are updated and stable."""

        try:
            self._page.wait_for_function(
                """
                ([oldHtml]) => {
                  const box = document.getElementById('listtype_list_content');
                  if (!box) return false;
                  const rows = box.querySelectorAll('.tb-row:not(.header)').length;
                  return box.innerHTML !== oldHtml || rows > 0;
                }
                """,
                arg=[old_html],
                timeout=12_000,
            )
        except Exception:
            self._logger.info("選課搜尋結果等待逾時，改以目前頁面內容解析。")
        try:
            self._page.wait_for_timeout(600)
        except Exception:
            pass

    def _parse_courses(self, keyword: str) -> list[EnrollmentCourse]:
        """Read result rows into dataclasses."""

        raw_courses = self._page.evaluate(
            """
            () => Array.from(document.querySelectorAll('#listtype_list_content .tb-row:not(.header)'))
              .map(row => {
                const link = row.querySelector('a[href^="/info/"]');
                if (!link) return null;
                const match = link.getAttribute('href').match(/\\/info\\/(\\d+)/);
                if (!match) return null;
                const title = (row.querySelector('h3')?.textContent || link.textContent || '').trim();
                let hours = '';
                let status = '';
                row.querySelectorAll('.info div').forEach(div => {
                  const text = div.textContent || '';
                  if (text.includes('認證時數')) {
                    const h = text.match(/([\\d.]+)\\s*小時/);
                    if (h) hours = h[1];
                  }
                  if (text.includes('選課狀態')) {
                    status = (div.querySelector('span')?.textContent || '').trim();
                  }
                });
                return { courseId: match[1], title, hours, status };
              })
              .filter(Boolean)
            """
        )
        courses: list[EnrollmentCourse] = []
        for item in raw_courses:
            status = str(item.get("status", "")).strip()
            if status != "未報名":
                continue
            course_id = str(item.get("courseId", "")).strip()
            if not course_id:
                continue
            courses.append(
                EnrollmentCourse(
                    keyword=keyword,
                    course_id=course_id,
                    title=str(item.get("title", "")).strip() or f"課程 {course_id}",
                    hours=str(item.get("hours", "")).strip(),
                    status=status,
                    info_url=f"https://elearn.hrd.gov.tw/info/{course_id}",
                )
            )
        return courses

    def _click_enroll_button(self) -> None:
        """Click the enrollment button on a course info page."""

        candidates = (
            'button[onclick*="enployCourse"]',
            'a[onclick*="enployCourse"]',
            'input[onclick*="enployCourse"]',
            'button:has-text("報名")',
            'a:has-text("報名")',
            'input[value*="報名"]',
        )
        button = self._first_visible(candidates, timeout=12_000)
        if button is None:
            debug = self._page.evaluate(
                """
                () => Array.from(document.querySelectorAll('button,a,input'))
                  .map(el => ({
                    tag: el.tagName,
                    text: (el.textContent || el.value || '').trim(),
                    onclick: el.getAttribute('onclick') || '',
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                  }))
                  .filter(x => x.visible && (x.text.includes('報名') || x.onclick.includes('Course')))
                  .slice(0, 20)
                """
            )
            raise EnrollmentError(f"找不到可點擊的報名按鈕。偵測到的候選元素：{debug}")
        self._page.once("dialog", lambda dialog: dialog.accept())
        button.click()

    def _confirm_enroll_dialog(self) -> None:
        """Confirm bootbox/native enrollment confirmation dialogs."""

        candidates = (
            'button[data-bb-handler="confirm"].btn-success',
            '.modal-footer button.btn-success',
            'button:has-text("確定")',
            'button:has-text("確認")',
            'a:has-text("確定")',
        )
        button = self._first_visible(candidates, timeout=8_000)
        if button is not None:
            button.click()
            self._safe_wait_networkidle()
            return
        self._logger.info("未偵測到確認視窗，可能已直接送出或需人工確認。")

    def _first_visible(self, selectors: tuple[str, ...], timeout: int) -> Locator | None:
        """Find the first visible locator from several fallback selectors."""

        deadline_ms = timeout
        step_ms = 400
        elapsed = 0
        while elapsed <= deadline_ms:
            for selector in selectors:
                locator = self._page.locator(selector)
                count = locator.count()
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if candidate.is_visible():
                            return candidate
                    except Exception:
                        continue
            self._page.wait_for_timeout(step_ms)
            elapsed += step_ms
        return None
