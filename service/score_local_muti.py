from new_recognize import image_socre

import datetime

from tools.mysql_utils import mysqlconnector
import pandas as pd

import time

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import threading

import os

sleep_t=0.1

###  全域控制變數
MAX_WORKERS = 1  # 根據CPU核心數調整
TASK_QUEUE = Queue()
DB_LOCK = threading.Lock()  # 資料庫操作鎖

### 🧵 執行緒安全資料庫連線池
class DBPool:
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if not cls._instance:
                cls._instance = super().__new__(cls)
                cls._local = threading.local()
            return cls._instance
    
    def get_conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = mysqlconnector()
            self._local.conn.connect()
        return self._local.conn

### 核心處理函數
def process_item(item):
    tmp_file = f"/tmp/{threading.get_ident()}_resized.jpg"  # 線程專用暫存檔
    try:
        #  圖片描述處理
        print(datetime.datetime.now(), '開始處理:', threading.get_ident,item.origin_full_path)
        img_socre=image_socre(item.thumbs_full_path)
        upload_df=pd.DataFrame([{
                'origin_full_path': item.origin_full_path,
                'thumbs_full_path': item.thumbs_full_path,
                'human_activity_date': item.human_activity_date,
                'human_activity_name': item.human_activity_name,
                'human_laptop_number': item.human_laptop_number,
                'human_photographer': item.human_photographer,
                'human_photo_time': item.human_photo_time,
                'img_score': img_socre
            }])
        upload_df['human_photo_time'] = pd.to_datetime(upload_df['human_photo_time'])
        upload_df['human_photo_time'] = upload_df['human_photo_time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        with DB_LOCK:
            inert_into_img_upload(upload_df=upload_df)

    except Exception as e:
        print(f" 處理失敗 {item.origin_full_path}: {str(e)}")
    # finally:
    #     if os.path.exists(tmp_file):
    #         os.remove(tmp_file)  # 清理暫存檔

    time.sleep(sleep_t)


def get_to_do_list():
    q=f"""
    SELECT         
        origin_full_path,
        thumbs_full_path,
        human_activity_date,
        human_activity_name,
        human_laptop_number,
        human_photographer,
        human_photo_time,
        img_score,
        create_time,
        update_time
    FROM 
        img_upload
    WHERE img_score = 0
    """
    return db.execute_query(sql_str=q)


def inert_into_img_upload(upload_df):
    # img_upload insert into db 開始 ===
    upsert_query = """
    INSERT INTO img_upload (
        origin_full_path,
        thumbs_full_path,
        human_activity_date,
        human_activity_name,
        human_laptop_number,
        human_photographer,
        human_photo_time,
        img_score,
        create_time,
        update_time
    )
    VALUES (
        %(origin_full_path)s,
        %(thumbs_full_path)s,
        %(human_activity_date)s,
        %(human_activity_name)s,
        %(human_laptop_number)s,
        %(human_photographer)s,
        %(human_photo_time)s,
        %(img_score)s,
        NOW(),
        NOW()
    )
    ON DUPLICATE KEY UPDATE
        img_score = VALUES(img_score),
        update_time = NOW();
    """
    try:
        # 轉換 DataFrame 為字典列表
        data_dict = upload_df.to_dict('records')
        # 批次執行（使用 executemany 提升效率）
        db.executemany_query(sql_str=upsert_query, seq_of_params=data_dict, commit=True)
        time.sleep(sleep_t)
    except Exception as e:
        print(f"{datetime.datetime.now()} 資料庫操作失敗: {str(e)}")
        # logger.error(f"資料庫操作失敗: {str(e)}")

###  主執行流程
if __name__ == "__main__":
    db = mysqlconnector()
    db.connect()
    print(f'{datetime.datetime.now()} 初始化任務隊列')
    todo_data = get_to_do_list()
    todo_df = pd.DataFrame(todo_data["data"], columns=todo_data["columns"])
    [TASK_QUEUE.put(row) for row in todo_df.itertuples()]
    
    print(f'🎬 啟動{MAX_WORKERS}個工作線程')
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_item, TASK_QUEUE.get()) 
                  for _ in range(min(MAX_WORKERS, TASK_QUEUE.qsize()))]
        
        while not TASK_QUEUE.empty():
            futures.append(executor.submit(process_item, TASK_QUEUE.get()))
            
        for future in futures:
            try:
                future.result()  # 捕捉例外
            except Exception as e:
                print(f" 線程異常: {str(e)}")
    
    print(f' {datetime.datetime.now()} 全部任務完成！')

