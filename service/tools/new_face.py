import os
import cv2
import numpy as np
from sklearn import preprocessing
import pickle
import pandas as pd
import datetime

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

# Compatibility shim for older insightface drawing code on newer NumPy.
if not hasattr(np, 'int'):
    np.int = int

import sys
sys.path.append(os.getenv('CONFIG_PATH'))
from tools.face_recognition_core import SharedFaceRecognitionCore
from tools.mysql_utils import mysqlconnector
from config import FACE_MODEL_PATH, FACE_EMBEDDING_PATH


print(f'FACE_MODEL_PATH: {FACE_MODEL_PATH}')
print(f'FACE_EMBEDDING_PATH: {FACE_EMBEDDING_PATH}')
# 照片路徑

# def save_image_with_unicode_filename(image, filename):
#     # Encode image to png
#     is_success, im_buf_arr = cv2.imencode(".png", image)
#     byte_im = im_buf_arr.tobytes()

#     # Write to file
#     with open(filename, 'wb') as f_output:
#         f_output.write(byte_im)

class FaceRecognition:
    def __init__(self,
                 gpu_id=0,
                 threshold=1.24,
                 det_thresh=0.50,
                 det_size=(640, 640)):
        """
        Face recognition utility class
        :param gpu_id: Positive number for GPU ID, negative number for CPU
        :param threshold: Face recognition threshold
        :param det_thresh: Detection threshold
        :param det_size: Detection model image size
        """
        self.model_name = 'antelopev2'
        self.gpu_id = gpu_id
        self.threshold = threshold
        self.det_thresh = det_thresh
        self.det_size = det_size

        self.embedding_path = f'{FACE_EMBEDDING_PATH}faces_embedding_{self.model_name}.pkl'
        self._core = SharedFaceRecognitionCore(
            model_root=FACE_MODEL_PATH,
            embedding_path=self.embedding_path,
            model_name=self.model_name,
            gpu_id=self.gpu_id,
            threshold=self.threshold,
            det_thresh=self.det_thresh,
            det_size=self.det_size,
            allowed_modules=["detection", "recognition"],
            providers=['CUDAExecutionProvider', 'CPUExecutionProvider'],
            fallback_providers=['CPUExecutionProvider'],
            enable_probes=False,
        )
        self.model = self._core.model
        self.faces_embedding = self._core.faces_embedding

    def ensure_embedding_meta_table(self, db):
        create_sql = """
        CREATE TABLE IF NOT EXISTS face_embedding_meta (
            id INT AUTO_INCREMENT PRIMARY KEY,
            base_id INT NOT NULL,
            user_name VARCHAR(255) NOT NULL,
            file_path VARCHAR(512) NOT NULL,
            file_name VARCHAR(255) NOT NULL,
            phash VARCHAR(64) DEFAULT '',
            model_name VARCHAR(64) NOT NULL,
            embedding_exists TINYINT(1) NOT NULL DEFAULT 0,
            face_count INT NOT NULL DEFAULT 0,
            status VARCHAR(64) NOT NULL DEFAULT 'pending',
            error_message TEXT NULL,
            embedding_update_time DATETIME NULL,
            create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_face_embedding_meta_base_model (base_id, model_name),
            KEY idx_face_embedding_meta_user_name (user_name),
            KEY idx_face_embedding_meta_status (status)
        );
        """
        db.execute_query(create_sql, commit=True)

    def upsert_embedding_meta(self, db, rows):
        if not rows:
            return

        upsert_sql = """
        INSERT INTO face_embedding_meta (
            base_id,
            user_name,
            file_path,
            file_name,
            phash,
            model_name,
            embedding_exists,
            face_count,
            status,
            error_message,
            embedding_update_time,
            create_time,
            update_time
        )
        VALUES (
            %(base_id)s,
            %(user_name)s,
            %(file_path)s,
            %(file_name)s,
            %(phash)s,
            %(model_name)s,
            %(embedding_exists)s,
            %(face_count)s,
            %(status)s,
            %(error_message)s,
            %(embedding_update_time)s,
            NOW(),
            NOW()
        )
        ON DUPLICATE KEY UPDATE
            user_name = VALUES(user_name),
            file_path = VALUES(file_path),
            file_name = VALUES(file_name),
            phash = VALUES(phash),
            embedding_exists = VALUES(embedding_exists),
            face_count = VALUES(face_count),
            status = VALUES(status),
            error_message = VALUES(error_message),
            embedding_update_time = VALUES(embedding_update_time),
            update_time = NOW();
        """
        db.executemany_query(upsert_sql, rows, commit=True)

    # load faces from pickle file
    def load_faces_from_pickle(self):
        self._core.load_faces_from_pickle()

    # load faces from face_db folder
    # 照片路徑
    # 用來萃取每個人的基礎特徵
    def load_faces(self):
        # 如果faces_embedding.pkl存在，則改名為faces_embedding.pkl.old
        # 如果faces_embedding.pkl.old存在，則刪除faces_embedding.pkl.old，再將faces_embedding.pkl改名
        model_name = self.model_name
        if os.path.exists(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl'):
            if os.path.exists(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl.old'):
                os.remove(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl.old')
            os.rename(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl',
                      f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl.old')
        # SELECT BASE 取得每個人的名字及基礎特徵照片路徑
        db = mysqlconnector()
        db.connect()
        self.ensure_embedding_meta_table(db)
        q="""
        SELECT
            id AS base_id,
            CONCAT(dept,'_',year,'_',team,'_',name) user_name,
            file_path,
            file_name,
            phash,
            CONCAT(file_path,'/', file_name) file_full_path
        FROM base
        WHERE phash <> ''
        """
        print(q)
        res=db.execute_query(sql_str=q, commit=False)
        df = pd.DataFrame(res["data"], columns=res["columns"])

        # print(df)
        meta_rows = []
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        for i in range(0,len(df)):
            current_row = df.loc[i]
            print(f"{datetime.datetime.now()} {current_row['file_full_path']}")

            meta_row = {
                "base_id": int(current_row["base_id"]),
                "user_name": current_row["user_name"],
                "file_path": current_row["file_path"],
                "file_name": current_row["file_name"],
                "phash": current_row["phash"] or "",
                "model_name": model_name,
                "embedding_exists": 0,
                "face_count": 0,
                "status": "pending",
                "error_message": "",
                "embedding_update_time": None,
            }

            try:
                input_image = cv2.imdecode(np.fromfile(current_row["file_full_path"], dtype=np.uint8), 1)
                if input_image is None:
                    meta_row["status"] = "image_decode_failed"
                    meta_row["error_message"] = "cv2.imdecode returned None"
                    meta_rows.append(meta_row)
                    continue

                faces = self.model.get(input_image)
                meta_row["face_count"] = len(faces)

                if len(faces) != 1:
                    meta_row["status"] = "face_count_mismatch"
                    meta_row["error_message"] = f"expected 1 face, got {len(faces)}"
                    meta_rows.append(meta_row)
                    print(f"{datetime.datetime.now()} Error: {current_row['file_full_path']} expected 1 face, got {len(faces)}")
                    continue

                face = faces[0]
                embedding = np.array(face.embedding).reshape((1, -1))
                embedding = preprocessing.normalize(embedding)
                self.faces_embedding.append({"user_name": current_row["user_name"], "feature": embedding})
                meta_row["embedding_exists"] = 1
                meta_row["status"] = "ready"
                meta_row["embedding_update_time"] = now_str
                meta_rows.append(meta_row)
                print(f'{datetime.datetime.now()} feature: {embedding[0][:2]}')
            except Exception as exc:
                meta_row["status"] = "error"
                meta_row["error_message"] = str(exc)[:1000]
                meta_rows.append(meta_row)
                print(f"{datetime.datetime.now()} Error: {current_row['file_full_path']} {exc}")

        with open(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl', 'wb') as f:
            pickle.dump(self.faces_embedding, f)

        self.upsert_embedding_meta(db, meta_rows)
        db.close()

    def recognition(self, image):
        return self._core.recognition(image)

# 以下應該沒有用到？？？
    @staticmethod
    def feature_compare(feature1, feature2, threshold):
        diff = np.subtract(feature1, feature2)
        dist = np.sum(np.square(diff), 1)
        print(dist)
        if dist < threshold:
            print(dist)
            print('same person')
            return True
        else:
            return False

    def register(self, image, user_name):
        faces = self.model.get(image)
        if len(faces) != 1:
            return 'picture has no face or more than one face'
        embedding = np.array(faces[0].embedding).reshape((1, -1))
        embedding = preprocessing.normalize(embedding)
        is_exits = False
        for saved_face in self.faces_embedding:
            r = self.feature_compare(embedding, saved_face["feature"], self.threshold)
            if r:
                is_exits = True
        if is_exits:
            return 'already exists'
        # create a folder with the user_name inside the face_db folder
        # 照片路徑
        # user_folder = os.path.join(self.face_db, user_name)
        if not os.path.exists(user_folder):
            os.makedirs(user_folder)
        cv2.imencode('.png', image)[1].tofile(os.path.join(user_folder, '%s.png' % user_name))
        self.faces_embedding.append({"user_name": user_name, "feature": embedding})
        return "success"

    def detect(self, image):
        faces = self.model.get(image)
        results = list()
        for face in faces:
            result = dict()
            result["bbox"] = np.array(face.bbox).astype(np.int32).tolist()
            result["kps"] = np.array(face.kps).astype(np.int32).tolist()
            result["landmark_3d_68"] = np.array(face.landmark_3d_68).astype(np.int32).tolist()
            result["landmark_2d_106"] = np.array(face.landmark_2d_106).astype(np.int32).tolist()
            result["pose"] = np.array(face.pose).astype(np.int32).tolist()
            result["age"] = face.age
            gender = 'male'
            if face.gender == 0:
                gender = 'female'
            result["gender"] = gender
            embedding = np.array(face.embedding).reshape((1, -1))
            embedding = preprocessing.normalize(embedding)
            result["embedding"] = embedding
            results.append(result)
        return results
