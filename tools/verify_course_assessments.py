"""Run a real-session course-access and assessment-list verification.

This diagnostic reuses the persistent Chrome profile.  It opens course player
shells and assessment list pages only; it never opens questions, changes an
answer, or submits an assessment.
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.browser import BrowserController  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.course import CourseScanner  # noqa: E402
from core.login import LoginService  # noqa: E402
from core.logger import setup_logging  # noqa: E402


def main() -> int:
    """Verify every unfinished course using the saved login session."""

    config = AppConfig.load()
    config.ensure_directories()
    setup_logging(config)
    browser = BrowserController(config)
    try:
        browser.start()
        browser.open_home()
        login = LoginService(page=browser.page, config=config)
        if not login.is_logged_in():
            print("VERIFY_ERROR: persistent Session is not logged in.", flush=True)
            return 2

        login.open_personal_area("診斷工具已進入個人專區")
        scanner = CourseScanner(page=browser.page, config=config)
        courses = scanner.scan_unanswered_assessment_courses()
        print(f"VERIFY_RESULT_COUNT={len(courses)}", flush=True)
        for index, course in enumerate(courses, start=1):
            print(
                f"VERIFY_RESULT_{index}={course.title} | "
                f"can_attend={course.can_attend} | "
                f"assessment={course.assessment_status} | "
                f"note={course.inspection_note}",
                flush=True,
            )
        return 0
    finally:
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
