import tools.sqlite_wrapper as wrapper

class DatabaseWrapper:
    def __init__(self, db_name=None):
        self.conn = None
        self.cursor = None
        self.db_name = db_name
        if db_name:
            self.connect_to_db(db_name)

    def initialize_db(self, db_name, create_table_sql):
        wrapper.initialize_db(db_name, create_table_sql)

    def connect_to_db(self, db_name, check_thread=False):
        self.conn, self.cursor = wrapper.connect_to_db(db_name=self.db_name, check_thread=check_thread)

    def insert_record(self, table_name, columns, values):
        if not self.conn or not self.cursor:
            self.connect_to_db(self.db_name)
        if self.conn and self.cursor:
            wrapper.insert_record(self.cursor, table_name, columns, values)
            self.commit_and_close()

    def select_records(self, table_name, columns, where_clause=None, where_args=()):
        if not self.conn or not self.cursor:
            self.connect_to_db(self.db_name)
        if self.conn and self.cursor:
            select_records = wrapper.select_records(self.cursor, table_name, columns, where_clause, where_args)
            self.commit_and_close()
            return select_records

    def update_record(self, table_name, set_clause, where_clause=None, all_values=()):
        if not self.conn or not self.cursor:
            self.connect_to_db(self.db_name)
        if self.conn and self.cursor:
            wrapper.update_record(self.cursor, table_name, set_clause, where_clause, all_values)
            self.commit_and_close()

    def commit_and_close(self):
        if self.conn:
            wrapper.commit_and_close(self.conn)
            self.conn = None
            self.cursor = None

    def execute_sql(self, sql):
        if not self.conn or not self.cursor:
            self.connect_to_db(self.db_name, check_thread=True)
        if self.conn and self.cursor:
            select_records = wrapper.execute_sql(self.cursor, sql)
            self.commit_and_close()
            return select_records
