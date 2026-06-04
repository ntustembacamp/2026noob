# noob 使用者手冊

## 1. 文件目的

本手冊提供一般作業人員使用，說明如何使用 `noob` 人臉辨識系統進行照片辨識與查詢。

## 2. 你會用到的功能

一般作業人員主要會接觸以下功能：

- 送出照片辨識
- 查看 API 文件頁
- 檢查辨識是否已接收
- 視需要查看標記後照片

## 3. 使用前確認

請先確認系統管理人員已完成以下工作：

- API 已啟動
- MySQL 已啟動
- 模型與 embedding 已準備完成

你可以先打開：

- `http://localhost:8000/docs`

如果頁面能正常打開，代表系統可使用。

## 4. 最常用的頁面

### 4.1 API 文件頁

網址：

- `http://localhost:8000/docs`

用途：

- 查看有哪些 API
- 直接手動測試
- 送出辨識請求

### 4.2 OpenAPI 規格頁

網址：

- `http://localhost:8000/openapi.json`

一般作業人員通常不需要直接使用這個頁面。

## 5. 如何手動送出辨識

### 5.1 打開文件頁

在瀏覽器打開：

- `http://localhost:8000/docs`

### 5.2 找到 API

找到：

- `POST /async-recognize/`

### 5.3 按 `Try it out`

填入兩個參數：

- `file_path`
- `label_face_name`

### 5.4 參數說明

#### `file_path`

這是照片在容器內可讀到的路徑。

常見格式：

- `/root/noob/database/test_db/xxx.png`
- `/mnt/activity/...`

注意：

- 不要填 Windows 路徑，例如 `C:\xxx\yyy.jpg`
- 要填系統內部可讀的 Linux 路徑

#### `label_face_name`

- `false`
  - 只做辨識，不輸出標記圖
- `true`
  - 做辨識，並另外輸出畫框標記圖

### 5.5 按 `Execute`

如果系統正常，通常會回：

- HTTP `202`

這表示：

- 系統已收到照片
- 已放入背景處理
- 不是立即回傳全部辨識結果

## 6. 建議的第一次測試

若系統管理人員已提供測試圖，可先填：

```text
/root/noob/database/test_db/real_person_test.png
```

並設定：

- `label_face_name = false`

## 7. 如何看回應

### 7.1 成功接收

若回應 `202`，表示：

- 請求格式正確
- 系統已開始背景處理

### 7.2 常見錯誤

#### 404

通常表示：

- `file_path` 指向的檔案不存在

#### 500

通常表示：

- 系統內部處理發生錯誤
- 這種情況請通知系統管理人員查看日誌

## 8. 如何輸出標記後照片

若希望輸出畫框與名稱標記圖：

- 將 `label_face_name` 設為 `true`

系統若成功處理，標記圖通常會出現在：

- `database/mark_db/`

對應本機位置通常是：

- `C:\Users\Test\Desktop\Codex\AI人臉辨識\noob\database\mark_db\`

## 9. 如何使用圖片品質評分

文件頁中還有：

- `POST /image_socre`

用途：

- 計算一張照片的品質分數

使用方式：

1. 打開 `POST /image_socre`
2. 按 `Try it out`
3. 填入 `file_path`
4. 按 `Execute`

注意：

- 第一次使用時，初始化可能較慢
- 若系統管理人員正在調整模型或 timeout，第一次請稍微等待

## 10. 作業人員常見操作情境

### 情境 1：測試系統有沒有活著

1. 打開 `http://localhost:8000/docs`
2. 能打開就代表 API 目前可用

### 情境 2：送一張照片做辨識

1. 打開 `/docs`
2. 使用 `POST /async-recognize/`
3. 填入照片路徑
4. 按 `Execute`

### 情境 3：想要看標記後照片

1. 將 `label_face_name` 設為 `true`
2. 送出辨識
3. 到 `database/mark_db/` 查看輸出圖

## 11. 使用限制

請注意以下限制：

- 只能處理系統看得到的檔案路徑
- 不是所有照片都一定辨識成功
- 若未先建立人臉資料與 embedding，系統可能只能偵測到人臉，但認不出是誰

## 12. 何時要通知系統管理人員

遇到以下情況請通知系統管理人員：

- `/docs` 打不開
- API 一直回 500
- 照片路徑明明存在卻一直失敗
- 標記圖沒有輸出
- 新增人員後仍無法辨識

## 13. 作業注意事項

- 不要自行修改系統設定檔
- 不要自行刪除模型檔或 embedding 檔
- 不要把 Windows 路徑直接填進 API
- 若是新增人員資料，需由系統管理人員重建 embedding

## 14. 快速操作摘要

### 辨識照片

1. 開 `http://localhost:8000/docs`
2. 找 `POST /async-recognize/`
3. 按 `Try it out`
4. 填 `file_path`
5. 視需要設定 `label_face_name`
6. 按 `Execute`

### 品質評分

1. 開 `http://localhost:8000/docs`
2. 找 `POST /image_socre`
3. 按 `Try it out`
4. 填 `file_path`
5. 按 `Execute`

## 15. 使用者常見問答

### Q1：為什麼照片明明在電腦裡，系統卻說找不到？

因為 API 吃的是容器內路徑，不是 Windows 路徑。

### Q2：回 `202` 是不是已經辨識完？

不是。`202` 代表已接收，系統會在背景處理。

### Q3：為什麼有時候認不出來？

可能原因：

- 照片品質不好
- 臉太小
- 光線不佳
- 該人尚未建立在 `base` 與 embedding 裡

### Q4：我能不能自己新增人員讓系統辨識？

不建議直接由作業人員處理。新增人員通常需要：

- 新增參考照片
- 更新 `base`
- 重建 embedding

這通常應由系統管理人員執行。
