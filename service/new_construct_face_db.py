# 特徵建構流程
# 1. 執行 new_insert_feature_src.py
# 2. 執行 new_construct_face_db.py

# 從table: base 取得所有大頭照路徑 並取得特徵
# 將特徵儲存至 embedding/pkl 內

import sys, os
sys.path.append(os.getenv('CONFIG_PATH'))
from tools.new_face import FaceRecognition

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

# Load faces and save the embedding to a pickle file
face_recognition = FaceRecognition()
face_recognition.load_faces()
