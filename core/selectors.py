"""Centralized website selector management.

All Playwright selectors used by browser, login, course scanning, and future
automation modules are registered here.  Keeping selectors in one place makes
site changes easier to repair and prevents page logic from being scattered
through the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class SelectorStrategy(str, Enum):
    """Supported strategies for locating elements with Playwright."""

    CSS = "css"
    TEXT = "text"
    ROLE = "role"
    LABEL = "label"
    PLACEHOLDER = "placeholder"
    XPATH = "xpath"


class SelectorGroup(str, Enum):
    """Logical selector groups used by each application module."""

    HOME = "home"
    LOGIN = "login"
    PROFILE = "profile"
    COURSE = "course"
    PLAYER = "player"
    POPUP = "popup"
    IFRAME = "iframe"
    AUTOMATION = "automation"
    ASSESSMENT = "assessment"
    SURVEY = "survey"


@dataclass(frozen=True, slots=True)
class Selector:
    """A selector definition consumed by Playwright services."""

    key: str
    value: str
    group: SelectorGroup
    strategy: SelectorStrategy = SelectorStrategy.CSS
    description: str = ""
    role_name: str | None = None
    timeout_ms: int = 15_000


class SelectorNotFoundError(KeyError):
    """Raised when a selector key is not registered."""


class SelectorManager:
    """Registry and lookup helper for all website selectors."""

    def __init__(self) -> None:
        self._selectors: dict[str, Selector] = dict(DEFAULT_SELECTORS)

    def get(self, key: str) -> Selector:
        """Return a selector by key, or raise a readable error."""

        try:
            return self._selectors[key]
        except KeyError as exc:
            raise SelectorNotFoundError(f"Selector not found: {key}") from exc

    def all(self) -> tuple[Selector, ...]:
        """Return all registered selectors."""

        return tuple(self._selectors.values())

    def by_group(self, group: SelectorGroup) -> tuple[Selector, ...]:
        """Return selectors that belong to a specific module group."""

        return tuple(selector for selector in self._selectors.values() if selector.group == group)

    def register(self, selector: Selector, overwrite: bool = False) -> None:
        """Register a new selector, optionally replacing an existing one."""

        if selector.key in self._selectors and not overwrite:
            raise ValueError(f"Selector already exists: {selector.key}")
        self._selectors[selector.key] = selector


DEFAULT_SELECTORS: Final[dict[str, Selector]] = {
    "home.ecpa_login": Selector(
        key="home.ecpa_login",
        value="a#login_md, a.login-btn[href='/mooc/co_login_dialog.php']",
        group=SelectorGroup.HOME,
        description="Homepage login entry.",
    ),
    "home.personal_area": Selector(
        key="home.personal_area",
        value="個人專區",
        group=SelectorGroup.HOME,
        strategy=SelectorStrategy.TEXT,
        description="Personal area entry shown after login.",
    ),
    "login.busy_confirm": Selector(
        key="login.busy_confirm",
        value="了解，我清楚了",
        group=SelectorGroup.LOGIN,
        strategy=SelectorStrategy.TEXT,
        description="Busy-site confirmation button that can appear before eCPA login.",
        timeout_ms=5_000,
    ),
    "login.ecpa_provider": Selector(
        key="login.ecpa_provider",
        value="人事服務網eCPA",
        group=SelectorGroup.LOGIN,
        strategy=SelectorStrategy.TEXT,
        description="eCPA identity-provider entry on the login dialog.",
        timeout_ms=10_000,
    ),
    "login.account_password_mode": Selector(
        key="login.account_password_mode",
        value="帳號密碼登入",
        group=SelectorGroup.LOGIN,
        strategy=SelectorStrategy.TEXT,
        description="Account/password login mode on eCPA pages.",
        timeout_ms=10_000,
    ),
    "login.account": Selector(
        key="login.account",
        value="#aliasid",
        group=SelectorGroup.LOGIN,
        description="eCPA account input.",
    ),
    "login.password": Selector(
        key="login.password",
        value="#pas",
        group=SelectorGroup.LOGIN,
        description="eCPA password input.",
    ),
    "login.submit": Selector(
        key="login.submit",
        value="button[onclick='startIdPas()']",
        group=SelectorGroup.LOGIN,
        description="eCPA account/password submit button.",
    ),
    "profile.page_marker": Selector(
        key="profile.page_marker",
        value="個人專區",
        group=SelectorGroup.PROFILE,
        strategy=SelectorStrategy.TEXT,
        description="Text marker used to verify personal dashboard pages.",
    ),
    "course.unfinished_items": Selector(
        key="course.unfinished_items",
        value="[data-course-status='unfinished']",
        group=SelectorGroup.COURSE,
        description="Reserved selector for a future normalized unfinished-course marker.",
    ),
    "course.list_container": Selector(
        key="course.list_container",
        value=".course-list-container",
        group=SelectorGroup.COURSE,
        description="Course list container in the personal dashboard.",
    ),
    "course.list_item": Selector(
        key="course.list_item",
        value=".course-list-block",
        group=SelectorGroup.COURSE,
        description="Single course card in the personal dashboard.",
    ),
    "course.title": Selector(
        key="course.title",
        value=".course-list-block-info-name",
        group=SelectorGroup.COURSE,
        description="Course title area.",
    ),
    "course.type": Selector(
        key="course.type",
        value=".course-list-block-courseType",
        group=SelectorGroup.COURSE,
        description="Course type label.",
    ),
    "course.pass_info": Selector(
        key="course.pass_info",
        value=".course-list-block-info-pass",
        group=SelectorGroup.COURSE,
        description="Course exam, survey, and certification-hours information.",
    ),
    "course.action": Selector(
        key="course.action",
        value=".course-list-block-function",
        group=SelectorGroup.COURSE,
        description="Course action area.",
    ),
    "course.empty_message": Selector(
        key="course.empty_message",
        value=".course-list-block-sysInfo, .system-message, .alert",
        group=SelectorGroup.COURSE,
        description="Message shown when the course list is empty or unavailable.",
    ),
    "automation.course_entry_button": Selector(
        key="automation.course_entry_button",
        value="button.btnAction[onclick^='gotoCourse'], input.btnAction[onclick^='gotoCourse']",
        group=SelectorGroup.AUTOMATION,
        description="Primary button that enters the selected course player.",
        timeout_ms=30_000,
    ),
    "course.detail_body": Selector(
        key="course.detail_body",
        value="body",
        group=SelectorGroup.COURSE,
        description="Course detail page text used for status-only inspection.",
        timeout_ms=15_000,
    ),
    "assessment.entry_link": Selector(
        key="assessment.entry_link",
        value="a#SYS_04_02_002, a[href*='/learn/exam/exam_list.php']",
        group=SelectorGroup.ASSESSMENT,
        description="Course-player link that opens the assessment list without opening questions.",
        timeout_ms=15_000,
    ),
    "assessment.list_body": Selector(
        key="assessment.list_body",
        value="body",
        group=SelectorGroup.ASSESSMENT,
        description="Assessment-list page text used only for completion-status inspection.",
        timeout_ms=15_000,
    ),
    "assessment.proceed_button": Selector(
        key="assessment.proceed_button",
        value="div.main-text:has-text('進行測驗')",
        group=SelectorGroup.ASSESSMENT,
        description="Assessment-list control that opens the pre-attempt page.",
        timeout_ms=20_000,
    ),
    "assessment.start_attempt": Selector(
        key="assessment.start_attempt",
        value="input.cssBtn[type='button'][value='開始作答'][onclick*='examBegin']",
        group=SelectorGroup.ASSESSMENT,
        description="Pre-attempt button that opens the user-controlled question page.",
        timeout_ms=20_000,
    ),
    "assessment.leave_course_button": Selector(
        key="assessment.leave_course_button",
        value=(
            "a:has-text('離開課程'), button:has-text('離開課程'), "
            "input[value*='離開課程']"
        ),
        group=SelectorGroup.ASSESSMENT,
        description="Course-player exit control used after its assessment popup is open.",
        timeout_ms=10_000,
    ),
    "assessment.answer_control": Selector(
        key="assessment.answer_control",
        value="input[type='radio'], input[type='checkbox'], select",
        group=SelectorGroup.ASSESSMENT,
        description="Answer controls used only to fill user-provided clipboard answers without submitting.",
        timeout_ms=15_000,
    ),
    "assessment.submit_button": Selector(
        key="assessment.submit_button",
        value="input[type='submit'][value*='送出答案'], input[type='submit'][value*='送出']",
        group=SelectorGroup.ASSESSMENT,
        description="Final assessment submission control, used only after an explicit GUI confirmation.",
        timeout_ms=15_000,
    ),
    "assessment.question_rows": Selector(
        key="assessment.question_rows",
        value="tr.bg03, tr.bg04",
        group=SelectorGroup.ASSESSMENT,
        description="Question rows in the platform assessment table.",
        timeout_ms=15_000,
    ),
    "player.main_frame": Selector(
        key="player.main_frame",
        value="iframe[src*='/online/online.php'], iframe[src*='/learn/'], frame[src*='/online/online.php'], frame[src*='/learn/']",
        group=SelectorGroup.PLAYER,
        description="Main course player iframe after entering a course.",
        timeout_ms=30_000,
    ),
    "player.iframe": Selector(
        key="player.iframe",
        value="iframe, frame",
        group=SelectorGroup.IFRAME,
        description="Reserved frame selector for later course player automation.",
    ),
    "player.launch_activity": Selector(
        key="player.launch_activity",
        value="a[onclick*='launchActivity'], [onclick*='launchActivity']",
        group=SelectorGroup.PLAYER,
        description="SCORM chapter links that call launchActivity.",
    ),
    "player.legacy_start": Selector(
        key="player.legacy_start",
        value="a[href*='/learn/path/launch'], a[href*='launch.htm'], a[href*='launch.php']",
        group=SelectorGroup.PLAYER,
        description="Legacy frameset start-course links.",
    ),
    "player.media": Selector(
        key="player.media",
        value="video, object, embed",
        group=SelectorGroup.PLAYER,
        description="Media-like content inside a lesson frame.",
    ),
    "popup.close": Selector(
        key="popup.close",
        value="button[aria-label='Close'], .modal button.close, .fancybox-close-small, [data-fancybox-close]",
        group=SelectorGroup.POPUP,
        description="Reserved popup close selector for later automation.",
    ),
    "player.idle_reminder_confirm": Selector(
        key="player.idle_reminder_confirm",
        value=(
            "#div_auto_logout input[type='button'][value='確定'], "
            ".blockUI.blockMsg #div_auto_logout input[type='button'][value='確定']"
        ),
        group=SelectorGroup.PLAYER,
        description="Confirmation button inside the course reading-idle reminder overlay.",
        timeout_ms=2_000,
    ),
    "survey.entry_link": Selector(
        key="survey.entry_link",
        value="a#SYS_04_02_003, a[href*='/learn/questionnaire/questionnaire_list.php']",
        group=SelectorGroup.SURVEY,
        description="Course-player menu link for questionnaire/evaluation.",
        timeout_ms=15_000,
    ),
    "survey.fill_button": Selector(
        key="survey.fill_button",
        value=".main-text, a, button, input[type='button'], input[type='submit']",
        group=SelectorGroup.SURVEY,
        description="Questionnaire-list entry that opens the answer form.",
        timeout_ms=15_000,
    ),
    "survey.submit_button": Selector(
        key="survey.submit_button",
        value="input[type='submit'][value*='確定繳交'], input.cssBtn[value*='確定'], button[type='submit']",
        group=SelectorGroup.SURVEY,
        description="Questionnaire form submit button.",
        timeout_ms=15_000,
    ),
    "survey.rating_input": Selector(
        key="survey.rating_input",
        value="input.rating-input",
        group=SelectorGroup.SURVEY,
        description="Optional post-survey course rating input.",
        timeout_ms=5_000,
    ),
    "survey.rating_button": Selector(
        key="survey.rating_button",
        value="[onclick*='doRating']",
        group=SelectorGroup.SURVEY,
        description="Optional post-survey course rating button.",
        timeout_ms=5_000,
    ),
}
