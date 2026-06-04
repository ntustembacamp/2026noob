import sys, os
sys.path.append(os.getenv('CONFIG_PATH'))
from config import PHOTO_MANAGEMENT_DB_PATH, FACE_DONE_PATH

from tools.db_wrapper import DatabaseWrapper
import tools.utils as utils
import platform
import argparse

# Initialize the parser
parser = argparse.ArgumentParser(description="input the destination folder name and SQL query to export the images")

# Add arguments for destination folder and SQL query
parser.add_argument('--dest_folder', type=str, required=True, help='The destination folder name')
parser.add_argument('--sql', type=str, required=True, help='The SQL query')

# Parse the arguments
args = parser.parse_args()

# Assign the parsed arguments to variables
dest_folder = args.dest_folder
sql = args.sql

# Custom validation example
if not dest_folder or not isinstance(dest_folder, str):
    print("Error: Destination folder name is invalid.")
    sys.exit(1)

if not sql or not isinstance(sql, str):
    print("Error: SQL query is invalid.")
    sys.exit(1)


# FACE_DONE_PATH = 'done_db'

os_type = platform.system()
if os_type == "Windows":
    EXPORT_PATH = 'search_result'
else:
    EXPORT_PATH = 'export_db_symlink'


#dest_folder = '測試模糊圖片'
#sql = "SELECT DISTINCT dest_file_name  from v_photo_tag vpt WHERE score < 0.55"


if not os.path.exists(EXPORT_PATH):
    os.makedirs(EXPORT_PATH)

target_dir = os.path.join(EXPORT_PATH, dest_folder)
if not os.path.exists(target_dir):
    os.makedirs(target_dir)

# select the desti_file_name from the photos table using the input SQL string

db = DatabaseWrapper(PHOTO_MANAGEMENT_DB_PATH)
# select the desti_file_name from the photos table using the input SQL string
dest_file_names = db.execute_sql(sql)
if not dest_file_names:
    print("No records found.")
    exit(0)
for r in dest_file_names:
    dest_file_name = r[0]
    original_file_path = os.path.join(FACE_DONE_PATH, dest_file_name)
    os_type = platform.system()
    if os_type == "Windows":
        utils.create_hardlink_for_image(original_file_path, target_dir, [dest_file_name],delay=False)
    else:
        current_folder = os.getcwd()
        abs_path = os.path.join(current_folder, FACE_DONE_PATH, dest_file_name)
        utils.create_symlink_for_image(abs_path, [EXPORT_PATH,dest_folder], [dest_file_name])
print("Export completed.")
