# Locol-Vocol 介面截圖

更新日期：2026-10-30。

| 圖片 | 畫面 | 尺寸 |
| --- | --- | --- |
| `workspace-light.jpg` | 明亮工作台、錄音紀錄側欄與單一上傳入口 | 1280 × 720 |
| `workspace-dark.jpg` | 深色工作台 | 1280 × 720 |
| `transcript-desktop.jpg` | 桌面逐字稿與播放列 | 1280 × 720 |
| `transcript-mobile.jpg` | 手機逐字稿、播放列與底部分頁 | 390 × 844 |

所有圖片均從目前程式建立的獨立 Docker 示範環境擷取，使用虛構人物、靜音音檔與示範逐字稿；未使用正式資料庫、實際會議內容或真實 API key。示範錄音以 `speakr/tests/e2e/seed_browser_recordings.py` 建立，標題改為「產品流程討論」與「使用者訪談整理」。

重新截圖時，請在獨立的暫存資料庫執行 `speakr/tests/e2e/run_speakr.py`，並停用背景轉錄與 Inquire 模式，以符合本專案的預設介面。透過右上角選單切換外觀，等待側欄動畫結束後再擷取；手機版使用 390 × 844 視窗。請勿以實際會議資料更新公開截圖。
