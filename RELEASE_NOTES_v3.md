# E 等公務員學習助手 v3

## 更新日期

2026-07-20

## 更新內容

- 上課清單加入課程勾選，可指定要執行的課程。
- 正在執行的課程以不同顏色標示。
- 加入停止功能，可中斷排程並返回課程介面。
- 剩餘時數為 `00:00:00` 的課程不再進入。
- 掃描並顯示已閱讀時數、剩餘時數及即時倒數。
- Log 的時長改為分鐘與秒格式，不再顯示純秒數。
- 保留閱讀閒置提醒偵測，按下確認時寫入 Log。
- 問卷候選條件改為閱讀時數已達標且問卷尚未填寫。
- 更新 HTML 分頁介面與離線 Windows 打包版本。

## 離線版

請到 [GitHub Releases](https://github.com/yaotong110329/e-learning-course-assistant/releases/tag/v3.0.0)
下載完整 Windows 離線 ZIP。解壓縮後執行 `離線啟動.bat`，不需要重組分割檔。

完整 ZIP SHA-256：

```text
66a7e1009fadcc8fcd6dc6abd4f79d2be91852e09f90b63564e41c4b9ba2243a
```

## 驗證

- Python 模組編譯檢查通過。
- JavaScript 語法檢查通過。
- 打包後 EXE 啟動測試通過。
- 分割檔重組及 ZIP CRC 驗證通過。
