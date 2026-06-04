import sys, os
sys.path.append(os.getenv('CONFIG_PATH'))
from config import FACE_DONE_PATH, PHOTO_MANAGEMENT_DB_PATH

from tools.db_wrapper import DatabaseWrapper
import tools.utils as utils
import platform
import csv
import io

# FACE_DONE_PATH = 'done_db'

os_type = platform.system()
if os_type == "Windows":
    EXPORT_PATH = 'export_by_time'
else:
    EXPORT_PATH = 'export_db_symlink'

if not os.path.exists(EXPORT_PATH):
    os.makedirs(EXPORT_PATH)

db = DatabaseWrapper(PHOTO_MANAGEMENT_DB_PATH)

# Read from the standard input (which will be the piped file stream)
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
csvfile = sys.stdin
# csvfile = open('activities.csv', mode='r', newline='', encoding='utf-8')

csvreader = csv.reader(csvfile)

# Skip the header row if your CSV file has one
next(csvreader, None)
# Iterate through each row in the CSV file
for row in csvreader:
    # Extract the values from the row and set them to variables
    begin_time = row[0]
    # if begin_time is begin with #, then it is a comment line, skip it
    if begin_time.startswith('#'):
        continue
    end_time = row[1]
    dest_folder = row[2]

    target_dir = os.path.join(EXPORT_PATH, dest_folder)
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    sql = (
        "SELECT dest_file_name, id "
        "FROM photos "
        "WHERE"
        "  (exif_date IS NOT NULL AND exif_date BETWEEN '{begin_time}' AND '{end_time}')"
        "  OR"
        "  (exif_date IS NULL AND create_time BETWEEN '{begin_time}' AND '{end_time}')"
    ).format(begin_time=begin_time, end_time=end_time)

    # print sql command
    print(sql)

    result = db.execute_sql(sql)
    dest_file_name_list = []
    # check if the result is empty
    if not result:
        print("No records found.")
        continue
    for r in result:
        dest_file_name = r[0]
        photo_id = r[1]
        original_file_path = os.path.join(FACE_DONE_PATH, dest_file_name)
        dest_file_name_list.append(dest_file_name)
        team_result = db.select_records('tag_photo', ['tag_value'], 'photo_id = ? AND tag_name = ?', (photo_id, 'TEAM'))
        if team_result:
            team = int(team_result[0][0])
            team_name = f'Team {team:02}'
            dest_file_name_list.append(f"{team_name}{os.sep}{dest_file_name}")
        os_type = platform.system()
        if os_type == "Windows":
            utils.create_hardlink_for_image(original_file_path, target_dir, dest_file_name_list, delay=False)
        else:
            current_folder = os.getcwd()
            abs_path = os.path.join(current_folder, FACE_DONE_PATH, dest_file_name)
            utils.create_symlink_for_image(abs_path, [EXPORT_PATH,dest_folder], dest_file_name_list)
    print("Export completed.")







#dest_folder = '測試模糊圖片'
#sql = "SELECT DISTINCT dest_file_name  from v_photo_tag vpt WHERE score < 0.55"




# select the desti_file_name from the photos table using the input SQL string


# select the desti_file_name from the photos table using the input SQL string
