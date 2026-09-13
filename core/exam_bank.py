"""Exam bank retrieval and parsing from roddayeye (永無止盡的學習路).

This service queries the local index of ~6,800 courses from roddayeye,
fetches the official answer key, and parses it into structured questions
and answers for automated assessment completion.
"""

from __future__ import annotations

import html
import json
import re
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.config import AppConfig
from core.logger import get_logger


def _get_ssl_context() -> ssl.SSLContext:
    """Create an SSL context supporting macOS default environments without CA cert errors."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl._create_unverified_context()
    except Exception:
        return ssl.create_default_context()



class ExamBankError(RuntimeError):
    """Raised when course answers cannot be found or parsed."""


@dataclass(frozen=True, slots=True)
class ExamQuestion:
    """Parsed question with correct answer strings."""

    question: str
    correct_answers: tuple[str, ...]
    is_true_false: bool = False
    is_multi: bool = False


class ExamBankService:
    """Lookup and fetch course answers from roddayeye blog."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._logger = get_logger(__name__)
        self._index: dict[str, str] = {}
        self._normalized_index: dict[str, str] = {}
        self._overrides: dict[str, list[str]] = {}
        self._load_index()
        self._load_overrides()

    def _load_overrides(self) -> None:
        """Load locally verified answer corrections to fix errors or typos in blog answer keys."""
        overrides_path = self._config.resources_dir / "exam_bank_overrides.json"
        if not overrides_path.exists():
            return
        try:
            data = json.loads(overrides_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._overrides = {
                    self._normalize_title(k): v if isinstance(v, list) else [str(v)]
                    for k, v in data.items()
                }
                self._logger.info("已載入 %d 筆題庫勘誤修正檔。", len(self._overrides))
        except Exception as exc:
            self._logger.exception("載入題庫勘誤失敗：%s", exc)


    def _load_index(self) -> None:
        """Load the pre-indexed title-to-URL mapping."""

        index_path = self._config.resources_dir / "exam_bank_index.json"
        if not index_path.exists():
            self._logger.warning("題庫索引檔不存在：%s", index_path)
            return

        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._index = data
                self._normalized_index = {
                    self._normalize_title(k): v for k, v in data.items()
                }
                self._logger.info("已載入題庫索引，共 %d 門課程。", len(self._index))
        except Exception as exc:
            self._logger.exception("載入題庫索引失敗：%s", exc)

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a course title by removing brackets, punctuation, whitespace, and tags."""

        cleaned = re.sub(r"[《》〈〉（）()【】\[\]\s\-.,:：，。解答題庫]+", "", title)
        return cleaned.strip()

    def find_article_url(self, course_title: str) -> str | None:
        """Find the corresponding blog article URL for a course title."""

        if course_title in self._index:
            return self._index[course_title]

        norm_query = self._normalize_title(course_title)
        if not norm_query:
            return None

        # 1. Exact normalized match
        if norm_query in self._normalized_index:
            return self._normalized_index[norm_query]

        # 2. Substring matching (prefer longer matches)
        best_match_url: str | None = None
        best_match_len = 0

        for key, url in self._normalized_index.items():
            if key in norm_query or norm_query in key:
                common_len = min(len(key), len(norm_query))
                if common_len > best_match_len:
                    best_match_len = common_len
                    best_match_url = url

        return best_match_url

    def fetch_exam_answers(self, course_title: str) -> list[ExamQuestion]:
        """Fetch and parse questions and correct answers for a course."""

        url = self.find_article_url(course_title)
        if not url:
            raise ExamBankError(f"題庫中尚未收錄課程「{course_title}」的解答。")

        self._logger.info("正在自題庫抓取「%s」解答：%s", course_title, url)
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                },
            )
            ssl_ctx = _get_ssl_context()
            with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
                raw_html = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            self._logger.exception("抓取題庫網頁失敗：%s", exc)
            raise ExamBankError(f"連線題庫網頁失敗：{exc}") from exc

        questions = self._parse_article_html(raw_html)
        if not questions:
            raise ExamBankError(f"在題庫網頁中找不到解析結果：{url}")

        questions = self._apply_overrides(questions)
        self._logger.info("成功自題庫解析出 %d 題解答。", len(questions))
        return questions

    def _apply_overrides(self, questions: list[ExamQuestion]) -> list[ExamQuestion]:
        """Override questions with verified answer corrections."""
        if not self._overrides:
            return questions

        updated: list[ExamQuestion] = []
        for q in questions:
            norm_q = self._normalize_title(q.question)
            matched_override = None
            for ov_key, ov_answers in self._overrides.items():
                if ov_key == norm_q or (len(ov_key) >= 6 and (ov_key in norm_q or norm_q in ov_key)):
                    matched_override = ov_answers
                    break
            if matched_override:
                self._logger.info(
                    "已套用題庫勘誤修正：題目「%s」答案由 %s 修正為 %s",
                    q.question,
                    q.correct_answers,
                    matched_override,
                )
                updated.append(
                    ExamQuestion(
                        question=q.question,
                        correct_answers=tuple(matched_override),
                        is_true_false=q.is_true_false,
                        is_multi=len(matched_override) > 1,
                    )
                )
            else:
                updated.append(q)
        return updated


    @classmethod
    def _parse_article_html(cls, raw_html: str) -> list[ExamQuestion]:
        """Extract structured questions and answers from table rows in raw HTML."""

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", raw_html, re.DOTALL)
        if not rows:
            return []

        questions: list[ExamQuestion] = []
        cur_q_text: str | None = None
        cur_options: list[tuple[str, str]] = []

        def finish_current_question():
            nonlocal cur_q_text, cur_options
            if not cur_q_text or not cur_options:
                return

            correct_answers: list[str] = []
            opt_texts = [text for _, text in cur_options]
            is_tf = any(t in {"○", "╳", "O", "X", "是", "否"} for t in opt_texts)

            for marker, text in cur_options:
                is_checked = "v" in marker.lower() or marker in {"○", "O"}
                if is_checked:
                    if is_tf:
                        if text in {"○", "O", "是"} or marker in {"○", "O"}:
                            correct_answers.append("是")
                        elif text in {"╳", "X", "否"}:
                            correct_answers.append("否")
                        else:
                            correct_answers.append(text)
                    else:
                        correct_answers.append(text)

            is_multi = len(correct_answers) > 1
            questions.append(
                ExamQuestion(
                    question=cur_q_text,
                    correct_answers=tuple(correct_answers),
                    is_true_false=is_tf,
                    is_multi=is_multi,
                )
            )
            cur_q_text = None
            cur_options = []

        for row_html in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
            if not tds:
                continue

            cleaned_cells = [
                html.unescape(re.sub(r"<[^>]+>", "", td)).strip().replace("\xa0", " ").strip()
                for td in tds
            ]
            first_col = cleaned_cells[0] if len(cleaned_cells) > 0 else ""
            last_col = cleaned_cells[-1] if len(cleaned_cells) > 0 else ""

            # Filter out author credit watermark row (e.g. 'roddayeye' or 'r.o.d.d.a.y.e.y.e.')
            clean_wm = re.sub(r"[^a-zA-Z]+", "", last_col).lower()
            if "roddayeye" in clean_wm:
                continue

            if first_col.upper() == "Q":
                finish_current_question()
                cur_q_text = last_col
            elif cur_q_text is not None and last_col:
                cur_options.append((first_col, last_col))

        finish_current_question()
        return questions
