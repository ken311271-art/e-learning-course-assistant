# E 等公務員學習助手

Windows 桌面工具，使用 Python、PySide6 與 Playwright 協助操作 E 等公務員學習平台。

## 目前功能

- 啟動 Chrome 並保存登入 Session
- 自動偵測登入狀態及進入個人專區
- 跨頁掃描課程、閱讀時數與剩餘時數
- 勾選要執行的課程，剩餘時數為零的課程不再進入
- 目前執行中的課程使用不同顏色標示
- 偵測影音、投影片與多種 SCORM 章節結構
- 課程倒數、停止上課及返回課程介面
- 自動處理閱讀閒置確認視窗，並在 Log 留下紀錄
- 掃描符合閱讀時數且尚未填寫的問卷
- 選課條件、搜尋課程與勾選報名

## 下載離線版

請到 [GitHub Releases](https://github.com/yaotong110329/e-learning-course-assistant/releases/latest)
下載最新版 Windows 離線 ZIP。完整解壓縮後執行 `離線啟動.bat`。

## 原始碼執行

需求：Python 3.12+、Google Chrome、網路連線。

```bat
install.bat
run.bat
```

## 專案結構

- `core/`：瀏覽器、登入、課程掃描、上課、問卷與選課邏輯
- `gui/`：PySide6 視窗及 WebChannel 溝通
- `resources/ui/`：HTML、CSS、JavaScript 操作介面
- `tools/`：不作答、不送出的診斷工具

## 注意事項

- 離線版已包含 Python 相依套件，但操作網站仍需要網路。
- 不要只複製 EXE，請保留解壓縮後的完整資料夾。
- `user_data*`、`logs`、偵測快照及帳密設定不會上傳 GitHub。
- 網站改版後，Selector 或流程可能需要更新。
