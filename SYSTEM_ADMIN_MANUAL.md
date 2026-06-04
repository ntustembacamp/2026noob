# noob 系統管理手冊

## 1. 文件目的

本手冊提供系統管理人員使用，用於部署、啟用、維護與排除 `noob` 人臉辨識系統。

## 2. 系統概述

`noob` 是一套以本地模型推論為核心的人臉辨識系統，主要功能如下：

- 以 `InsightFace antelopev2` 進行人臉偵測與辨識
- 以 MySQL 儲存人臉資料索引與辨識結果
- 提供 FastAPI API 介面
- 支援從指定資料夾匯入人臉照片並重建 embedding

目前建議的啟用方式為：

- Windows 主機
- Docker Desktop
- MySQL 容器
- GPU 推論

## 3. 系統架構

主要元件：

- API 入口：`service/main.py`
- 辨識核心：`service/new_recognize.py`
- 人臉模型與 embedding 比對：`service/tools/new_face.py`
- 人臉資料匯入 `base`：`service/new_insert_feature_src.py`
- 重建 embedding：`service/new_construct_face_db.py`
- MySQL 連線：`service/tools/mysql_utils.py`
- Docker 啟動：`infrastructure/docker-compose-win.yml`
- MySQL 啟動：`infrastructure/docker-compose-sql.yml`
- 設定檔：`shared/config.py`

## 4. 角色與權限建議

系統管理人員至少需要具備以下權限：

- Windows 主機管理權限
- Docker Desktop 操作權限
- 專案資料夾讀寫權限
- `C:\activity` 與 `C:\feature_src` 存取權限
- MySQL 管理權限

## 5. 環境需求

建議環境：

- Windows 10/11
- Python 3.12
- Docker Desktop
- NVIDIA GPU 與對應驅動

必要路徑：

- `C:\activity`
- `C:\feature_src`

必要檔案：

- `service/models/antelopev2/*.onnx`
- `service/embedding/faces_embedding_antelopev2.pkl`

## 6. 重要資料夾說明

- `service/models/antelopev2/`
  - 存放 InsightFace 模型檔
- `service/embedding/`
  - 存放人臉 embedding 檔
- `database/test_db/`
  - 可放測試照片
- `database/mark_db/`
  - 若開啟標記功能，輸出標記後照片
- `C:\feature_src`
  - 匯入人臉參考照來源
- `C:\activity`
  - 作業照片來源

## 7. 啟用流程

### 7.1 啟動 MySQL

在專案根目錄執行：

```powershell
docker compose -f infrastructure/docker-compose-sql.yml up -d
```

確認容器狀態：

```powershell
docker ps
```

應看到：

- `db`

### 7.2 啟動 API

在專案根目錄執行：

```powershell
docker compose -f infrastructure/docker-compose-win.yml up -d --build
```

確認容器狀態：

```powershell
docker ps
```

應看到：

- `face_reco`
- `hub`

### 7.3 驗證服務是否啟用

開啟：

- `http://localhost:8000/docs`
- `http://localhost:8000/openapi.json`

若能回應 `200`，代表 API 啟用成功。

## 8. MySQL 設定

目前系統設定位於 `shared/config.py`：

- `MYSQL_HOST = 'db'`
- `MYSQL_PORT = 3306`
- `MYSQL_USER = 'myuser'`
- `MYSQL_PWD = 'mypassword'`
- `MYSQL_DB = 'mydatabase'`

這代表 API 容器會透過 Docker 網路直接連到 MySQL 容器。

## 9. 初始化資料表

至少需要以下資料表：

- `base`
- `reco_result`

用途：

- `base`
  - 儲存人臉主檔與參考照片索引
- `reco_result`
  - 儲存辨識結果

## 10. 匯入人臉照片

### 10.1 準備參考照片

將參考照片放入：

- `C:\feature_src`

因 Docker 掛載後，容器內對應為：

- `/mnt/feature_src`

### 10.2 檔名格式

檔名需符合以下格式：

```text
dept_year_team_name.jpg
```

範例：

```text
資管_114_A班_王小明.jpg
老師_999_行政_陳老師.png
```

### 10.3 寫入 `base`

執行：

```powershell
docker exec face_reco python /root/noob/service/new_insert_feature_src.py
```

執行後會將照片資訊寫入 `base` 表。

## 11. 重建 embedding

當新增、刪除或更新參考照片時，必須重建 embedding。

執行：

```powershell
docker exec face_reco python /root/noob/service/new_construct_face_db.py
```

輸出檔案：

- `service/embedding/faces_embedding_antelopev2.pkl`

注意：

- 重建 embedding 不等於重建 Docker image
- 只有模型環境或程式碼有變更時，才需要 `docker compose ... --build`

## 12. API 管理

### 12.1 主要 API

- `POST /async-recognize/`
  - 背景執行照片辨識
- `POST /image_socre`
  - 計算照片品質分數

### 12.2 手動測試

可使用瀏覽器打開：

- `http://localhost:8000/docs`

## 13. 日常維運工作

系統管理人員建議定期執行：

- 檢查 Docker 容器是否正常
- 檢查 `db` 與 `face_reco` 日誌
- 確認模型檔與 embedding 檔是否存在
- 確認 MySQL 中 `reco_result` 是否持續寫入
- 在新增人員資料後重建 embedding

## 14. 常用指令

查看容器：

```powershell
docker ps
```

查看 API 日誌：

```powershell
docker logs face_reco
```

持續追蹤 API 日誌：

```powershell
docker logs -f face_reco
```

查看 MySQL 日誌：

```powershell
docker logs db
```

重啟 API：

```powershell
docker compose -f infrastructure/docker-compose-win.yml restart
```

重建 API image：

```powershell
docker compose -f infrastructure/docker-compose-win.yml up -d --build
```

## 15. 常見問題與排除

### 問題 1：`/docs` 無法開啟

檢查：

- Docker Desktop 是否啟動
- `face_reco` 是否正在執行
- `docker logs face_reco` 是否有錯誤

### 問題 2：模型找不到

檢查：

- `service/models/antelopev2/` 內是否有 `.onnx`
- 檔案是否放在正確層級，而不是多包一層子資料夾

### 問題 3：辨識不到人名

檢查：

- `base` 表是否已有資料
- `faces_embedding_antelopev2.pkl` 是否已重建
- 參考照片檔名格式是否正確

### 問題 4：`image_socre` 第一次很慢

原因：

- 第一次初始化品質模型需要較久時間

目前系統已將 Gunicorn timeout 提高，避免初始化時被過早中斷。

### 問題 5：照片沒有寫入 `reco_result`

檢查：

- 照片路徑是否為容器可讀取路徑
- 照片是否為有效圖檔
- MySQL 是否可連線

## 16. 資安與資料注意事項

目前檢查到的專案主流程中：

- 照片主要在本機與 Docker 容器內處理
- 結果寫入本機 MySQL
- 未看到將照片外送到 OpenAI 的主流程

但仍建議系統管理人員：

- 管控 `C:\activity` 與 `C:\feature_src` 權限
- 定期備份 MySQL
- 定期備份 `faces_embedding_antelopev2.pkl`
- 將測試照片與正式照片分開管理

## 17. 建議交接清單

交接給下一位管理人員時，至少應提供：

- 專案位置
- Docker 啟動指令
- MySQL 帳號密碼
- 模型檔取得方式
- embedding 重建流程
- 測試照片與驗證流程
## 2026-04-22 維運修正紀錄

本次實際維運時，已修正以下幾個會影響啟動與辨識的問題：

### Docker 與啟動修正
- `shared/config.py`
  - MySQL host 由固定 IP 改為 `db`
  - API 容器改以 Docker compose service name 連到 MySQL
- `infrastructure/Dockerfile`
  - 套件由 `libgl1-mesa-glx` 改為 `libgl1`
  - 避免新版 base image build 失敗
- `infrastructure/start.sh`
  - 已補上較長 timeout
  - 避免 `/image_socre` 第一次模型初始化時 worker 被重啟

### `/image_socre` 修正
- `service/new_recognize.py`
  - `clipiqa+` 已改為 lazy load
  - 避免 FastAPI 啟動時卡在模型下載 / 初始化，造成 `/docs` 與 `/openapi.json` 無法使用

### `label_face_name=true` 沒有輸出標記圖

這次排查確認，標記圖沒出來可能有 3 類原因：

1. 目錄不存在
- 程式原本沒有先建立 `database/mark_db/`
- 現在已補建立此目錄

2. 存圖流程不夠健壯
- `service/new_recognize.py` 現在已補上：
  - 存圖前自動建立目錄
  - `os.path.join(...)` 組路徑
  - 存圖成功 / 失敗日誌
  - `cv2.imencode(...)` 失敗時明確報錯

3. NumPy 與 InsightFace 相容性問題
- `service/tools/new_face.py` 已補上 `np.int` 相容處理
- 原因：畫框函式內部仍使用舊版 `np.int`，在新版 NumPy 會拋出：
  - `module 'numpy' has no attribute 'int'`
- 現象：
  - API 回 `202`
  - log 顯示已辨識到人
  - 但 `mark_db` 沒有新圖

### 已驗證成功案例
- 來源圖片：`/mnt/activity/企管_114_2_吳偉青.JPG`
- `label_face_name=true`
- 成功輸出：
  - `/root/noob/database/mark_db/企管_114_2_吳偉青_mark.jpg`
- 成功寫入 `reco_result`
- 成功辨識：
  - `reco_name = ["企管_114_2_吳偉青"]`

## 管理員排錯 SOP

當作業人員回報「API 回 202，但沒有標記圖」時，請依序檢查：
- 先看 `/var/log/fastapi.log`，確認請求是否真的有送 `label_face_name=true`
- 再看 `/var/log/activity_photo_reco.log`，確認背景辨識是否真的有跑
- 搜尋：
  - `開始處理`
  - `results:`
  - `start saving marked face image`
  - `marked face image saved`
- 確認目錄存在：
  - 本機：`./database/mark_db/`
  - 容器：`/root/noob/database/mark_db/`
- 確認圖片路徑為容器內路徑，例如 `/mnt/activity/xxx.jpg`
- 若有抓到臉但都是 `unknown`
  - 先查 `base` 表是否有此人
  - 再確認 `faces_embedding_antelopev2.pkl` 是否已重建
## 2026-04-26 embedding 管理補充

### base / face_embedding_meta / .pkl 三者關係

目前 `noob` 的人臉特徵管理分成 3 層：

- `base`
  - 人員主檔與來源照片索引。
  - 主要欄位包含 `dept / year / team / name / file_path / file_name / phash`。
  - 這張表代表「應該拿哪些照片來建特徵」。
- `face_embedding_meta`
  - embedding 管理表。
  - 用來記錄每一筆 `base` 資料目前的特徵狀態，例如：
    - `embedding_exists`
    - `face_count`
    - `status`
    - `error_message`
    - `embedding_update_time`
  - 這張表代表「特徵有沒有建好、失敗原因是什麼、最後何時更新」。
- `faces_embedding_antelopev2.pkl`
  - runtime 真正辨識時會載入的特徵庫。
  - 這是 API 做人臉比對時實際使用的快取檔案。

簡單理解：

- `base` = 人員主檔
- `face_embedding_meta` = 特徵管理狀態
- `.pkl` = 辨識引擎目前正在使用的特徵庫

### admin-ui 現在可做什麼

`http://localhost:8000/admin-ui`

目前除了可以查詢與編輯 `base`，也整合了 embedding 後台功能：

- 查詢 `face_embedding_meta`
- 顯示目前 `base / meta / ready / deleted / runtime .pkl` 筆數
- 檢視每筆特徵狀態
- 刪除單筆 embedding
- 重建全部 embedding

### 刪除單筆 embedding

在 `embedding 特徵庫管理` 區塊按 `刪除特徵` 時，系統會：

1. 先備份目前的 `faces_embedding_antelopev2.pkl`
2. 從 runtime `.pkl` 移除對應的人員特徵
3. 將 `face_embedding_meta` 更新為：
   - `embedding_exists = 0`
   - `status = 'deleted'`

注意：

- 這個動作不會刪除 `base`
- 只會刪除目前特徵與狀態
- 如果之後執行 `重建全部 embedding`，系統仍會依 `base` 重新把該人建回來

### 重建全部 embedding

按下 `重建全部 embedding` 時，系統會：

1. 先備份目前的 `.pkl`
2. 依 `base` 重新讀圖建特徵
3. 重新輸出新的 `faces_embedding_antelopev2.pkl`
4. 同步更新 `face_embedding_meta`

常見狀態說明：

- `ready`
  - 成功建立特徵
- `deleted`
  - 管理員手動刪除目前特徵
- `missing_meta`
  - `base` 有資料，但 `face_embedding_meta` 尚未建立
- `face_count_mismatch`
  - 照片偵測到的人臉數不是 1
- `image_decode_failed`
  - 照片無法正常讀取
- `error`
  - 建特徵過程發生其他錯誤

### 維運建議

日常維運建議順序：

1. 先確認 `base` 是否正確
2. 再看 `face_embedding_meta` 是否有失敗或缺漏
3. 最後確認 runtime `.pkl` 筆數是否與預期接近

如果發生「`base` 有資料，但辨識不到人」的情況，優先檢查：

- `face_embedding_meta.status`
- `face_embedding_meta.error_message`
- `.pkl` 是否已重建
