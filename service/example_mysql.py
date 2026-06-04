import sys, os
sys.path.append(os.getenv('CONFIG_PATH'))
from tools.mysql_utils import mysqlconnector

import pandas as pd


# # SELECT

# db = mysqlconnector()
# db.connect()
# q="""
# SELECT CONCAT(Dept,'_',year,'_',team,'_',name) person_name, CONCAT(file_path,'/', file_name) file_full_path FROM base
# """d
# res=db.execute_query(sql_str=q, commit=False)
# df = pd.DataFrame(res["data"], columns=res["columns"])
# print(df)

# db.close()

# # SELECT * FROM base
# # DESCRIBE base




# # INSERT DATA
# db = mysqlconnector()
# db.connect()

# df = pd.read_csv('/root/noob/database/114_ppl_list.csv', encoding='utf-8-sig')
# df['team'] = df['team'].astype(str).str.zfill(2)
# df['year'] = df['year'].astype(int)
# df.fillna('', inplace=True)
# print(df)

# # UPSERT 語法 (含時間戳記自動更新)
# upsert_query = """
# INSERT INTO ppl (
#     dept,
#     year,
#     team,
#     name,
#     ppl_id,
#     create_time,
#     update_time
# )
# VALUES (
#     %(dept)s,
#     %(year)s,
#     %(team)s,
#     %(name)s,
#     %(ppl_id)s,
#     NOW(),
#     NOW()
# )
# ON DUPLICATE KEY UPDATE
#     ppl_id = VALUES(ppl_id),
#     update_time = NOW();
# """
# try:
#     # 轉換 DataFrame 為字典列表
#     data_dict = df.to_dict('records')
#     # 批次執行（使用 executemany 提升效率）
#     db.executemany_query(sql_str=upsert_query, seq_of_params=data_dict, commit=True)
#     print(f"成功寫入 {len(df)} 筆資料！")
# except Exception as e:
#     print(f"資料庫操作失敗: {str(e)}")
# finally:
#     db.close()

