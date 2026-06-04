import os
import cv2
import insightface
import numpy as np
from sklearn import preprocessing
import pickle

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

# import collect_data as cd

import sys
sys.path.append(os.getenv('CONFIG_PATH'))
from config import FACE_MODEL_PATH, FACE_EMBEDDING_PATH, FACE_DB_PATH

print(f'FACE_MODEL_PATH: {FACE_MODEL_PATH}')
print(f'FACE_EMBEDDING_PATH: {FACE_EMBEDDING_PATH}')
print(f'FACE_DB_PATH: {FACE_DB_PATH}')

# def save_image_with_unicode_filename(image, filename):
#     # Encode image to png
#     is_success, im_buf_arr = cv2.imencode(".png", image)
#     byte_im = im_buf_arr.tobytes()

#     # Write to file
#     with open(filename, 'wb') as f_output:
#         f_output.write(byte_im)


class FaceRecognition:
    def __init__(self, gpu_id=0, face_db=FACE_DB_PATH, threshold=1.24, det_thresh=0.50, det_size=(640, 640)):
        """
        Face recognition utility class
        :param gpu_id: Positive number for GPU ID, negative number for CPU
        :param face_db: Face database folder
        :param threshold: Face recognition threshold
        :param det_thresh: Detection threshold
        :param det_size: Detection model image size
        """
        self.model_name = 'antelopev2'
        #self.model_name = 'buffalo_l'
        self.gpu_id = gpu_id
        self.face_db = face_db
        self.threshold = threshold
        self.det_thresh = det_thresh
        self.det_size = det_size

        # Load the face recognition model, when allowed_modules=['detection', 'recognition'], it only performs detection and recognition
        self.model = insightface.app.FaceAnalysis(root=FACE_MODEL_PATH,
                                                  name=self.model_name,
                                                  allowed_modules=None,
                                                  providers=['CUDAExecutionProvider'])
        self.model.prepare(ctx_id=self.gpu_id, det_thresh=self.det_thresh, det_size=self.det_size)

        self.faces_embedding = list()

        #self.load_faces(self)

    # load faces from pickle file
    def load_faces_from_pickle(self):
        with open(f'{FACE_EMBEDDING_PATH}faces_embedding_{self.model_name}.pkl', 'rb') as f:
            self.faces_embedding = pickle.load(f)

    # load faces from face_db folder
    def load_faces(self):
        if not os.path.exists(self.face_db):
            os.makedirs(self.face_db)
        # if 'faces_embedding.pkl' self.face_db rename it to faces_embedding.pkl.old
        model_name = self.model_name
        if os.path.exists(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl'):
            if os.path.exists(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl.old'):
                os.remove(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl.old')
            os.rename(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl',
                      f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl.old')
            
        for root, dirs, files in os.walk(self.face_db):
            for subdirectory in dirs:
                subdirectory_path = os.path.join(root, subdirectory)
                # Iterate over the contents of each first-level subdirectory
                for sub_root, sub_dirs, sub_files in os.walk(subdirectory_path):
                    for file in sub_files:
                        # check if the file is an image file, if not, skip
                        if file.split(".")[1].lower() not in ['jpg', 'jpeg', 'png']:
                            continue
                        input_image = cv2.imdecode(np.fromfile(os.path.join(sub_root, file), dtype=np.uint8), 1)
                        #user_name = file.split(".")[0]
                        # use the folder name as the user_name
                        user_name = os.path.basename(sub_root)
                        faces= self.model.get(input_image)
                        # if the image does not contain just one face, skip
                        if len(faces) != 1:
                            print(f'Error: {file} does not contain just one face')
                            continue
                        face = faces[0]
                        embedding = np.array(face.embedding).reshape((1, -1))
                        embedding = preprocessing.normalize(embedding)
                        self.faces_embedding.append({
                            "user_name": user_name,
                            "feature": embedding
                        })
                    break
            break

        with open(f'{FACE_EMBEDDING_PATH}faces_embedding_{model_name}.pkl', 'wb') as f:
            pickle.dump(self.faces_embedding, f)


    def recognition(self, image):
        faces = self.model.get(image)
        results = list()

        for face in faces:
            embedding = np.array(face.embedding).reshape((1, -1))
            embedding = preprocessing.normalize(embedding)
            user_name = "unknown"
            minimum_dist = self.threshold
            for saved_face in self.faces_embedding:
                print(saved_face["user_name"])
                diff = np.subtract(embedding, saved_face["feature"])
                dist = np.sum(np.square(diff), 1)
                print(dist)
                if dist < minimum_dist:
                    minimum_dist = dist
                    user_name = saved_face["user_name"]
                    face['name'] = user_name
                    print('same person')
                    
                '''
                r = self.feature_compare(embedding, saved_face["feature"], self.threshold)
                if r:
                    user_name = saved_face["user_name"]
                    face['name'] = user_name
                    break
                '''

            results.append(user_name)

        return results, faces

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
        user_folder = os.path.join(self.face_db, user_name)
        if not os.path.exists(user_folder):
            os.makedirs(user_folder)
        cv2.imencode('.png', image)[1].tofile(os.path.join(user_folder, '%s.png' % user_name))
        self.faces_embedding.append({
            "user_name": user_name,
            "feature": embedding
        })
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
