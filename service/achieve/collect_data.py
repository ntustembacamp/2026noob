import sys, os
sys.path.append(os.getenv('CONFIG_PATH'))
from config import PHOTO_MANAGEMENT_DB_PATH


from tools.db_wrapper import DatabaseWrapper

# CREATE TABLE persons (
# 	id INTEGER,
# 	name TEXT,
# 	team INTEGER,
# 	department TEXT,
# 	folder_name TEXT, grade INTEGER,
# 	CONSTRAINT PERSONS_PK PRIMARY KEY (id)
# );

# CREATE UNIQUE INDEX persons_folder_name_IDX ON persons (folder_name);

# CREATE TABLE tag_photo (
# 	id INTEGER NOT NULL,
# 	photo_id INTEGER,
# 	tag_name TEXT,
# 	tag_value TEXT,
# 	CONSTRAINT tag_photo_pk PRIMARY KEY (id)
# );

# CREATE UNIQUE INDEX tag_photo_photo_id_IDX ON tag_photo (photo_id,tag_value,tag_name);

# CREATE TABLE tag_photo (
# 	id INTEGER NOT NULL,
# 	photo_id INTEGER,
# 	tag_name TEXT,
# 	tag_value TEXT,
# 	CONSTRAINT tag_photo_pk PRIMARY KEY (id)
# );

# create a database object
db = DatabaseWrapper(f'{PHOTO_MANAGEMENT_DB_PATH}')

create_sql = '''
CREATE TABLE IF NOT EXISTS persons (
            id INTEGER PRIMARY KEY,
            name TEXT
        , team TEXT, department TEXT, folder_name TEXT);

CREATE UNIQUE INDEX IF NOT EXISTS persons_folder_name_IDX ON persons (folder_name);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    source_file_name TEXT,
    known_ppl INTEGER,
    unknown_ppl INTEGER,
    create_time TEXT,
    source_path TEXT,
    dest_file_name TEXT,
    exif_date TEXT,
    process_datetime TEXT,
    score REAL
);

CREATE TABLE IF NOT EXISTS tag_photo (
    id INTEGER NOT NULL,
    photo_id INTEGER, 
    tag_name TEXT,
    tag_value TEXT,
    CONSTRAINT tag_photo_pk PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS tag_photo_photo_id_IDX ON tag_photo (photo_id,tag_value,tag_name);
'''

# define a function to initialize the database
def initialize_db(db_name=f'{PHOTO_MANAGEMENT_DB_PATH}', create_table_sql=create_sql):
    db.initialize_db(db_name, create_table_sql)

# define a function to open a connection to the database
def connect_to_db(db_name=f'{PHOTO_MANAGEMENT_DB_PATH}'):
    db.connect_to_db(db_name)

# define a function to select records from persons table
def select_persons(columns=['team'], where_clause='folder_name=?', where_args=()):
    return db.select_records('persons', columns, where_clause, where_args)

# define a fucntion to intsert a record to persons table
def insert_person(name, team, department, folder_name, grade):
    db.insert_record('persons', ['name', 'team', 'department', 'folder_name', 'grade'], [name, team, department, folder_name, grade])

# define a function to insert a record to photos table
def insert_photo(source_file_name, known_ppl, unknown_ppl, create_time, source_path, dest_file_name, exif_date, process_datetime, score):
    db.insert_record('photos', ['source_file_name', 'known_ppl', 'unknown_ppl', 'create_time', 'source_path', 'dest_file_name', 'exif_date', 'process_datetime', 'score'], [source_file_name, known_ppl, unknown_ppl, create_time, source_path, dest_file_name, exif_date, process_datetime, score])

# define a function to update a record in photos table
def update_photo(dest_file_name, known_ppl, unknown_ppl, process_datetime):
    db.update_record('photos', 'known_ppl = ?, unknown_ppl = ?, process_datetime = ?', where_clause='dest_file_name = ?', all_values=(known_ppl, unknown_ppl, process_datetime, dest_file_name))


# define a function to insert a record to tag_photo table
# the input is a dest_file_name, and a list of tags which contains tag_name and tag_value
def insert_photo_tags(dest_file_name, tags):
    reslut = db.select_records('photos', ['id'], 'dest_file_name = ?', (dest_file_name,))
    teams = []
    if not reslut:
        return
    photo_id = reslut[0][0]
    # extract the tag_value into a list
    folder_name_list = [tag['tag_value'] for tag in tags]
    # according to the folder_name to create where_caluse with ?
    where_clause = 'folder_name in (' + ','.join(['?' for _ in folder_name_list]) + ')'
    where_clause += ' and team is not null group by team'
    reslut = db.select_records('persons', columns=['team','count(team) as cnt'], where_clause=where_clause, where_args=folder_name_list)
    if not reslut:
        return
    # get the team's percentage
    total = sum([r[1] for r in reslut])
    # if total > 3 then calculate the percentage
    # and if team percentage >= 0.5, add that team to the tag_photo table, tag_name = 'TEAM', tag_value = team
    if total > 3:
        team_dict = {r[0]: r[1]/total for r in reslut}
        for team in team_dict:
            if team_dict[team] >= 0.5:
                db.insert_record('tag_photo', ['photo_id','tag_name', 'tag_value'], [photo_id, 'TEAM', team])
                teams.append(f'Team {team:02}')
        #print(team_dict)
    for tag in tags:
        db.insert_record('tag_photo', ['photo_id','tag_name', 'tag_value'], [photo_id, tag['tag_name'], tag['tag_value']])
    return teams
