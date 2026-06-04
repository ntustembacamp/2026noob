# 特徵建構流程
# 1. 執行 new_insert_feature_src.py
# 2. 執行 new_construct_face_db.py

# 大頭照 to base
# table: base 人員大頭照 照片清單
# 接著執行 new_construct_face_db.py

import os
from PIL import Image
import imagehash
import pandas as pd
import datetime

import sys
sys.path.append(os.getenv('CONFIG_PATH'))
from config import FEATURE_PATH
from tools.mysql_utils import mysqlconnector


folder_path = FEATURE_PATH
file_list = os.listdir(folder_path)
# file_list.remove('.DS_Store')
# file_list.remove('.gitkeep')

# print(file_list)
# 結果儲存用
results = []

for filename in file_list:
    if filename not in ['.DS_Store','.gitkeep']:
        file_path = os.path.join(folder_path, filename)
        # 檢查是否為檔案（排除資料夾）
        if os.path.isfile(file_path):
            # print(file_path)
            tmp = (filename.split('.')[0]).split('_')
            try:
                img = Image.open(file_path)
                phash = imagehash.phash(img)
                tmp.append(str(phash))
            except Exception as e:
                tmp.append('')
            # print(tmp)
            tmp.append(folder_path)
            tmp.append(filename)
            # print(len(results),tmp)
            results.append(tmp)

df = pd.DataFrame(data=results, columns=['dept', 'year', 'team', 'name', 'phash', 'file_path', 'file_name'])

print(f'{datetime.datetime.now()} 總筆數: {df.__len__()}')

# 關鍵型態轉換
try:
    df['year'] = df['year'].astype(int)  # 確保與資料庫 INT 型態相容
except Exception as e:
    print(f"年度轉換錯誤: {str(e)}")
    exit(1)


db = mysqlconnector()
db.connect()

# UPSERT 語法 (含時間戳記自動更新)
upsert_query = """
INSERT INTO base (
    dept, 
    year, 
    team, 
    name, 
    phash, 
    file_path, 
    file_name, 
    create_time, 
    update_time
)
VALUES (
    %(dept)s, 
    %(year)s, 
    %(team)s, 
    %(name)s, 
    %(phash)s, 
    %(file_path)s, 
    %(file_name)s, 
    NOW(), 
    NOW()
)
ON DUPLICATE KEY UPDATE
    phash = VALUES(phash),
    file_path = VALUES(file_path),
    file_name = VALUES(file_name),
    update_time = NOW();
"""

# 批次執行 UPSERT
try:
    # 轉換 DataFrame 為字典列表
    data_dict = df.to_dict('records')

    # 批次執行（使用 executemany 提升效率）
    db.executemany_query(
        sql_str=upsert_query,
        seq_of_params=data_dict,
        commit=True
    )
    print(f"成功寫入 {len(df)} 筆資料！")
except Exception as e:
    print(f"資料庫操作失敗: {str(e)}")
finally:
    db.close()
