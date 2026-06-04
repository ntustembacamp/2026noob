# Download Database and Model, db_model_list.csv path
# for v1.0
DB_MODEL_ROOT_PATH = '/root/noob/'
DB_MODEL_DL_LIST_PATH = '/root/noob/infrasturcture/db_model_list.csv'

# Database Path for db_wrapper
# for v1.0
DB_FILE_PATH = '/root/noob/database/'
FACE_TEST_PATH = '/root/noob/database/test_db/'
FACE_DONE_PATH = '/root/noob/database/done_db/'
FACE_MARK_PATH = '/root/noob/database/mark_db/'
FACE_DB_PATH = '/root/noob/database/face_db/'
PHOTO_MANAGEMENT_DB_PATH = '/root/noob/database/photo_management.db'

# face.py 模型路徑
FACE_MODEL_PATH = '/root/noob/service/'
FACE_EMBEDDING_PATH = '/root/noob/service/embedding/'


# MySQL Database Config
# for v2.0
# This workspace currently runs the API and MySQL through Docker compose,
# so the API container should reach MySQL by container name on custom_network.
MYSQL_HOST = 'db'
MYSQL_PORT = 3306
MYSQL_USER = 'myuser'
MYSQL_PWD = 'mypassword'
MYSQL_DB = 'mydatabase'
MYSQL_ROOT_PWD = 'rootpassword'


FEATURE_PATH='/mnt/feature_src'


# log儲存位置
RECOGNIZE_LOG='/root/noob/logs/activity_photo_reco.log'
