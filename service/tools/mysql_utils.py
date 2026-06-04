import mysql.connector
from mysql.connector import errorcode
import os
import sys
import datetime

sys.path.append(os.getenv('CONFIG_PATH'))
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PWD, MYSQL_DB


class mysqlconnector:
    def __init__(self):
        self.config = {
            'host': MYSQL_HOST,
            'user': MYSQL_USER,
            'password': MYSQL_PWD,
            'database': MYSQL_DB,
            'port': MYSQL_PORT,
            'charset': 'utf8mb4',
            'collation': 'utf8mb4_0900_ai_ci',
            'use_unicode': True
        }
        self.conn = None
        self.cursor = None

    def connect(self):
        try:
            self.conn = mysql.connector.connect(**self.config)
            self.cursor = self.conn.cursor()
            print(f"{datetime.datetime.now()} 連線成功")
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
                print(f"{datetime.datetime.now()} 帳號或密碼錯誤")
            elif err.errno == errorcode.ER_BAD_DB_ERROR:
                print(f"{datetime.datetime.now()} 資料庫不存在")
            else:
                print(f'{datetime.datetime.now()} {err}')
            self.conn = None


    def execute_query(self, sql_str, commit=False):
        """
        res=db.execute_query(sql_str=q, commit=False) \n
        df = pd.DataFrame(res["data"], columns=res["columns"]) \n
        """
        if self.conn is None or not self.conn.is_connected():
            print(f"{datetime.datetime.now()} 尚未連線資料庫")
            return
        try:
            self.cursor.execute(sql_str)
            if commit or sql_str.strip().split()[0].upper() in [
                    'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER'
            ]:
                self.conn.commit()
                print(f"{datetime.datetime.now()} 資料已提交 (commit)")
            if sql_str.strip().upper().startswith('SELECT') or sql_str.strip().upper().startswith(
                    'SHOW'):
                data = self.cursor.fetchall()
                columns = [col[0] for col in self.cursor.description]
                return {"columns": columns, "data": data}
            if sql_str.strip().upper().startswith('DESCRIBE'):
                data = self.cursor.fetchall()
                columns = [col[0] for col in self.cursor.description]
                return {"columns": columns, "data": data}
        except mysql.connector.Error as err:
            print(f"{datetime.datetime.now()} 執行 SQL 發生錯誤: {err}")

    def executemany_query(self, sql_str, seq_of_params, commit=False):
        if self.conn is None or not self.conn.is_connected():
            print(f"{datetime.datetime.now()} 尚未連線資料庫")
            return
        try:
            self.cursor.executemany(sql_str, seq_of_params)
            # 自動判斷或手動 commit
            if commit or sql_str.strip().split()[0].upper() in ['INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER']:
                self.conn.commit()
                print(f"{datetime.datetime.now()} 資料已提交 (commit)")
        except mysql.connector.Error as err:
            print(f"{datetime.datetime.now()} 執行 executemany 發生錯誤: {err}")


    def close(self):
        if self.cursor:
            self.cursor.close()
        if self.conn and self.conn.is_connected():
            self.conn.close()
            print(f"{datetime.datetime.now()} 連線已關閉")



# 以下為範例
# if __name__ == "__main__":
#     sql_str = "SHOW DATABASES"
#     db = mysqlconnector()
#     db.connect()
#     db.execute_query(sql_str)
#     db.close()
