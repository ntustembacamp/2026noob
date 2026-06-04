import sys, os
sys.path.append(os.getenv('CONFIG_PATH'))
from tools.mysql_utils import mysqlconnector

import pandas as pd


# INSERT DATA
db = mysqlconnector()
db.connect()

# 指定檔案
csv_file_path='/root/noob/database/table_csv_backup'
csv_file_name='img_tag_1'


q=f"""
SELECT * 
FROM {csv_file_name}
LIMIT 1
"""

df=db.execute_query(sql_str=q)
df=pd.DataFrame(data=df['data'], columns=df['columns'])

df_columns = list(df.columns)
insert_columns_str = ', '.join(df_columns)
values_columns_str = ', '.join([f'%({col})s' for col in df_columns])

df = pd.read_csv(f'{csv_file_path}/{csv_file_name}.csv', encoding='utf-8-sig')
print(df)

# INSERT 
q = f"""
INSERT INTO {csv_file_name} (
    {insert_columns_str}
)
VALUES (
    {values_columns_str}
)
"""
try:
    # 轉換 DataFrame 為字典列表
    data_dict = df.to_dict('records')
    # 批次執行（使用 executemany 提升效率）
    db.executemany_query(sql_str=q, seq_of_params=data_dict, commit=True)
    print(f"成功寫入 {len(df)} 筆資料！")
except Exception as e:
    print(f"資料庫操作失敗: {str(e)}")
finally:
    db.close()

