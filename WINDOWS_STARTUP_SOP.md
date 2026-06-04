# Windows 啟動 SOP

這份 SOP 以目前這台 Windows 電腦的狀態整理，目標是讓 `noob` 專案可以用最少試錯成本啟動。

## 先說結論

- 推薦啟動方式：`Docker + MySQL 容器 + GPU`
- 不推薦一開始走 Windows 原生 Python 直接跑
- 目前專案還不能直接啟動，因為缺少模型檔、embedding 檔，而且 Docker daemon 尚未啟動

## 主程式與主要流程

- API 入口：`service/main.py`
- 辨識流程：`service/new_recognize.py`
- 人臉模型與 embedding 比對：`service/tools/new_face.py`
- 建立 embedding：`service/new_construct_face_db.py`
- 匯入特徵來源到 `base` 表：`service/new_insert_feature_src.py`
- MySQL 連線：`service/tools/mysql_utils.py`
- Docker 啟動：`infrastructure/docker-compose-win.yml`
- MySQL 容器：`infrastructure/docker-compose-sql.yml`
- 啟動命令：`infrastructure/start.sh`
- Docker 用設定檔：`shared/config.py`

## 可先忽略的舊版或輔助檔案

這些檔案目前不是 v2 啟動主線，可以先不用碰：

- `service/achieve/`
- `service/example_api.py`
- `service/example_mysql.py`
- `service/example_reco.py`
- `service/reco_local_muti.py`
- `service/score_local_muti.py`
- `service/refer/`
- `service/tools/check_pkl.py`
- `service/tools/feature_src_check_list.py`
- `service/tools/feature_src_rename.py`
- `database/table_csv_backup/`
- `database/default_script/` 下的大多數 SQL 檔

## 啟動前必要條件

### 1. 開啟 Docker Desktop

目前 `docker` 指令存在，但 Docker daemon 尚未啟動；在 daemon 沒起來前，`docker ps` 和 `docker compose up` 都不會成功。

### 2. 確認這些掛載路徑存在

這台電腦已經有下列資料夾，符合 `docker-compose-win.yml` 的掛載設定：

- `C:\activity`
- `C:\feature_src`

容器內會對應成：

- `/mnt/activity`
- `/mnt/feature_src`

### 3. 準備模型檔與 embedding 檔

目前缺少這兩個必要資源：

- `service/models/antelopev2/`
- `service/embedding/faces_embedding_antelopev2.pkl`

目前資料夾狀態：

- `service/models/` 只有 `.gitkeep`
- `service/embedding/` 只有 `.gitkeep`

依 README 描述，原始下載與解壓目標應該是：

- `antelopev2.tar.gz` 解壓到 `service/models/`
- `faces_embedding_antelopev2.tar.gz` 解壓後應提供 `faces_embedding_antelopev2.pkl` 到 `service/embedding/`
- `photo_management.tar.gz` 解壓到 `database/`

注意：程式碼實際讀的是 `faces_embedding_antelopev2.pkl`。README 有一處提到 `faces_embedding_antelopev2_114.pkl`，但目前主程式沒有讀這個檔名。

### 4. MySQL 必須先可用

v2 主流程會直接打 MySQL，不是 SQLite。

主要依賴資料表：

- `base`
- `reco_result`

初始化可參考：

- `database/default_script/table_base.sql`
- `database/default_script/table_reset_reco_result.sql`

## 建議啟動順序

### 步驟 1：開 Docker Desktop

先確認 Docker Desktop 已經完全啟動，再執行下面命令。

### 步驟 2：啟動 MySQL 容器

在專案根目錄 `noob` 下執行：

```powershell
docker compose -f infrastructure/docker-compose-sql.yml up -d
```

### 步驟 3：修正 MySQL 連線設定

`shared/config.py` 目前把 `MYSQL_HOST` 寫死成 `192.168.0.180`。

如果 MySQL 是你剛用 compose 起的容器，這個值通常不會對。比較合理的作法是改成 MySQL 容器可以被同網路解析到的主機名稱或正確 IP。

如果你沿用目前專案 README 的做法，就需要先查到 MySQL 容器所在的 `custom_network` IP，再改寫 `shared/config.py`。

### 步驟 4：確認模型與 embedding 已就位

至少要確認：

- `service/models/antelopev2/` 不是空的
- `service/embedding/faces_embedding_antelopev2.pkl` 存在

如果只有模型沒有 embedding，可以依序執行：

1. 準備 `FEATURE_PATH` 對應到 `/mnt/feature_src`
2. 執行 `new_insert_feature_src.py` 匯入 `base`
3. 執行 `new_construct_face_db.py` 產生 `faces_embedding_antelopev2.pkl`

### 步驟 5：啟動 API 容器

```powershell
docker compose -f infrastructure/docker-compose-win.yml up --build
```

服務預設會啟在：

- `http://localhost:8000`

API 入口：

- `POST /async-recognize/`
- `POST /image_socre`

## 最小成功條件檢查表

以下全部成立，API 才有機會正常工作：

- Docker Desktop 已啟動
- MySQL 容器已啟動
- `shared/config.py` 的 MySQL 設定可連線
- `service/models/antelopev2/` 已放好模型
- `service/embedding/faces_embedding_antelopev2.pkl` 已存在
- `/mnt/activity` 內有要辨識的圖檔
- `/mnt/feature_src` 內有可建立人臉庫的人像圖

## 目前這台電腦的檢查結果

### 已具備

- Python 3.12 已安裝
- `pip` 可用
- `docker` 指令存在
- NVIDIA GPU 正常
- `C:\activity` 存在
- `C:\feature_src` 存在

### 尚未完成

- Docker daemon 尚未啟動
- 本機 Python 未安裝 v2 需要的套件，例如 `fastapi`、`insightface`、`mysql-connector-python`、`pyiqa`
- `service/models/antelopev2/` 不存在
- `service/embedding/faces_embedding_antelopev2.pkl` 不存在
- `database/` 下沒有解壓後的完整資料庫內容
- `shared/config.py` 的 MySQL host 可能不符合目前環境

## 為什麼不建議直接走 Windows 原生 Python

README 雖然有 Windows Plan B，但以目前程式碼來看，直接用 `shared/win/config.py` 跑 v2 主流程會卡住，原因如下：

- `shared/win/config.py` 沒有 `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PWD`、`MYSQL_DB`
- `shared/win/config.py` 沒有 `RECOGNIZE_LOG`
- `service/new_recognize.py` 和 `service/tools/mysql_utils.py` 會直接 import 這些設定

所以目前原生 Windows 模式比較像參考流程，不是可直接照做就成功的狀態。若要走這條路，還需要補 Windows 版設定檔。

## 缺少檔案清單

目前至少缺這些檔案或內容：

- `service/models/antelopev2/*`
- `service/embedding/faces_embedding_antelopev2.pkl`
- `database/` 解壓後資料

如果你要把這套系統真正跑起來，下一步最值得做的是：

1. 開啟 Docker Desktop
2. 補齊模型與 embedding 檔
3. 我再幫你把 `shared/config.py` 改成適合目前這台機器的 MySQL 設定
