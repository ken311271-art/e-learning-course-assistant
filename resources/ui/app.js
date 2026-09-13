"use strict";

const state = {
  bridge: null,
  busy: false,
  busyAction: "",
  browserRunning: false,
  loggedIn: false,
  studyCourses: [],
  selectedStudy: -1,
  studyChecked: new Set(),
  currentStudyCourse: "",
  countdownCourse: "",
  countdownSeconds: null,
  assessmentCourses: [],
  selectedAssessment: -1,
  conditions: [],
  enrollmentCourses: [],
  enrollmentChecked: new Set(),
  selectedCondition: -1,
  logCount: 0,
};

const pageTitles = {
  login: "登入",
  study: "上課",
  enrollment: "選課",
  survey: "問卷",
  assessment: "測驗",
};

const byId = (id) => document.getElementById(id);

function createElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function formatDuration(rawSeconds) {
  const seconds = Math.max(0, Number.parseInt(rawSeconds || 0, 10));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return [hours, minutes, remainder].map((part) => String(part).padStart(2, "0")).join(":");
}

function updateStudyCountdown(seconds, courseTitle = "", forceZero = false) {
  const parsed = Number(seconds);
  if (!Number.isFinite(parsed) || (parsed <= 0 && !forceZero)) return;
  state.countdownSeconds = Math.max(0, Math.floor(parsed));
  if (courseTitle) state.countdownCourse = courseTitle;
  byId("study-countdown").textContent = formatDuration(state.countdownSeconds);
  byId("countdown-course").textContent = state.countdownCourse || "尚未開始";
}

function showToast(message) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 4200);
}

function appendClientLog(message, level = "info") {
  appendLog({ message, level, created_at: new Date().toLocaleTimeString("zh-TW", { hour12: false }) });
}

function appendLog(payload) {
  const content = byId("log-content");
  const line = createElement("div", `log-line ${payload.level || "info"}`);
  const time = createElement("time", "", payload.created_at || "");
  const message = createElement("span", "", payload.message || "");
  line.append(time, message);
  content.appendChild(line);
  while (content.children.length > 500) content.firstElementChild.remove();
  content.scrollTop = content.scrollHeight;
  state.logCount += 1;
  byId("log-count").textContent = `${state.logCount} 筆`;
}

function callBridge(method, ...args) {
  if (!state.bridge || typeof state.bridge[method] !== "function") {
    showToast("介面尚未連接背景服務，請稍候。 ");
    return;
  }
  state.bridge[method](...args);
}

function switchPage(pageName) {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === pageName);
  });
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
  byId(`page-${pageName}`).classList.add("active");
  byId("page-title").textContent = pageTitles[pageName];
}

function setBadge(element, text, kind = "neutral") {
  element.textContent = text;
  element.className = `status-badge ${kind}`;
}

function updateSystemStatus() {
  const browserBadge = byId("browser-badge");
  const loginBadge = byId("login-badge");
  const sessionState = byId("session-state");
  const sidebarDot = byId("sidebar-dot");

  if (state.busy) {
    setBadge(browserBadge, "執行中", "busy");
    sessionState.textContent = "執行中";
    sessionState.className = "inline-state busy";
  } else if (state.browserRunning) {
    setBadge(browserBadge, "瀏覽器已啟動", "ready");
    sessionState.textContent = "已啟動";
    sessionState.className = "inline-state ready";
  } else {
    setBadge(browserBadge, "瀏覽器未啟動", "neutral");
    sessionState.textContent = "尚未啟動";
    sessionState.className = "inline-state";
  }

  setBadge(loginBadge, state.loggedIn ? "已登入" : "尚未登入", state.loggedIn ? "ready" : "neutral");
  sidebarDot.classList.toggle("ready", state.browserRunning);
  byId("sidebar-state-text").textContent = state.browserRunning ? "瀏覽器已連線" : "瀏覽器未啟動";
}

function updateControls() {
  const idle = !state.busy;
  const browserReady = state.browserRunning && idle;
  const loginReady = state.loggedIn && idle;

  byId("start-browser-button").disabled = state.browserRunning || state.busy;
  byId("close-browser-button").disabled = !state.browserRunning || state.busy;
  byId("check-login-button").disabled = !browserReady;
  byId("login-button").disabled = !browserReady;

  byId("scan-courses-button").disabled = !loginReady;
  byId("enter-course-button").disabled = !loginReady || state.selectedStudy < 0;
  byId("inspect-player-button").disabled = !loginReady;
  byId("playback-button").disabled = !loginReady || state.studyChecked.size === 0;
  byId("skip-course-button").disabled = !(state.busy && state.busyAction === "runPlayback");
  byId("stop-playback-button").disabled = !(state.busy && state.busyAction === "runPlayback");

  byId("add-condition-button").disabled = state.busy;
  byId("remove-condition-button").disabled = state.busy || state.selectedCondition < 0;
  const enabledConditions = state.conditions.filter((item) => item.enabled);
  byId("search-enrollment-button").disabled = !loginReady || enabledConditions.length === 0;
  byId("enroll-button").disabled = !loginReady || state.enrollmentChecked.size === 0;

  byId("survey-button").disabled = !loginReady;
  byId("enter-assessment-button").disabled = !loginReady || state.selectedAssessment < 0;
  byId("auto-answer-button").disabled = !loginReady || state.selectedAssessment < 0 || state.busy;
  byId("fill-assessment-button").disabled = !loginReady;
  byId("submit-assessment-button").disabled = !loginReady;
  updateSystemStatus();
}

function renderStudyCourses() {
  const body = byId("study-course-body");
  body.replaceChildren();
  byId("course-count").textContent = String(state.studyCourses.length);
  if (!state.studyCourses.length) {
    const row = createElement("tr", "empty-row");
    const cell = createElement("td", "", "目前沒有未完成課程");
    cell.colSpan = 8;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  state.studyCourses.forEach((course, index) => {
    const remainingSeconds = Number(course.remaining_reading_seconds || 0);
    const canSchedule = remainingSeconds > 0;
    const rowClasses = [];
    if (index === state.selectedStudy) rowClasses.push("selected");
    if (course.title === state.currentStudyCourse) rowClasses.push("running");
    if (!canSchedule) rowClasses.push("completed");
    const row = createElement("tr", rowClasses.join(" "));
    const checkCell = createElement("td", "check-cell");
    const checkbox = createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.studyChecked.has(index);
    checkbox.disabled = !canSchedule || state.busy;
    checkbox.title = canSchedule ? "加入上課排程" : "閱讀時數已達標";
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.studyChecked.add(index);
      else state.studyChecked.delete(index);
      byId("study-selection-label").textContent = `已勾選 ${state.studyChecked.size} 門`;
      updateControls();
    });
    checkCell.appendChild(checkbox);
    row.appendChild(checkCell);
    row.appendChild(createElement("td", "", String(index + 1)));
    row.appendChild(createElement("td", "title-cell", course.title || "-"));
    const hasReadingTime = Number(course.required_reading_seconds || 0) > 0;
    row.appendChild(createElement("td", "", hasReadingTime ? formatDuration(course.required_reading_seconds) : "-"));
    row.appendChild(createElement("td", "", hasReadingTime ? formatDuration(course.studied_reading_seconds) : "-"));
    row.appendChild(createElement("td", "", hasReadingTime ? formatDuration(course.remaining_reading_seconds) : "-"));
    row.appendChild(createElement("td", "", course.exam_score || "-"));
    row.appendChild(createElement("td", "", course.survey_status || "-"));
    row.addEventListener("click", () => {
      state.selectedStudy = index;
      byId("study-selection-label").textContent = course.title;
      renderStudyCourses();
      updateControls();
    });
    body.appendChild(row);
  });
}

function renderAssessmentCourses() {
  const body = byId("assessment-body");
  body.replaceChildren();
  byId("assessment-count").textContent = String(state.assessmentCourses.length);
  if (!state.assessmentCourses.length) {
    const row = createElement("tr", "empty-row");
    const cell = createElement("td", "", "目前沒有找到未答測驗課程");
    cell.colSpan = 5;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  state.assessmentCourses.forEach((course, index) => {
    const row = createElement("tr", index === state.selectedAssessment ? "selected" : "");
    row.appendChild(createElement("td", "", String(index + 1)));
    row.appendChild(createElement("td", "title-cell", course.title || "-"));
    row.appendChild(createElement("td", "", course.can_attend === false ? "無法上課" : "可上課"));
    row.appendChild(createElement("td", "", course.assessment_status || course.exam_score || "未提供"));
    row.appendChild(createElement("td", "", course.inspection_note || "-"));
    row.addEventListener("click", () => {
      state.selectedAssessment = index;
      byId("assessment-selection-label").textContent = course.title;
      renderAssessmentCourses();
      updateControls();
    });
    body.appendChild(row);
  });
}

function renderConditions() {
  const body = byId("condition-body");
  body.replaceChildren();
  byId("condition-count-label").textContent = `${state.conditions.length} 筆`;
  if (!state.conditions.length) {
    const row = createElement("tr", "empty-row");
    const cell = createElement("td", "", "尚未加入選課條件");
    cell.colSpan = 5;
    row.appendChild(cell);
    body.appendChild(row);
    updateControls();
    return;
  }

  state.conditions.forEach((condition, index) => {
    const row = createElement("tr", index === state.selectedCondition ? "selected" : "");
    const checkCell = createElement("td", "check-cell");
    const checkbox = createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = condition.enabled;
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      condition.enabled = checkbox.checked;
      updateControls();
    });
    checkCell.appendChild(checkbox);
    row.appendChild(checkCell);
    row.appendChild(createElement("td", "title-cell", condition.keyword));
    row.appendChild(createElement("td", "", condition.count));
    row.appendChild(createElement("td", "", condition.minimum));
    row.appendChild(createElement("td", "", condition.maximum));
    row.addEventListener("click", () => {
      state.selectedCondition = index;
      renderConditions();
      updateControls();
    });
    body.appendChild(row);
  });
  updateControls();
}

function addCondition() {
  const keywordInput = byId("condition-keyword");
  const keyword = keywordInput.value.trim();
  if (!keyword) {
    showToast("請先輸入課程關鍵字。 ");
    keywordInput.focus();
    return false;
  }
  state.conditions.push({
    enabled: true,
    keyword,
    count: String(Math.max(1, Number.parseInt(byId("condition-count").value || "1", 10))),
    minimum: byId("condition-hours-min").value || "1",
    maximum: byId("condition-hours-max").value || "3",
  });
  keywordInput.value = "";
  state.selectedCondition = state.conditions.length - 1;
  renderConditions();
  appendClientLog(`已加入選課條件：${keyword}`);
  keywordInput.focus();
  return true;
}

function removeCondition() {
  if (state.selectedCondition < 0) return;
  const removed = state.conditions.splice(state.selectedCondition, 1)[0];
  state.selectedCondition = -1;
  renderConditions();
  appendClientLog(`已刪除選課條件：${removed.keyword}`);
}

function showEnrollmentResults() {
  byId("enrollment-conditions-view").classList.add("hidden");
  byId("enrollment-results-view").classList.remove("hidden");
  byId("back-to-conditions-button").classList.remove("hidden");
  byId("enrollment-subtitle").textContent = "可勾選多門課程後一次報名。";
}

function showEnrollmentConditions() {
  byId("enrollment-results-view").classList.add("hidden");
  byId("enrollment-conditions-view").classList.remove("hidden");
  byId("back-to-conditions-button").classList.add("hidden");
  byId("enrollment-subtitle").textContent = "建立搜尋條件後前往選課中心查詢。";
}

function searchEnrollment() {
  if (!state.conditions.length && byId("condition-keyword").value.trim()) addCondition();
  const lines = state.conditions
    .filter((condition) => condition.enabled)
    .map((condition) => [condition.keyword, condition.count, condition.minimum, condition.maximum].join(","));
  if (!lines.length) {
    showToast("請先加入至少一筆啟用中的選課條件。 ");
    return;
  }
  state.enrollmentCourses = [];
  state.enrollmentChecked.clear();
  showEnrollmentResults();
  const body = byId("enrollment-result-body");
  body.innerHTML = '<tr class="empty-row"><td colspan="6">正在搜尋課程...</td></tr>';
  byId("enrollment-result-count").textContent = "搜尋中";
  callBridge("searchEnrollment", lines.join("\n"));
}

function renderEnrollmentResults() {
  const body = byId("enrollment-result-body");
  body.replaceChildren();
  byId("enrollment-result-count").textContent = `${state.enrollmentCourses.length} 門`;
  if (!state.enrollmentCourses.length) {
    const row = createElement("tr", "empty-row");
    const cell = createElement("td", "", "沒有找到可報名課程");
    cell.colSpan = 6;
    row.appendChild(cell);
    body.appendChild(row);
    updateControls();
    return;
  }

  state.enrollmentCourses.forEach((course, index) => {
    const row = createElement("tr");
    const checkCell = createElement("td", "check-cell");
    const checkbox = createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.enrollmentChecked.has(index);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.enrollmentChecked.add(index);
      else state.enrollmentChecked.delete(index);
      updateControls();
    });
    checkCell.appendChild(checkbox);
    row.appendChild(checkCell);
    row.appendChild(createElement("td", "", course.keyword || "-"));
    row.appendChild(createElement("td", "title-cell", course.title || "-"));
    row.appendChild(createElement("td", "", course.hours || "-"));
    row.appendChild(createElement("td", "status-cell", course.status || "-"));
    row.appendChild(createElement("td", "", course.course_id || "-"));
    row.addEventListener("click", (event) => {
      if (event.target === checkbox) return;
      checkbox.checked = !checkbox.checked;
      checkbox.dispatchEvent(new Event("change"));
    });
    body.appendChild(row);
  });
  updateControls();
}

function enrollSelected() {
  const indexes = [...state.enrollmentChecked].sort((left, right) => left - right);
  if (!indexes.length) {
    showToast("請先勾選至少一門要報名的課程。 ");
    return;
  }
  if (!window.confirm(`確定要報名 ${indexes.length} 門課程？`)) return;
  callBridge("enrollSelected", JSON.stringify(indexes));
}

function updateEnrollmentResults(results) {
  const resultById = new Map(results.map((result) => [result.course.course_id, result]));
  state.enrollmentCourses.forEach((course) => {
    const result = resultById.get(course.course_id);
    if (result) course.status = result.success ? "已報名" : `失敗：${result.message}`;
  });
  renderEnrollmentResults();
}

function renderSurveyResult(result) {
  byId("survey-state").textContent = result.message || "處理中";
  ["scanned", "eligible", "filled", "skipped", "failed"].forEach((name) => {
    const value = result[`${name}_count`];
    if (typeof value === "number") byId(`survey-${name}`).textContent = String(value);
  });
  const activity = byId("survey-activity");
  const empty = activity.querySelector(".empty-activity");
  if (empty) empty.remove();
  const item = createElement("div", "activity-item");
  item.appendChild(createElement("strong", "", result.course_title || "問卷處理"));
  item.appendChild(createElement("span", "", result.message || ""));
  activity.appendChild(item);
  activity.scrollTop = activity.scrollHeight;
}

function handleEvent(rawMessage) {
  let event;
  try {
    event = JSON.parse(rawMessage);
  } catch (error) {
    appendClientLog(`無法解析背景訊息：${error}`, "error");
    return;
  }
  const payload = event.payload || {};

  switch (event.type) {
    case "bootstrap":
      state.browserRunning = Boolean(payload.browser_running);
      state.loggedIn = Boolean(payload.logged_in);
      state.busy = Boolean(payload.busy);
      break;
    case "busy":
      state.busy = Boolean(payload.busy);
      state.busyAction = payload.action || "";
      if (payload.message) byId("current-status").textContent = payload.message;
      if (state.studyCourses.length) renderStudyCourses();
      break;
    case "browser_state": {
      const browserState = payload.state || {};
      state.browserRunning = Boolean(payload.browser_running);
      state.loggedIn = Boolean(payload.logged_in);
      state.busy = Boolean(payload.busy);
      state.busyAction = payload.busy_action || "";
      byId("current-url").textContent = browserState.current_url || "-";
      byId("current-page").textContent = browserState.page_title || "-";
      byId("current-status").textContent = browserState.message || "-";
      break;
    }
    case "login_result": {
      const result = payload.result || {};
      state.loggedIn = Boolean(payload.logged_in);
      byId("current-url").textContent = result.current_url || byId("current-url").textContent;
      byId("current-page").textContent = result.page_title || byId("current-page").textContent;
      byId("current-status").textContent = result.message || "已登入";
      if (state.loggedIn) showToast("已登入並進入個人專區。 ");
      break;
    }
    case "course_scan":
      state.studyCourses = payload.courses || [];
      state.assessmentCourses = payload.assessment_courses || [];
      state.selectedAssessment = -1;
      state.selectedStudy = -1;
      state.studyChecked = new Set(
        state.studyCourses
          .map((course, index) => Number(course.remaining_reading_seconds || 0) > 0 ? index : -1)
          .filter((index) => index >= 0),
      );
      state.currentStudyCourse = "";
      state.countdownCourse = "";
      state.countdownSeconds = null;
      byId("study-countdown").textContent = "--:--:--";
      byId("countdown-course").textContent = "尚未開始";
      byId("study-selection-label").textContent = `已勾選 ${state.studyChecked.size} 門`;
      byId("assessment-selection-label").textContent = "尚未選取";
      renderStudyCourses();
      renderAssessmentCourses();
      break;
    case "assessment_scan":
      state.assessmentCourses = payload.courses || [];
      state.selectedAssessment = -1;
      byId("assessment-selection-label").textContent = "尚未選取";
      renderAssessmentCourses();
      break;
    case "automation_result": {
      const result = payload.result || {};
      const previousCourse = state.currentStudyCourse;
      if (state.busyAction === "runPlayback" && result.course_title
          && result.status !== "course_skipped") {
        state.currentStudyCourse = result.course_title;
      }
      if (result.status === "playback_finished" || result.status === "playback_stopped") {
        state.currentStudyCourse = "";
      }
      let shouldRenderStudy = previousCourse !== state.currentStudyCourse;
      if (result.status === "course_completed" || result.status === "course_skipped") {
        const title = result.course_title;
        const target = state.studyCourses.find((c) => c.title === title);
        if (target) {
          if (result.course_info) {
            target.studied_reading_seconds = result.course_info.studied_reading_seconds;
            target.remaining_reading_seconds = result.course_info.remaining_reading_seconds;
            target.required_reading_seconds = result.course_info.required_reading_seconds;
            target.survey_status = result.course_info.survey_status || target.survey_status;
          }
          if (Number(target.remaining_reading_seconds || 0) <= 0) {
            const idx = state.studyCourses.indexOf(target);
            state.studyChecked.delete(idx);
            byId("study-selection-label").textContent = `已勾選 ${state.studyChecked.size} 門`;
          }
          shouldRenderStudy = true;
        }
      }
      if (shouldRenderStudy) renderStudyCourses();
      const frameCount = Array.isArray(result.frame_urls) ? result.frame_urls.length : 0;
      const details = [
        result.message,
        result.chapter_title ? `章節：${result.chapter_title}` : "",
        frameCount > 0 ? `iframe ${frameCount} 個` : "",
        Number(result.video_count || 0) > 0 ? `影音元素 ${result.video_count} 個` : "",
      ].filter(Boolean);
      byId("study-detail").textContent = details.join(" | ");
      const remainingSeconds = Number(result.remaining_seconds || 0);
      const isCurrentCourseTick = result.status === "playback_waiting"
        && Boolean(result.course_title)
        && (state.countdownCourse === result.course_title || remainingSeconds > 0);
      if (remainingSeconds > 0 || isCurrentCourseTick) {
        updateStudyCountdown(remainingSeconds, result.course_title || "", true);
      } else if (result.status === "playback_finished") {
        state.countdownCourse = "排程完成";
        updateStudyCountdown(0, "排程完成", true);
      } else if (result.status === "playback_stopped") {
        state.countdownCourse = "已停止";
        state.countdownSeconds = null;
        byId("study-countdown").textContent = "--:--:--";
        byId("countdown-course").textContent = "已停止";
      }
      if (result.current_url) byId("current-url").textContent = result.current_url;
      if (result.page_title) byId("current-page").textContent = result.page_title;
      break;
    }
    case "survey_result":
      renderSurveyResult(payload.result || {});
      break;
    case "enrollment_search":
      state.enrollmentCourses = payload.courses || [];
      state.enrollmentChecked = new Set(state.enrollmentCourses.map((_, index) => index));
      showEnrollmentResults();
      renderEnrollmentResults();
      break;
    case "enrollment_submit":
      updateEnrollmentResults(payload.results || []);
      break;
    case "notice":
      showNotice(payload.title || "完成", payload.message || "操作已完成。");
      break;
    case "log":
      appendLog(payload);
      break;
    case "error":
      if (state.busyAction === "runPlayback") {
        state.currentStudyCourse = "";
        state.countdownCourse = "已中止";
        state.countdownSeconds = null;
        byId("study-countdown").textContent = "--:--:--";
        byId("countdown-course").textContent = "已中止";
        renderStudyCourses();
      }
      showToast(payload.message || "執行失敗");
      appendClientLog(`錯誤：${payload.message || "執行失敗"}`, "error");
      byId("current-status").textContent = payload.message || "執行失敗";
      break;
    default:
      appendClientLog(`收到未知事件：${event.type}`, "warning");
  }
  updateControls();
}

function showNotice(title, message) {
  byId("notice-title").textContent = title;
  byId("notice-message").textContent = message;
  byId("notice-modal").classList.remove("hidden");
  byId("notice-close-button").focus();
}

function hideNotice() {
  byId("notice-modal").classList.add("hidden");
}

function bindActions() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });

  byId("start-browser-button").addEventListener("click", () => callBridge("startBrowser"));
  byId("close-browser-button").addEventListener("click", () => callBridge("closeBrowser"));
  byId("check-login-button").addEventListener("click", () => callBridge("checkLogin"));
  byId("login-button").addEventListener("click", () => {
    callBridge("login", byId("account").value.trim(), byId("password").value, byId("remember").checked);
  });
  byId("password").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !byId("login-button").disabled) byId("login-button").click();
  });

  byId("scan-courses-button").addEventListener("click", () => callBridge("scanCourses"));
  byId("enter-course-button").addEventListener("click", () => callBridge("enterStudyCourse", state.selectedStudy));
  byId("inspect-player-button").addEventListener("click", () => callBridge("inspectPlayer"));
  byId("playback-button").addEventListener("click", () => {
    callBridge("runPlayback", JSON.stringify(Array.from(state.studyChecked).sort((a, b) => a - b)));
  });
  byId("skip-course-button").addEventListener("click", () => callBridge("skipCourse"));
  byId("stop-playback-button").addEventListener("click", () => callBridge("stopPlayback"));

  byId("add-condition-button").addEventListener("click", addCondition);
  byId("remove-condition-button").addEventListener("click", removeCondition);
  byId("condition-keyword").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addCondition();
  });
  byId("search-enrollment-button").addEventListener("click", searchEnrollment);
  byId("back-to-conditions-button").addEventListener("click", showEnrollmentConditions);
  byId("enroll-button").addEventListener("click", enrollSelected);

  byId("survey-button").addEventListener("click", () => callBridge("processSurveys"));
  byId("enter-assessment-button").addEventListener("click", () => callBridge("enterAssessmentCourse", state.selectedAssessment));
  byId("auto-answer-button").addEventListener("click", () => callBridge("autoAnswerAssessment", state.selectedAssessment));
  byId("fill-assessment-button").addEventListener("click", () => callBridge("fillAssessmentAnswers"));
  byId("submit-assessment-button").addEventListener("click", () => callBridge("submitAssessment"));
  byId("notice-close-button").addEventListener("click", hideNotice);
  byId("notice-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) hideNotice();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !byId("notice-modal").classList.contains("hidden")) hideNotice();
  });

  byId("log-toggle").addEventListener("click", () => {
    const drawer = byId("log-drawer");
    const collapsed = drawer.classList.toggle("collapsed");
    byId("log-toggle").setAttribute("aria-expanded", String(!collapsed));
    byId("log-toggle-label").textContent = collapsed ? "展開" : "收合";
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bindActions();
  renderStudyCourses();
  renderAssessmentCourses();
  renderConditions();
  updateControls();

  if (typeof QWebChannel === "undefined" || !window.qt || !qt.webChannelTransport) {
    showToast("無法連接 Python 背景服務。 ");
    appendClientLog("Qt WebChannel 初始化失敗。", "error");
    return;
  }

  new QWebChannel(qt.webChannelTransport, (channel) => {
    state.bridge = channel.objects.bridge;
    state.bridge.event.connect(handleEvent);
    state.bridge.initialize();
    appendClientLog("HTML 介面已連接背景服務。 ");
  });
});
