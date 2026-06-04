from pyiqa import create_metric
import cv2
from pillow_heif import register_heif_opener
from tools.face import FaceRecognition
from PIL import Image
# 圖像質量

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
# 監控資料用

import numpy as np
# 運算用

import tools.collect_data as cd
import tools.utils as utils
# # 自建

# import io
import os
import time
from datetime import datetime
import platform
# 取得系統資訊

import sys
sys.path.append(os.getenv('CONFIG_PATH'))
from config import FACE_TEST_PATH, FACE_DONE_PATH, FACE_MARK_PATH, FACE_DB_PATH

print('載入所有設定路徑===')
print(f'FACE_TEST_PATH: {FACE_TEST_PATH}')
print(f'FACE_DONE_PATH: {FACE_DONE_PATH}')
print(f'FACE_MARK_PATH: {FACE_MARK_PATH}')
print(f'FACE_DB_PATH: {FACE_DB_PATH}')
print('載入所有設定路徑===')

# 判斷系統類型決定匯出EXPORT_PATH===
os_type = platform.system()
if os_type == "Windows":
    EXPORT_PATH = 'export_db'
else:
    EXPORT_PATH = 'export_db_symlink'
    # 捷徑檔
# 判斷系統類型決定匯出EXPORT_PATH===

LABEL_FACE_NAME = True
# 要不要標記臉部

# Create a metric object
# 建立圖像品質物件
metric = create_metric('clipiqa+')


# define a function to test the image quality, input is the PIL image, output is the image quality score
# 利用圖像品質物件判斷美學分數
def test_image_quality(image):
    score = metric(image)
    return score.item()

# 另存圖像 unicode，應該是解決亂碼問題？
def save_image_with_unicode_filename(image, filename):
    # Encode image to jpg
    is_success, im_buf_arr = cv2.imencode(".jpg", image)
    byte_im = im_buf_arr.tobytes()

    # Write to file
    with open(filename, 'wb') as f_output:
        f_output.write(byte_im)


# 讀取iphone格式照片
# 只要建立物件：register_heif_opener即可處理HEIC檔
def read_heic_image(file_path):
    register_heif_opener()
    image = Image.open(file_path)

    return image

face_recognition = FaceRecognition(face_db=FACE_DB_PATH)
face_recognition.load_faces_from_pickle()
print("Face recognition model loaded.")

# 監控資料夾物件
class MyHandler(FileSystemEventHandler):
    """
    event 指的是監控資料夾內動作
    event.event_type 事件名稱
    event.src_path
    event.dest_path
    """
    def __init__(self):
        self.existing_files = set()
    def on_modified(self, event):
        # call the on_created evnet hanlder
        print(f"File modified: {event.src_path}")
        #self.on_created(event)

    def on_moved(self, event):
        # call the on_created evnet hanlder
        print(f"File moved: {event.src_path} to {event.dest_path}")
        #self.on_created(event)

    def on_created(self, event):
        print(f"event type: {event.event_type}  path : {event.src_path}")

        # 可保留
        # 排除 建立資料夾 或 被刪除檔案
        if event.is_directory:
            return
        if not os.path.exists(event.src_path):
            return

        # 可保留
        # 取得 檔案路徑 及 檔案名稱
        root = os.path.dirname(event.src_path)
        file = os.path.basename(event.src_path)

        # 可保留
        # 確認檔案已完成存入資料夾
        # otherwise, Image open will raise an error about permission denied, it's because the file is still being written
        # 如果為完成存入則休息一段時間再重新檢查
        prev_size = -1
        while True:
            curr_size = os.path.getsize(event.src_path)
            if curr_size == prev_size:
                break
            prev_size = curr_size
            time.sleep(0.5)  # Wait for 0.5 second before checking again

        # 可保留
        # 如果不是指定副檔名則不處理
        # Lower the file extension and check if it is an image file including heic
        if  file.lower().endswith(('.png', '.jpg', '.jpeg', '.heic')) == False:
            
            return

        # set the file_name to the file name without extension
        file_name_without_extension, file_extension  = os.path.splitext(file)

        if file.lower().endswith('.heic'):
            # 如果是HEIC檔 則先轉為 JPEG
            # 依據 有無exif檔處理，將JPEG存到DONE_DB???
            pil_image = read_heic_image(os.path.join(root, file))
            exif_data = pil_image.getexif()
            new_file_name = f"{file_name_without_extension}.jpg"
            new_fiel_path = os.path.join(root,new_file_name)

            if exif_data:
                pil_image.save(new_fiel_path, "JPEG",exif=exif_data)
            else:
                pil_image.save(new_fiel_path, "JPEG")

            done_path = os.path.join(FACE_DONE_PATH, file)
            os.rename(event.src_path, done_path)
            return
        else:
            # 如果是非HEIC檔 則直接讀檔
            #input_image = cv2.imdecode(np.fromfile(os.path.join(root, file), dtype=np.uint8), 1)
            pil_image = Image.open(os.path.join(root, file))

        # 取出 拍攝日期
        exif_date = utils.get_image_creation_date(pil_image)

        # 調整大小至高1200
        resized_image = utils.resize_image_auto_width(pil_image, fixed_height=1200)

        # 圖像品質物件判斷美學分數
        score = test_image_quality(resized_image)
        # 隨機生成序號作為資料庫key值 及 DONE_DB資料夾內的檔案名稱
        dest_file_name = utils.generate_unique_filename(score, extension=file_extension)

        # insert the photo information to the database
        create_time = datetime.fromtimestamp(int(os.path.getctime(os.path.join(root, file))))
        cd.insert_photo(file, None, None, create_time, root, dest_file_name, exif_date, None, score)

        # convert the PIL image to cv2 image
        # 轉成cv2才能進模型 face_recognition
        # close the PIL image, otherwise the file will be locked and can't be moved
        input_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
        pil_image.close()

        # 開始辨識照片中每張臉
        # results: 辨識結果
        # faces: 臉的數量?
        results, faces = face_recognition.recognition(input_image)
        teams = None
        # 初始化變數: 小隊

        # 如果有偵測出人臉則計算已知臉數量、未知臉數量，儲存至資料庫：photo_management，最後儲存人臉標記 結果至FACE_MARK_PATH
        # 如果未偵測出人臉則已知臉數量為0，未知臉數量為0，儲存至資料庫：photo_management
        if len(results) > 0:
            unknown_count = results.count("unknown")
            known_count = len(results) - unknown_count

            # 將資料庫中該照片的 已知臉 與 未知臉數量更新 並打上 update_time
            cd.update_photo(dest_file_name, known_count, unknown_count, datetime.now())
            print("=====================================")

            # create a list of the object wich contain the tag_name = 'PERSON', 'tag_value' = the name of the person
            tags = list()
            for result in results:
                if result != "unknown":
                    tags.append({"tag_name": "PERSON", "tag_value": result})
                print(f"識別結果:{result} 在 {file} 裡面")

            teams = cd.insert_photo_tags(dest_file_name, tags)
            # 計算小隊比例，後面會用到
            print(f'teams:{teams}')

            if LABEL_FACE_NAME:
                rimg = face_recognition.model.draw_on(input_image, faces)
                try:
                    save_image_with_unicode_filename(rimg, f"{FACE_MARK_PATH}{file_name_without_extension}_mark.jpg")
                except Exception as e:
                    print("Error save_image_with_unicode_filename: ", e)

        else:
            cd.update_photo(dest_file_name, 0, 0, datetime.now())

        # 將辨識完成照片移動至DONE_DB資料夾
        try:
            # 將照片名稱重新命名為 資料庫key值
            original_file_path = os.path.join(root, file)
            done_path = os.path.join(FACE_DONE_PATH, dest_file_name)

            dest_file_list = [dest_file_name]

            # 依照小隊資料夾分類
            #if teams is not None and Lengh > 0 append the team name to the dest_file_list
            if teams:
                for team in teams:
                    dest_file_list.append(f"{team}{os.sep}{dest_file_name}")
                    print(f"{team}{os.sep}{dest_file_name}")
                    print(f'team:{team}')
                    print(f'os.sep:{os.sep}')
                    print(f'dest_file_name:{dest_file_name}')
            else:
                pass
            # 搬檔並重新命名
            print(f'original_file_path: {original_file_path}')
            print(f'done_path: {done_path}')
            os.rename(original_file_path, done_path)
            print('完成資料庫key值 重新命名並 搬檔至DONE_DB資料夾')

            # # 用不到，因此註解
            # # 依照作業系統進行不同處理，製作捷徑
            # if os_type == "Windows":
            #     utils.create_hardlink_for_image(original_file_path, EXPORT_PATH, dest_file_list)
            #     os.rename(original_file_path, done_path)
            # else:
            #     print('判斷為非win系統 進行搬檔')
            #     os.rename(original_file_path, done_path)
            #     print('完成資料庫key值 重新命名並 搬檔至DONE_DB資料夾')

            #     current_folder = os.getcwd()
            #     print(f'找出目前資料夾: {current_folder}')
            #     abs_path = os.path.join(current_folder, done_path)
            #     print(f'找出絕對路徑: {abs_path}')
            #     target_sub_dir = root.split(os.sep)
            #     print(f'target_sub_dir: {target_sub_dir}')

            #     idx = target_sub_dir.index(FACE_TEST_PATH)
            #     print(f'idx: {idx}')
            #     target_sub_dir = target_sub_dir[idx+1:]
            #     print(f'target_sub_dir[idx+1:]: {target_sub_dir}')
            #     target_sub_dir.insert(0, EXPORT_PATH)

            #     utils.create_symlink_for_image(abs_path, target_sub_dir, dest_file_list)

        except Exception as e:
            print("Error: move file to done_db", e)


# 以下為主程式的部分====
# 監控資料夾路徑
folder_to_track = FACE_TEST_PATH
# 建立監控資料夾物件
event_handler = MyHandler()
observer = Observer()

# 啟用監控資料夾物件
observer.schedule(event_handler, folder_to_track, recursive=True)
observer.start()
print("System is ready")

# 每秒執行一次指定資料夾
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    observer.stop()
observer.join()
