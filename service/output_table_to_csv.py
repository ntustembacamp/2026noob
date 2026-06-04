from tools.mysql_utils import mysqlconnector
import pandas as pd

db = mysqlconnector()
db.connect()

file_path='/root/noob/database/table_csv_backup'

tbl_name = 'reco_result'

upsert_query=f"""
SELECT * 
FROM {tbl_name}
"""

df=db.execute_query(sql_str=upsert_query)

df=pd.DataFrame(data=df['data'], columns=df['columns'])

df.to_csv(f'{file_path}/{tbl_name}.csv', index=False, encoding='utf-8-sig')