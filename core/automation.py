"""Course playback automation.

The scheduler plays real course content sections only. It skips non-counting
items such as environment checks, beginner guides, and prefaces, then rotates
through chapter items like ``1-1`` and ``1-2``. The total play time is based on
course detail text such as ``上課時數`` and ``已上課時數`` when that data is
available. The configured ``play_minutes`` is the maximum time to stay on one
chapter before moving to the next chapter.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, Callable

from core.config import AppConfig
from core.course import CourseInfo
from core.logger import get_logger
from core.selectors import SelectorManager

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page
else:
    Locator = Any
    Page = Any


EXTRA_PLAYBACK_SECONDS_PER_COURSE = 5 * 60


class AutomationStatus(str, Enum):
    """High-level status for course automation actions."""

    ENTERED_COURSE = "entered_course"
    INSPECTED_PLAYER = "inspected_player"
    CHAPTER_SELECTED = "chapter_selected"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_WAITING = "playback_waiting"
    PLAYBACK_FINISHED = "playback_finished"
    PLAYBACK_STOPPED = "playback_stopped"
    COURSE_SKIPPED = "course_skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AutomationResult:
    """Result returned to the GUI after an automation action."""

    status: AutomationStatus
    message: str
    course_title: str = ""
    chapter_title: str = ""
    current_url: str = ""
    page_title: str = ""
    frame_urls: tuple[str, ...] = ()
    video_count: int = 0
    player_frame_url: str = ""
    remaining_seconds: int = 0


@dataclass(frozen=True, slots=True)
class PlaybackScheduleConfig:
    """Settings for automatic, time-based course playback."""

    play_minutes: int = 20
    selected_course_titles: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "PlaybackScheduleConfig":
        """Load playback settings from JSON, creating a default file if missing."""

        if not path.exists():
            default = cls()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "play_minutes": default.play_minutes,
                        "selected_course_titles": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return default

        data = json.loads(path.read_text(encoding="utf-8"))
        titles = tuple(str(title).strip() for title in data.get("selected_course_titles", []) if str(title).strip())
        minutes = int(data.get("play_minutes", 20))
        return cls(play_minutes=max(1, minutes), selected_course_titles=titles)


@dataclass(frozen=True, slots=True)
class CourseStudyTime:
    """Parsed study-time information for one course."""

    required_seconds: int = 0
    studied_seconds: int = 0
    survey_status: str = ""

    @property
    def remaining_seconds(self) -> int:
        """Return remaining required study seconds."""

        if self.required_seconds <= 0:
            return 0
        return max(0, self.required_seconds - self.studied_seconds)


@dataclass(frozen=True, slots=True)
class ChapterSelection:
    """A selected playable chapter in the course player."""

    title: str
    frame_url: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class ChapterCandidate:
    """A possible lesson entry discovered in one player frame."""

    title: str
    source: str
    activity_id: str = ""
    href: str = ""
    target: str = ""
    frame_url: str = ""


@dataclass(frozen=True, slots=True)
class LearningContentState:
    """Small snapshot used to verify that a chapter click changed content."""

    media_count: int
    slide_count: int
    environment_count: int
    lesson_signal_count: int
    signature: tuple[str, ...]


class AutomationError(RuntimeError):
    """Raised when a course automation action cannot continue safely."""


class CourseAutomation:
    """Controller for actions inside one selected course."""

    SKIP_CHAPTER_KEYWORDS = (
        "環境檢測",
        "新手上路",
        "前言",
        "課程首頁",
        "操作導覽",
        "操作說明",
        "課程介紹",
        "課程資訊",
        "目錄索引",
        "自我評量",
        "教材下載",
        "課程資源",
    )
    CHAPTER_PATTERN = re.compile(r"^\s*\d+\s*[-.]\s*\d+")
    CHINESE_CHAPTER_PATTERN = re.compile(r"^\s*[一二三四五六七八九十]+[、.．]")
    UNIT_CHAPTER_PATTERN = re.compile(r"(單元\s*[一二三四五六七八九十\d]+|第\s*[一二三四五六七八九十\d]+\s*[章節])")

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

    def read_course_study_time(self, course: CourseInfo) -> CourseStudyTime:
        """Open the course detail page and parse required/completed study time."""

        if not course.course_url:
            return CourseStudyTime()

        self._page.goto(course.course_url, wait_until="domcontentloaded", timeout=30_000)
        try:
            self._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        self._activate_certification_section()
        body_text = self._page.locator("body").inner_text(timeout=10_000)
        required = self._extract_required_reading_seconds(body_text)
        studied = self._extract_studied_reading_seconds(body_text)
        survey_status = self._extract_survey_status(body_text)
        return CourseStudyTime(
            required_seconds=required,
            studied_seconds=studied,
            survey_status=survey_status,
        )

    def return_to_course_dashboard(self) -> AutomationResult:
        """Leave the active player and return to the personal course list."""

        dashboard_url = self._config.base_url.replace(
            "index.php",
            "user/learn_dashboard.php?tab=1",
        )
        try:
            if self._page.is_closed():
                pages = [page for page in self._page.context.pages if not page.is_closed()]
                if not pages:
                    raise AutomationError("瀏覽器目前沒有可使用的頁面。")
                self._page = pages[-1]
            self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30_000)
        except AutomationError:
            raise
        except Exception as exc:
            self._logger.exception("停止上課後無法返回課程清單。")
            raise AutomationError(f"已停止上課，但返回課程清單失敗：{exc}") from exc

        return AutomationResult(
            status=AutomationStatus.PLAYBACK_STOPPED,
            message="已停止上課並返回個人課程清單。",
            current_url=self._page.url,
            page_title=self._page.title(),
        )

    def enter_course(self, course: CourseInfo) -> AutomationResult:
        """Open a course detail page and enter the course player shell."""

        if not course.course_url:
            raise AutomationError("這門課缺少課程網址，請重新掃描課程。")

        self._logger.info("準備進入課程：%s", course.title)
        try:
            self._page.goto(course.course_url, wait_until="domcontentloaded", timeout=30_000)
            entry = self._page.locator(self._selectors.get("automation.course_entry_button").value).first
            entry.wait_for(state="attached", timeout=30_000)
            active_page = self._trigger_course_entry_once(entry)
            self._wait_for_player_shell(active_page)
            self._page = active_page
            self._close_blocking_popups()
        except AutomationError:
            raise
        except Exception as exc:
            self._logger.exception("進入課程失敗。")
            raise AutomationError(f"進入課程失敗：{exc}") from exc

        frame_urls = tuple(frame.url for frame in self._page.frames if frame.url)
        return AutomationResult(
            status=AutomationStatus.ENTERED_COURSE,
            message="已進入課程頁面，準備選擇正式章節。",
            course_title=course.title,
            current_url=self._page.url,
            page_title=self._page.title(),
            frame_urls=frame_urls,
            video_count=self._count_video_elements(self._page),
            player_frame_url=self._find_player_frame_url(self._page),
        )

    def inspect_player(self) -> AutomationResult:
        """Inspect the current course/player page without changing progress."""

        page = self._page
        self._wait_for_player_shell(page)
        frame_urls = tuple(frame.url for frame in page.frames if frame.url)
        video_count = self._count_video_elements(page)
        message = f"課程頁偵測完成：iframe {len(frame_urls)} 個，影片元素 {video_count} 個。"
        return AutomationResult(
            status=AutomationStatus.INSPECTED_PLAYER,
            message=message,
            current_url=page.url,
            page_title=page.title(),
            frame_urls=frame_urls,
            video_count=video_count,
            player_frame_url=self._find_player_frame_url(page),
        )

    def select_playable_chapter(self, visited_titles: set[str]) -> ChapterSelection:
        """Click the next playable chapter such as ``1-1`` or ``1-2``."""

        candidates = self._collect_chapter_candidates()
        debug_summary = self._build_player_debug_summary(candidates)
        self._close_blocking_popups()
        for candidate in candidates:
            if candidate.title in visited_titles:
                continue
            if not self._is_playable_candidate(candidate):
                continue
            before_state = self._capture_learning_content_state()
            if self._click_chapter_candidate(candidate):
                if candidate.source in {"launchActivity", "chapterId"}:
                    self._close_blocking_popups()
                    self._logger.info("章節已由網站選單成功點擊，直接進入閱讀倒數：%s", candidate.title)
                    return ChapterSelection(
                        title=candidate.title,
                        frame_url=self._find_player_frame_url(self._page),
                        source=f"{candidate.source}:trusted_click",
                    )
                self._wait_after_chapter_click(before_state)
                self._close_blocking_popups()
                after_state = self._capture_learning_content_state()
                if self._chapter_click_loaded_content(before_state, after_state):
                    return ChapterSelection(
                        title=candidate.title,
                        frame_url=self._find_player_frame_url(self._page),
                        source=candidate.source,
                    )
                self._logger.info(
                    "章節候選未切換到教材內容，改試下一個：%s；before=%s；after=%s",
                    candidate.title,
                    before_state,
                    after_state,
                )
                if candidate.source in {"launchActivity", "chapterId"}:
                    self._logger.info("章節已由網站選單成功點擊，教材內容無法由 DOM 驗證，仍進入閱讀倒數：%s", candidate.title)
                    return ChapterSelection(
                        title=candidate.title,
                        frame_url=self._find_player_frame_url(self._page),
                        source=f"{candidate.source}:dom_unverified",
                    )

        raise AutomationError(f"找不到可播放章節。{debug_summary}")

    def start_current_video(self) -> AutomationResult:
        """Mark the current lesson as started without blocking on player internals."""

        page = self._page
        self._close_blocking_popups()
        frame_urls = tuple(frame.url for frame in page.frames if frame.url)
        try:
            media_count = page.locator(
                "video, audio, object, embed, canvas, .pdfViewer, "
                ".page[data-page-number], #viewerContainer, #viewer"
            ).count()
        except Exception:
            media_count = 0

        message = "已進入教材頁，開始閱讀時數倒數。"
        return AutomationResult(
            status=AutomationStatus.PLAYBACK_STARTED,
            message=message,
            current_url=page.url,
            page_title=page.title(),
            frame_urls=frame_urls,
            video_count=media_count,
            player_frame_url=self._find_player_frame_url(page),
        )

    def _trigger_course_entry_once(self, entry: Locator) -> Page:
        """Click the course entry button once and return the active page."""

        active_page = self._page
        try:
            entry.scroll_into_view_if_needed(timeout=10_000)
        except Exception:
            self._logger.info("進入課程按鈕無法捲動到畫面中央，仍繼續嘗試點擊。")

        try:
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                entry.click(timeout=30_000)
        except Exception:
            self._logger.info("進入課程後未偵測到一般頁面跳轉，檢查是否已在原頁或新頁載入。")
            try:
                active_page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass

        pages = self._page.context.pages
        if pages:
            active_page = pages[-1]
            try:
                active_page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass
        return active_page

    def _wait_for_player_shell(self, page: Page) -> None:
        """Wait until the course player shell or its frames are available."""

        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            self._logger.info("課程頁仍有背景連線，改由頁面條件繼續判斷。")

        try:
            page.wait_for_function(
                """
                () => location.href.includes("/learn/") ||
                    document.querySelectorAll("iframe, frame").length > 0
                """,
                timeout=30_000,
            )
        except Exception as exc:
            raise AutomationError("已開啟課程，但找不到課程播放器頁面。") from exc

    def _collect_chapter_candidates(self) -> list[ChapterCandidate]:
        """Collect lesson candidates from several course-player layouts."""

        candidates: list[ChapterCandidate] = []
        seen: set[tuple[str, str, str, str]] = set()
        for frame in self._page.frames:
            try:
                frame_candidates = frame.evaluate(
                    """
                    () => {
                        const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                        const output = [];
                        const push = (node, source, extra = {}) => {
                            const text = clean(node.getAttribute("title")) ||
                                clean(node.innerText || node.textContent) ||
                                clean(node.getAttribute("aria-label"));
                            if (!text) {
                                return;
                            }
                            output.push({
                                title: text,
                                source,
                                activity_id: extra.activity_id || "",
                                href: node.href || node.getAttribute("href") || "",
                                target: node.getAttribute("target") || "",
                            });
                        };

                        for (const node of document.querySelectorAll("a[onclick*='launchActivity'], [onclick*='launchActivity']")) {
                            const onclick = node.getAttribute("onclick") || "";
                            const match = onclick.match(/launchActivity\\(\\s*this\\s*,\\s*['"]([^'"]+)['"]/);
                            push(node, "launchActivity", {activity_id: match ? match[1] : ""});
                        }

                        for (const node of document.querySelectorAll("li[id], a[id], button[id], [data-id], [data-activity-id]")) {
                            const id = node.id || node.getAttribute("data-id") || node.getAttribute("data-activity-id") || "";
                            if (/^I?\\d+\\s*[-.]\\s*\\d+/i.test(id) || /^I\\d+[-.]\\d+/i.test(id) || /^ITEM-/i.test(id) || /^I_SCO_/i.test(id)) {
                                push(node, "chapterId", {activity_id: id});
                            }
                        }

                        for (const node of document.querySelectorAll("a[href*='/learn/path/launch'], a[href*='launch.htm'], a[href*='launch.php']")) {
                            push(node, "legacyStart");
                        }

                        if (document.querySelector("video, object, embed")) {
                            output.push({
                                title: clean(document.title) || "目前教材頁",
                                source: "currentMedia",
                                activity_id: "",
                                href: location.href,
                                target: "",
                            });
                        }
                        return output;
                    }
                    """
                )
            except Exception:
                continue

            for item in frame_candidates:
                title = str(item.get("title", "")).strip()
                source = str(item.get("source", "")).strip()
                href = str(item.get("href", "")).strip()
                activity_id = str(item.get("activity_id", "")).strip()
                key = (title, source, href, activity_id)
                if not title or key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    ChapterCandidate(
                        title=title,
                        source=source,
                        activity_id=activity_id,
                        href=href,
                        target=str(item.get("target", "")).strip(),
                        frame_url=frame.url,
                    )
                )
        return self._sort_chapter_candidates(candidates)

    def _click_chapter_candidate(self, candidate: ChapterCandidate) -> bool:
        """Click a lesson candidate using the handler expected by its layout."""

        if self._click_chapter_candidate_with_locator(candidate):
            return True

        for frame in self._page.frames:
            try:
                clicked = bool(
                    frame.evaluate(
                        """
                        async (candidate) => {
                            const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                            const sameTitle = (node) => {
                                const title = clean(node.getAttribute("title"));
                                const text = clean(node.innerText || node.textContent);
                                const label = clean(node.getAttribute("aria-label"));
                                return title === candidate.title ||
                                    text === candidate.title ||
                                    label === candidate.title;
                            };
                            const sameHref = (node) => candidate.href && node.href === candidate.href;
                            const sameActivity = (node) => {
                                const onclick = node.getAttribute("onclick") || "";
                                return candidate.activity_id && onclick.includes(candidate.activity_id);
                            };

                            const nodes = Array.from(document.querySelectorAll(
                                "a[onclick*='launchActivity'], [onclick*='launchActivity'], li[id], a[id], button[id], a[href]"
                            ));
                            const exact = nodes.find((node) =>
                                (sameTitle(node) && (sameActivity(node) || sameHref(node) || candidate.source !== "launchActivity")) ||
                                sameActivity(node) ||
                                sameHref(node)
                            );
                            if (!exact) {
                                return false;
                            }

                            const clickable = exact.matches("a, button, [onclick]") ?
                                exact :
                                exact.querySelector("a[onclick*='launchActivity'], a[href], button, [onclick]") || exact;
                            const onclick = clickable.getAttribute("onclick") || "";
                            const match = onclick.match(/launchActivity\\(\\s*this\\s*,\\s*['"]([^'"]+)['"]\\s*,\\s*['"]?([^'")]*)['"]?\\s*\\)/);
                            if (clickable.scrollIntoView) {
                                clickable.scrollIntoView({block: "center", inline: "center"});
                            }
                            clickable.dispatchEvent(new MouseEvent("mouseover", {bubbles: true}));
                            clickable.dispatchEvent(new MouseEvent("mousedown", {bubbles: true}));
                            clickable.dispatchEvent(new MouseEvent("mouseup", {bubbles: true}));

                            if (candidate.source === "legacyStart" && clickable.getAttribute("target")) {
                                const target = clickable.getAttribute("target");
                                const targetWindow = parent && parent[target];
                                if (targetWindow) {
                                    targetWindow.location.href = clickable.href || clickable.getAttribute("href");
                                } else {
                                    clickable.click();
                                }
                            } else {
                                clickable.click();
                            }

                            await new Promise((resolve) => window.setTimeout(resolve, 250));
                            if (match && typeof window.launchActivity === "function") {
                                const item = clickable.closest("li");
                                const selected = item && item.classList.contains("selected");
                                if (!selected) {
                                    window.launchActivity(clickable, match[1], match[2] || "null");
                                }
                            }
                            return true;
                        }
                        """,
                        {
                            "title": candidate.title,
                            "source": candidate.source,
                            "activity_id": candidate.activity_id,
                            "href": candidate.href,
                        },
                    )
                )
                if clicked:
                    return True
            except Exception:
                continue
        return False

    def _click_chapter_candidate_with_locator(self, candidate: ChapterCandidate) -> bool:
        """Prefer Playwright's real click over synthetic JavaScript events."""

        selectors: list[str] = []
        if candidate.activity_id:
            selectors.append(f"a[onclick*={json.dumps(candidate.activity_id, ensure_ascii=False)}]")
        if candidate.title:
            selectors.append(f"a[title={json.dumps(candidate.title, ensure_ascii=False)}]")
        if candidate.href:
            selectors.append(f"a[href={json.dumps(candidate.href, ensure_ascii=False)}]")

        for frame in self._page.frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector)
                    count = locator.count()
                except Exception:
                    continue

                for index in range(count):
                    try:
                        item = locator.nth(index)
                        if not item.is_visible(timeout=500):
                            continue
                        item.scroll_into_view_if_needed(timeout=3_000)
                        item.click(timeout=10_000)
                        self._logger.info("已用 Playwright 真實點擊章節：%s", candidate.title)
                        return True
                    except Exception:
                        continue

        return False

    def _wait_after_chapter_click(self, before_state: LearningContentState) -> None:
        """Wait for content after selecting a chapter."""

        self._close_blocking_popups()
        try:
            self._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._close_blocking_popups()
            after_state = self._capture_learning_content_state()
            if self._chapter_click_loaded_content(before_state, after_state):
                return
            time.sleep(0.25)
        self._close_blocking_popups()

    def _capture_learning_content_state(self) -> LearningContentState:
        """Capture frame URLs, media-like nodes, and environment-check markers."""

        signatures: list[str] = []
        media_count = 0
        slide_count = 0
        environment_count = 0
        lesson_signal_count = 0
        for frame in self._page.frames:
            try:
                data = frame.evaluate(
                    """
                    () => {
                        const text = (document.body ? document.body.innerText : "")
                            .replace(/\\s+/g, " ")
                            .trim();
                        const directMedia = document.querySelectorAll("video, audio, object, embed").length;
                        const slideMedia = document.querySelectorAll(
                            "canvas, .pdfViewer, .page[data-page-number], #viewerContainer, #viewer, .slide, .slides, [class*='pdf'], [id*='pdf']"
                        ).length;
                        const lessonFrames = Array.from(document.querySelectorAll("iframe[src], frame[src]"))
                            .filter((node) => {
                                const src = node.getAttribute("src") || "";
                                return src &&
                                    !src.includes("about:blank") &&
                                    !src.includes("pathtree") &&
                                    !src.includes("co_dialog") &&
                                    !src.includes("/online/msg_view.php");
                            }).length;
                        const url = location.href || "";
                        const hasLessonSignal =
                            url.includes("SCORM_fetchResource") ||
                            url.includes("/contents/") ||
                            url.includes("contents/") ||
                            url.includes("Resource") ||
                            document.querySelectorAll("script[src*='scorm'], script[src*='SCORM']").length > 0 ||
                            document.querySelectorAll("canvas, .pdfViewer, .page[data-page-number], #viewerContainer, #viewer").length > 0 ||
                            text.includes("投影片");
                        const hasEnvironment =
                            text.includes("電腦環境檢測") ||
                            text.includes("環境檢測結果") ||
                            text.includes("環境檢測");
                        return {
                            url,
                            title: document.title || "",
                            text: text.slice(0, 180),
                            media: directMedia + lessonFrames + slideMedia,
                            slides: slideMedia,
                            environment: hasEnvironment,
                            lessonSignal: hasLessonSignal,
                        };
                    }
                    """
                )
            except Exception:
                continue

            url = str(data.get("url", ""))
            title = str(data.get("title", ""))
            text = str(data.get("text", ""))
            frame_media_count = int(data.get("media", 0))
            frame_slide_count = int(data.get("slides", 0))
            media_count += frame_media_count
            slide_count += frame_slide_count
            if bool(data.get("environment", False)):
                environment_count += 1
            if bool(data.get("lessonSignal", False)) or frame_media_count > 0:
                lesson_signal_count += 1
            signatures.append(f"{url}|{title}|{text}|m={frame_media_count}|s={frame_slide_count}")

        return LearningContentState(
            media_count=media_count,
            slide_count=slide_count,
            environment_count=environment_count,
            lesson_signal_count=lesson_signal_count,
            signature=tuple(signatures),
        )

    @staticmethod
    def _chapter_click_loaded_content(
        before_state: LearningContentState,
        after_state: LearningContentState,
    ) -> bool:
        """Return True only when a click appears to load real lesson content."""

        if after_state.media_count > 0 and after_state.signature != before_state.signature:
            return True
        if after_state.media_count > before_state.media_count:
            return True
        if after_state.slide_count > 0 and after_state.signature != before_state.signature:
            return True
        if after_state.lesson_signal_count > before_state.lesson_signal_count:
            return True
        if (
            after_state.signature != before_state.signature
            and after_state.environment_count < before_state.environment_count
        ):
            return True
        return False

    def _close_blocking_popups(self) -> None:
        """Close platform message popups that cover the course player."""

        for frame in self._page.frames:
            try:
                frame.evaluate(
                    """
                    () => {
                        const popupFrame = document.querySelector(
                            ".fancybox-iframe[src*='/online/msg_view.php'], iframe[src*='/online/msg_view.php']"
                        );
                        if (!popupFrame) {
                            return false;
                        }

                        const closeButton = document.querySelector(
                            ".fancybox-close-small, [data-fancybox-close], button[title='Close'], button[aria-label='Close']"
                        );
                        if (closeButton) {
                            closeButton.click();
                            return true;
                        }

                        const containers = document.querySelectorAll(
                            ".fancybox-container, .fancybox-slide--iframe, .fancybox-bg"
                        );
                        containers.forEach((node) => node.remove());
                        document.documentElement.classList.remove("fancybox-enabled");
                        document.body.classList.remove("fancybox-active", "compensate-for-scrollbar");
                        return true;
                    }
                    """
                )
            except Exception:
                continue

    def confirm_idle_reminders(self) -> int:
        """Confirm visible reading-idle reminders across the player frame tree.

        The platform places this overlay in different frames for different
        course packages.  The selector is intentionally scoped to
        ``#div_auto_logout`` so normal course buttons are never clicked.
        """

        selector = self._selectors.get("player.idle_reminder_confirm").value
        confirmed_count = 0
        for frame in self._page.frames:
            try:
                buttons = frame.locator(selector)
                for index in range(buttons.count()):
                    button = buttons.nth(index)
                    if not button.is_visible(timeout=250):
                        continue
                    button.click(timeout=2_000)
                    confirmed_count += 1
                    self._logger.info(
                        "已確認閱讀閒置提醒：frame=%s",
                        frame.url or "about:blank",
                    )
            except Exception as exc:
                self._logger.debug(
                    "檢查閱讀閒置提醒時略過 frame=%s：%s",
                    frame.url or "about:blank",
                    exc,
                )
        return confirmed_count

    def _is_playable_chapter_title(self, title: str) -> bool:
        """Return True when a title looks like a counted lesson chapter."""

        if any(keyword in title for keyword in self.SKIP_CHAPTER_KEYWORDS):
            return False
        return bool(
            self.CHAPTER_PATTERN.search(title)
            or self.CHINESE_CHAPTER_PATTERN.search(title)
            or self.UNIT_CHAPTER_PATTERN.search(title)
        )

    def _is_playable_candidate(self, candidate: ChapterCandidate) -> bool:
        """Return True when a candidate should be used as a lesson entry."""

        if any(keyword in candidate.title for keyword in self.SKIP_CHAPTER_KEYWORDS):
            return False
        if candidate.source == "legacyStart":
            return "開始上課" in candidate.title or "launch" in candidate.href
        if candidate.source == "currentMedia":
            return True
        if candidate.source in {"launchActivity", "chapterId"}:
            return bool(candidate.activity_id or candidate.href or candidate.title)
        return self._is_playable_chapter_title(candidate.title)

    def _sort_chapter_candidates(self, candidates: list[ChapterCandidate]) -> list[ChapterCandidate]:
        """Put numbered chapters first and fallback entries later."""

        source_order = {
            "launchActivity": 0,
            "chapterId": 1,
            "currentMedia": 2,
            "legacyStart": 3,
        }
        return sorted(
            candidates,
            key=lambda item: (
                self._candidate_rank(item),
                source_order.get(item.source, 9),
                item.title,
            ),
        )

    def _candidate_rank(self, candidate: ChapterCandidate) -> int:
        """Rank real content before menu, cover, and fallback entries."""

        if self.CHAPTER_PATTERN.search(candidate.title):
            return 0
        if self.UNIT_CHAPTER_PATTERN.search(candidate.title):
            return 1
        if self.CHINESE_CHAPTER_PATTERN.search(candidate.title):
            return 2
        if candidate.source in {"launchActivity", "chapterId"}:
            return 3
        if candidate.source == "currentMedia":
            return 4
        return 5

    def _build_player_debug_summary(self, candidates: list[ChapterCandidate]) -> str:
        """Build compact debug info for unsupported player layouts."""

        frame_summaries: list[str] = []
        for index, frame in enumerate(self._page.frames[:12]):
            try:
                text = frame.locator("body").inner_text(timeout=1_000)
            except Exception:
                text = ""
            compact_text = " ".join(text.split())[:80]
            frame_summaries.append(f"{index}:{frame.url or 'about:blank'} [{compact_text}]")

        candidate_text = ", ".join(
            f"{item.source}:{item.title}" for item in candidates[:12]
        ) or "無"
        frame_text = " | ".join(frame_summaries) or "無 frame"
        return f"候選={candidate_text}；frames={frame_text}"

    def _find_player_frame_url(self, page: Page) -> str:
        """Return the most likely player iframe URL."""

        for frame in page.frames:
            if "/online/online.php" in frame.url or "/learn/" in frame.url or "/learn/path/" in frame.url:
                return frame.url
        return ""

    def _count_video_elements(self, page: Page) -> int:
        """Count media-like lesson elements across the page and nested frames."""

        total = 0
        for frame in page.frames:
            try:
                total += int(
                    frame.evaluate(
                        """
                        () => {
                            const directMedia = document.querySelectorAll("video, audio, object, embed").length;
                            const slideMedia = document.querySelectorAll(
                                "canvas, .pdfViewer, .page[data-page-number], #viewerContainer, #viewer, .slide, .slides, [class*='pdf'], [id*='pdf']"
                            ).length;
                            const lessonFrames = Array.from(document.querySelectorAll("iframe[src]"))
                                .filter((node) => {
                                    const src = node.getAttribute("src") || "";
                                    return src &&
                                        !src.includes("about:blank") &&
                                        !src.includes("pathtree") &&
                                        !src.includes("co_dialog") &&
                                        !src.includes("/online/msg_view.php");
                                }).length;
                            return directMedia + lessonFrames + slideMedia;
                        }
                        """
                    )
                )
            except Exception:
                continue
        return total

    def _activate_certification_section(self) -> None:
        """Open the certification/pass-condition section if it is tabbed."""

        try:
            locator = self._page.get_by_text("認證時數", exact=True)
            count = locator.count()
            if count == 0:
                return
            for index in range(count - 1, -1, -1):
                item = locator.nth(index)
                try:
                    if item.is_visible(timeout=1_000):
                        item.click(timeout=3_000)
                        try:
                            self._page.wait_for_load_state("networkidle", timeout=5_000)
                        except Exception:
                            pass
                        return
                except Exception:
                    continue
        except Exception:
            self._logger.info("未能切換認證時數區塊，改以目前頁面文字解析通過條件。")

    @staticmethod
    def _extract_required_reading_seconds(text: str) -> int:
        """Parse required reading time from the pass-condition section."""

        normalized = " ".join(text.split())
        pass_condition_match = re.search(
            r"通過條件(?P<section>.*?)(我的課程狀態|學員推薦|關於平臺|$)",
            normalized,
        )
        search_area = pass_condition_match.group("section") if pass_condition_match else normalized
        durations = CourseAutomation._extract_all_reading_durations(search_area)
        if durations:
            return max(durations)
        return CourseAutomation._extract_labeled_duration(normalized, ("應閱讀時數", "最低閱讀時數", "閱讀時數"))

    @staticmethod
    def _extract_studied_reading_seconds(text: str) -> int:
        """Parse completed reading time from the learner-status section."""

        normalized = " ".join(text.split())
        status_match = re.search(
            r"我的課程狀態(?P<section>.*?)(通過狀態|學員推薦|關於平臺|$)",
            normalized,
        )
        search_area = status_match.group("section") if status_match else normalized
        return CourseAutomation._extract_labeled_duration(search_area, ("閱讀時數", "已閱讀時數", "已上課時數"))

    @staticmethod
    def _extract_survey_status(text: str) -> str:
        """Parse the learner's questionnaire state from the course detail page."""

        normalized = " ".join(text.split())
        status_match = re.search(
            r"我的課程狀態(?P<section>.*?)(通過狀態|學員推薦|關於平臺|$)",
            normalized,
        )
        search_area = status_match.group("section") if status_match else normalized
        match = re.search(
            r"問卷\s*[:：]\s*(未填寫?|尚未填寫?|已填寫?|已完成)",
            search_area,
        )
        return match.group(1) if match else ""

    @staticmethod
    def _extract_all_reading_durations(text: str) -> list[int]:
        """Return all durations immediately following a reading-time label."""

        pattern = r"閱讀時數\s*[:：]?\s*([0-9]+(?::[0-9]{1,2}){0,2}|[0-9]+(?:\.[0-9]+)?\s*小時|[0-9]+(?:\.[0-9]+)?\s*分鐘)"
        return [CourseAutomation._duration_to_seconds(match) for match in re.findall(pattern, text)]

    @staticmethod
    def _extract_labeled_duration(text: str, labels: tuple[str, ...]) -> int:
        """Parse a duration after one of the labels and return seconds."""

        normalized = " ".join(text.split())
        for label in labels:
            pattern = rf"{re.escape(label)}\s*[:：]?\s*([0-9]+(?::[0-9]{{1,2}}){{0,2}}|[0-9]+(?:\.[0-9]+)?\s*小時|[0-9]+(?:\.[0-9]+)?\s*分鐘)"
            match = re.search(pattern, normalized)
            if match:
                return CourseAutomation._duration_to_seconds(match.group(1))
        return 0

    @staticmethod
    def _duration_to_seconds(raw: str) -> int:
        """Convert common platform duration formats to seconds."""

        value = raw.strip()
        if "小時" in value:
            number = float(re.findall(r"[0-9]+(?:\.[0-9]+)?", value)[0])
            return int(number * 3600)
        if "分鐘" in value:
            number = float(re.findall(r"[0-9]+(?:\.[0-9]+)?", value)[0])
            return int(number * 60)

        parts = [int(part) for part in value.split(":") if part.isdigit()]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 1:
            return parts[0] * 60
        return 0


class PlaybackScheduler:
    """Run selected courses for the remaining required study time."""

    def __init__(
        self,
        page: Page,
        config: AppConfig,
        schedule: PlaybackScheduleConfig,
        stop_event: Event,
        skip_course_event: Event | None = None,
        selectors: SelectorManager | None = None,
    ) -> None:
        self._automation = CourseAutomation(page=page, config=config, selectors=selectors)
        self._schedule = schedule
        self._stop_event = stop_event
        self._skip_course_event = skip_course_event
        self._logger = get_logger(__name__)

    def filter_courses(self, courses: list[CourseInfo]) -> list[CourseInfo]:
        """Return courses selected by config; empty selection means all courses."""

        if not self._schedule.selected_course_titles:
            return courses

        selected: list[CourseInfo] = []
        for expected in self._schedule.selected_course_titles:
            for course in courses:
                if expected in course.title and course not in selected:
                    selected.append(course)
                    break
        return selected

    def run(
        self,
        courses: list[CourseInfo],
        emit_result: Callable[[AutomationResult], None] | None = None,
    ) -> list[AutomationResult]:
        """Run the configured playback queue."""

        planned_courses = self.filter_courses(courses)
        if not planned_courses:
            raise AutomationError("排程設定沒有符合的課程，請檢查 resources/course_schedule.json。")

        results: list[AutomationResult] = []
        seconds_per_chapter = self._schedule.play_minutes * 60
        for course in planned_courses:
            if self._stop_event.is_set():
                break
            if self._skip_course_event is not None:
                self._skip_course_event.clear()

            study_time = self._automation.read_course_study_time(course)
            if self._stop_event.is_set():
                break
            if study_time.required_seconds > 0 and study_time.remaining_seconds <= 0:
                skipped_result = AutomationResult(
                    status=AutomationStatus.COURSE_SKIPPED,
                    message=f"{course.title}：閱讀時數已達標，略過上課。",
                    course_title=course.title,
                )
                results.append(skipped_result)
                self._emit(emit_result, skipped_result)
                continue

            base_remaining = (
                study_time.remaining_seconds
                if study_time.required_seconds > 0
                else seconds_per_chapter
            )
            remaining_for_course = base_remaining + EXTRA_PLAYBACK_SECONDS_PER_COURSE
            self._emit(
                emit_result,
                AutomationResult(
                    status=AutomationStatus.PLAYBACK_WAITING,
                    message=(
                        f"{course.title} 需補足約 {max(1, remaining_for_course // 60)} 分鐘；"
                        f"每章最多播放 {self._schedule.play_minutes} 分鐘，"
                        "已額外加入 5 分鐘緩衝。"
                    ),
                    course_title=course.title,
                    remaining_seconds=remaining_for_course,
                ),
            )

            enter_result = self._automation.enter_course(course)
            enter_result = replace(enter_result, remaining_seconds=remaining_for_course)
            results.append(enter_result)
            self._emit(emit_result, enter_result)

            visited_chapters: set[str] = set()
            while remaining_for_course > 0 and not self._stop_event.is_set() and not self._skip_course_requested():
                chapter = self._automation.select_playable_chapter(visited_chapters)
                visited_chapters.add(chapter.title)
                chapter_result = AutomationResult(
                    status=AutomationStatus.CHAPTER_SELECTED,
                    message=f"已選擇章節：{chapter.title}",
                    course_title=course.title,
                    chapter_title=chapter.title,
                    current_url=enter_result.current_url,
                    player_frame_url=chapter.frame_url,
                    remaining_seconds=remaining_for_course,
                )
                results.append(chapter_result)
                self._emit(emit_result, chapter_result)

                raw_play_result = self._automation.start_current_video()
                segment_seconds = min(seconds_per_chapter, remaining_for_course)
                play_result = AutomationResult(
                    status=raw_play_result.status,
                    message=f"{course.title} / {chapter.title}：播放 {max(1, segment_seconds // 60)} 分鐘。",
                    course_title=course.title,
                    chapter_title=chapter.title,
                    current_url=raw_play_result.current_url,
                    page_title=raw_play_result.page_title,
                    frame_urls=raw_play_result.frame_urls,
                    video_count=raw_play_result.video_count,
                    player_frame_url=raw_play_result.player_frame_url,
                    remaining_seconds=remaining_for_course,
                )
                results.append(play_result)
                self._emit(emit_result, play_result)

                elapsed_seconds = self._wait_for_course_duration(
                    course.title,
                    chapter.title,
                    segment_seconds,
                    remaining_for_course,
                    emit_result,
                )
                remaining_for_course = max(0, remaining_for_course - elapsed_seconds)

            if self._skip_course_requested():
                self._emit(
                    emit_result,
                    AutomationResult(
                        status=AutomationStatus.PLAYBACK_WAITING,
                        message=f"已跳過目前課程：{course.title}",
                        course_title=course.title,
                        remaining_seconds=remaining_for_course,
                    ),
                )

        if self._stop_event.is_set():
            stopped = self._automation.return_to_course_dashboard()
            results.append(stopped)
            self._emit(emit_result, stopped)
            return results

        finished = AutomationResult(
            status=AutomationStatus.PLAYBACK_FINISHED,
            message="排程播放完成。",
        )
        results.append(finished)
        self._emit(emit_result, finished)
        return results

    def _wait_for_course_duration(
        self,
        title: str,
        chapter_title: str,
        total_seconds: int,
        course_remaining_at_start: int,
        emit_result: Callable[[AutomationResult], None] | None,
    ) -> int:
        """Emit a GUI-only per-second course countdown while allowing cancellation."""

        started_at = time.monotonic()
        while not self._stop_event.is_set() and not self._skip_course_requested():
            elapsed = min(total_seconds, max(0, int(time.monotonic() - started_at)))
            segment_remaining = max(0, total_seconds - elapsed)
            course_remaining = max(0, course_remaining_at_start - elapsed)
            confirmed_count = self._automation.confirm_idle_reminders()
            if confirmed_count:
                self._emit(
                    emit_result,
                    AutomationResult(
                        status=AutomationStatus.PLAYBACK_WAITING,
                        message=f"已確認 {confirmed_count} 個閱讀閒置提醒，繼續閱讀。",
                        course_title=title,
                        chapter_title=chapter_title,
                        remaining_seconds=course_remaining,
                    ),
                )
            else:
                self._emit(
                    emit_result,
                    AutomationResult(
                        status=AutomationStatus.PLAYBACK_WAITING,
                        message=f"{title} 上課中",
                        course_title=title,
                        chapter_title=chapter_title,
                        remaining_seconds=course_remaining,
                    ),
                )
            if segment_remaining <= 0:
                return elapsed
            time.sleep(1)

        return min(total_seconds, max(0, int(time.monotonic() - started_at)))

    def _skip_course_requested(self) -> bool:
        """Return True when the GUI asked to skip the current queued course."""

        return self._skip_course_event is not None and self._skip_course_event.is_set()

    @staticmethod
    def _emit(
        emit_result: Callable[[AutomationResult], None] | None,
        result: AutomationResult,
    ) -> None:
        """Emit progress when a GUI callback is provided."""

        if emit_result is not None:
            emit_result(result)
