import sys, os
sys.path.append(os.getenv('CONFIG_PATH'))
from config import FACE_DB_PATH

from tools.face import FaceRecognition
import tools.collect_data as cd

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

for root, dirs, files in os.walk(FACE_DB_PATH):
    for directory in dirs:
        folder_name_split = directory.split('_')
        if len(folder_name_split) != 4:
            name = folder_name_split[0]
            team = None
            department = '老師'
            grade = None
        else:
            grade = folder_name_split[0]
            team = folder_name_split[1]
            # if the team is XX then set team to None
            team = None if grade != '113' else team
            department = folder_name_split[2]
            name = folder_name_split[3]
            # insert the person to the persons table
            # insert_person(name, team, department, folder_name, grade):
        cd.insert_person(name, team, department, directory, grade)
    break

face_recognition = FaceRecognition(face_db=FACE_DB_PATH)

# Load faces from the face_db folder and save the embedding to a pickle file
face_recognition.load_faces()
