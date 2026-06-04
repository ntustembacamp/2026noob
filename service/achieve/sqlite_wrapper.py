import sqlite3

def connect_to_db(db_name, check_thread=False):
    """Connect to an SQLite database and return the connection and cursor."""
    try:
        conn = sqlite3.connect(db_name,check_same_thread=check_thread)
        cursor = conn.cursor()
        return conn, cursor
    except sqlite3.Error as e:
        print(f"Error connecting to database: {e}")
        return None, None

def insert_record(cursor, table, columns, values):
    """Insert a record into the specified table."""
    try:
        columns_str = ', '.join(columns)
        placeholders = ', '.join(['?'] * len(values))
        cursor.execute(f'INSERT INTO {table} ({columns_str}) VALUES ({placeholders})', values)
    except sqlite3.Error as e:
        print(f"Error inserting data: {e}")

def select_records(cursor, table, columns, where_clause=None, where_args=()):
    """Select records from the specified table with optional WHERE clause."""
    try:
        columns_str = ', '.join(columns)
        query = f'SELECT {columns_str} FROM {table}'
        if where_clause:
            query += f' WHERE {where_clause}'
        cursor.execute(query, where_args)
        return cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Error selecting data: {e}")
        return None

def update_record(cursor, table, set_clause, where_clause=None, all_values=()):
    """Update records in the specified table with optional WHERE clause."""
    try:
        query = f'UPDATE {table} SET {set_clause}'
        if where_clause:
            query += f' WHERE {where_clause}'
        cursor.execute(query, all_values)
    except sqlite3.Error as e:
        print(f"Error updating data: {e}")

def commit_and_close(conn):
    """Commit the transaction and close the database connection."""
    try:
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"Error committing or closing the connection: {e}")

def create_table(cursor, create_table_sql):
    """Create a table using the provided SQL statement."""
    try:
        cursor.execute(create_table_sql)
    except sqlite3.Error as e:
        print(f"Error creating table: {e}")

def initialize_db(db_name, create_table_sql):
    """Initialize the database by connecting, creating a table, and closing the connection."""
    conn, cursor = connect_to_db(db_name)
    if conn and cursor:
        create_table(cursor, create_table_sql)
        commit_and_close(conn)

# define a function to accept the SQL command and return the result
def execute_sql(cursor, sql):
    """Execute an SQL command and return the result."""
    try:
        cursor.execute(sql)
        return cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Error executing SQL command: {e}")
        return None

if __name__ == '__main__':

    # Example usage:

    # SQL statement to create a table
    create_table_sql = '''
        CREATE TABLE IF NOT EXISTS my_table (
            id INTEGER PRIMARY KEY,
            name TEXT,
            age INTEGER
        )
    '''

    # Initialize the database and create the table
    initialize_db('example.db', create_table_sql)

    # Connect to the database
    conn, cursor = connect_to_db('example.db')

    # Insert a record
    if conn and cursor:
        insert_record(cursor, 'my_table', ['name', 'age'], ['Alice', 30])
        commit_and_close(conn)

    # Select records
    conn, cursor = connect_to_db('example.db')
    if conn and cursor:
        records = select_records(cursor, 'my_table', ['id', 'name', 'age'])
        print(records)
        commit_and_close(conn)

    # Update a record
    conn, cursor = connect_to_db('example.db')
    if conn and cursor:
        update_record(cursor, 'my_table', 'age = ?, name = ?',where_clause='id=?',all_values=(35, 'Alice',1))
        commit_and_close(conn)
