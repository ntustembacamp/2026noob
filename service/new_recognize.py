from pyiqa import create_metric
import torch
import cv2
from pillow_heif import register_heif_opener
from PIL import Image
from PIL.ExifTags import TAGS
# 圖像質量

import numpy as np
import json
import pandas as pd
# pd.set_option('display.max_columns', None)
# 運算用

from tools.new_face import FaceRecognition
import tools.utils as utils
from tools.mysql_utils import mysqlconnector
# 自建

import os
import datetime
import contextlib
from pathlib import Path
from zoneinfo import ZoneInfo
# 取得系統資訊

import sys
sys.path.append(os.getenv('CONFIG_PATH'))
from config import FACE_MARK_PATH, RECOGNIZE_LOG

# === logging 設定區塊 ===
import logging
from logging import Handler

LOG_TIMEZONE = os.getenv('LOG_TIMEZONE', 'Asia/Taipei')
LOG_TZ = ZoneInfo(LOG_TIMEZONE)


def _now_log_tz():
    return datetime.datetime.now(LOG_TZ)


def _daily_reco_log_path():
    base = Path(RECOGNIZE_LOG)
    return base.with_name(f"activity_photo_reco_{_now_log_tz().strftime('%Y%m%d')}.log")


def _write_reco_compat_pointer(daily_path: Path):
    base = Path(RECOGNIZE_LOG)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        f"latest_daily_log={daily_path}\nupdated_at={_now_log_tz().isoformat(timespec='seconds')}\n",
        encoding='utf-8-sig'
    )


class TZFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=LOG_TZ)
        return dt.strftime(datefmt) if datefmt else dt.isoformat(sep=' ', timespec='seconds')


class DailyRecoFileHandler(Handler):
    def __init__(self):
        super().__init__()
        self._current_date = ''
        self._inner = None
        self._refresh()

    def _refresh(self):
        current_date = _now_log_tz().strftime('%Y%m%d')
        if self._inner is not None and current_date == self._current_date:
            return
        if self._inner is not None:
            self._inner.close()
        daily_path = _daily_reco_log_path()
        daily_path.parent.mkdir(parents=True, exist_ok=True)
        self._inner = logging.FileHandler(daily_path, mode='a', encoding='utf-8-sig')
        if self.formatter:
            self._inner.setFormatter(self.formatter)
        self._current_date = current_date
        _write_reco_compat_pointer(daily_path)

    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        if self._inner is not None:
            self._inner.setFormatter(fmt)

    def emit(self, record):
        self._refresh()
        if self._inner is not None:
            self._inner.emit(record)

    def close(self):
        if self._inner is not None:
            self._inner.close()
        super().close()


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False
_reco_formatter = TZFormatter('%(asctime)s - PID:%(process)d - %(levelname)s - %(message)s')
_reco_handler = DailyRecoFileHandler()
_reco_handler.setFormatter(_reco_formatter)
logger.addHandler(_reco_handler)

print(f'{datetime.datetime.now()} 載入所有設定路徑===')
# logger.info('載入所有設定路徑===')

print(f'{datetime.datetime.now()} FACE_MARK_PATH: {FACE_MARK_PATH}')
# logger.info(f'FACE_MARK_PATH: {FACE_MARK_PATH}')


# Create a metric object
# 建立圖像品質物件
device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
print('torch status:',torch.cuda.is_available())

metric = None
metric_device = None
# ... # 利用圖像品質物件判斷美學分數
def get_image_metric(force_device=None):
    global metric, metric_device
    target_device = force_device or device
    target_device_name = str(target_device)
    if metric is None or metric_device != target_device_name:
        logger.info(f'Initializing clipiqa+ metric on {target_device_name}')
        metric = create_metric('clipiqa+', device=target_device)
        metric_device = target_device_name
    return metric


def image_socre(file_path):
    """
    file_path 設定為 ai server 資料夾路徑 \n 
    /mnt/activity/dev/thumbs/檔案名稱 \n
    /mnt/activity/dev/origin/檔案名稱 \n
    """
    print(f'{datetime.datetime.now()} file_path: {file_path}, 開始評分')
    logger.info(f'file_path: {file_path}, 開始評分')
    pil_image = Image.open(file_path)
    try:
        score = get_image_metric()(pil_image).item()
    except RuntimeError as exc:
        error_text = str(exc)
        if "CUDA error" in error_text or "no kernel image is available for execution on the device" in error_text:
            logger.warning(f'clipiqa+ CUDA failed, fallback to CPU: {error_text}')
            score = get_image_metric(torch.device("cpu"))(pil_image).item()
        else:
            raise
    print(f'{datetime.datetime.now()} file_path: {file_path}, score: {score}')
    logger.info(f'file_path: {file_path}, score: {score}')
    return score


def extract_photo_taken_time(exif_data):
    if not exif_data:
        return None

    # Prefer DateTimeOriginal from the EXIF IFD when available.
    with contextlib.suppress(Exception):
        exif_ifd = exif_data.get_ifd(0x8769)
        if exif_ifd:
            for key, value in exif_ifd.items():
                if TAGS.get(key, key) == 'DateTimeOriginal' and value:
                    return datetime.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")

    # Fallback to any top-level EXIF datetime fields.
    for key, value in exif_data.items():
        tag_name = TAGS.get(key, key)
        if tag_name in ('DateTimeOriginal', 'DateTimeDigitized', 'DateTime') and value:
            with contextlib.suppress(ValueError, TypeError):
                return datetime.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")

    return None

# 另存圖像 unicode，應該是解決亂碼問題？
def save_image_with_unicode_filename(image, filename):
    output_dir = os.path.dirname(filename)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Encode image to jpg
    is_success, im_buf_arr = cv2.imencode(".jpg", image)
    if not is_success:
        raise ValueError(f"cv2.imencode failed for {filename}")

    byte_im = im_buf_arr.tobytes()

    # Write to file...
    with open(filename, 'wb') as f_output:
        f_output.write(byte_im)

# 讀取iphone格式照片
# 只要建立物件：register_heif_opener即可處理HEIC檔
def read_heic_image(file_path):
    register_heif_opener()
    image = Image.open(file_path)
    return image

face_recognition = FaceRecognition()
face_recognition.load_faces_from_pickle()

# print(f"{datetime.datetime.now()} Face recognition model loaded.")
# logger.info("Face recognition model loaded.")

def activity_photo_reco(file_full_path: str, LABEL_FACE_NAME=False):
    # Lower the file extension and check if it is an image file including heic
    if file_full_path.lower().endswith(('.png', '.jpg', '.jpeg', '.heic')) == False:
        return
    # ...     db = mysqlconnector()
    db = mysqlconnector()
    db.connect()

    reco_res_df = pd.DataFrame(columns=[
        'origin_full_path', 'thumbs_full_path', 'photo_taken_time', 'reco_count', 'reco_unknow', 'reco_res', 'reco_name', 'create_time', 'update_time'
    ])

    # set the file_name to the file name without extension
    file_path_with_name, file_extension = os.path.splitext(file_full_path)
    file_name = file_path_with_name.split('/')[file_path_with_name.split('/').__len__() - 1]
    exclude_len = file_name.__len__() * -1
    file_path = file_path_with_name[:exclude_len]

    if file_full_path.lower().endswith('.heic'):
        # 如果是HEIC檔 則先轉為 JPEG
        # print(f'{datetime.datetime.now()} 檔案為heic')
        # logger.info('檔案為heic')
        pil_image = read_heic_image(file_full_path)
        exif_data = pil_image.getexif()

        file_name = f"{file_name}.jpg"
        file_full_path = os.path.join(file_path, file_name)

        reco_res_df.loc[0, 'origin_full_path'] = file_full_path
        reco_res_df.loc[0, 'thumbs_full_path'] = file_full_path.replace('origin','thumbs')

        if exif_data:
            # print(f'{datetime.datetime.now()} 有exif檔')
            # logger.info('有exif檔')
            pil_image.save(file_full_path, "JPEG", exif=exif_data)
        else:
            # print(f'{datetime.datetime.now()} 無exif檔')
            # logger.info('無exif檔')
            pil_image.save(file_full_path, "JPEG")
    else:
        # 如果是非HEIC檔 則直接讀檔
        # print(f'{datetime.datetime.now()} 檔案非heic')
        # logger.info('檔案非heic')

        reco_res_df.loc[0, 'origin_full_path'] = file_full_path
        reco_res_df.loc[0, 'thumbs_full_path'] = file_full_path.replace('origin','thumbs')

        pil_image = Image.open(file_full_path)
        exif_data = pil_image.getexif()

    photo_taken_time = extract_photo_taken_time(exif_data)
    reco_res_df.loc[0, 'photo_taken_time'] = photo_taken_time
    logger.info(f'file_path: {file_full_path}, photo_taken_time: {photo_taken_time}')

    input_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
    pil_image.close()

    # 開始辨識照片中每張臉
    # results: 辨識結果
    # faces: 每張臉的詳細資料
    results, faces = face_recognition.recognition(input_image)
    print(f'{datetime.datetime.now()} results: {results}')
    logger.info(f'results: {results}')

    face_position = []
    for item in faces:
        face_position.append({
            "name":item.get("name"),
            "det_score":float(item.get("det_score")),
            "bbox":item.get("bbox").tolist()
            if isinstance(item.get("bbox"), np.ndarray) else item.get("bbox")
        })

    # 如果有偵測出人臉則計算已知臉數量、未知臉數量、人臉標記，儲存至資料庫
    # 依照 LABEL_FACE_NAME 將標記照片儲存至FACE_MARK_PATH
    if len(results) > 0:
        reco_unknow = results.count("unknown")
        reco_count = results.__len__() - reco_unknow

        print(f'{datetime.datetime.now()} 辨識總數量: {results.__len__()}, 已辨識數量: {reco_count}, 未辨識數量: {reco_unknow}')
        logger.info(f'辨識總數量: {results.__len__()}, 已辨識數量: {reco_count}, 未辨識數量: {reco_unknow}')

        # print(f'{datetime.datetime.now()} 已辨識數量: {reco_count}')
        # logger.info(f'已辨識數量: {reco_count}')

        reco_res_df.loc[0, 'reco_unknow'] = reco_unknow
        reco_res_df.loc[0, 'reco_count'] = reco_count

        # create a list of the object wich contain the tag_name = 'PERSON', 'tag_value' = the name of the person
        tags = list()
        for result in results:
            print(f"{datetime.datetime.now()} 識別結果:{result} 在 {file_full_path} 裡面")
            logger.info(f"識別結果:{result} 在 {file_full_path} 裡面")
            
            if result != "unknown":
                tags.append({"tag_name": "PERSON", "tag_value": result})
                # print(f"{datetime.datetime.now()} 識別結果:{result} 在 {file_full_path} 裡面")
                # logger.info(f"識別結果:{result} 在 {file_full_path} 裡面")

        reco_res_df.loc[0, 'reco_res'] = json.dumps(face_position, ensure_ascii=False, indent=2)
        reco_res_df.loc[0, 'reco_name'] = json.dumps(results, ensure_ascii=False)
        

        if LABEL_FACE_NAME:
            mark_file_path = os.path.join(FACE_MARK_PATH, f"{file_name}_mark.jpg")
            logger.info(f'start saving marked face image to {mark_file_path}')
            rimg = face_recognition.model.draw_on(input_image, faces)
            try:
                save_image_with_unicode_filename(rimg, mark_file_path)
                logger.info(f'marked face image saved to {mark_file_path}')
            except Exception as e:
                print("Error save_image_with_unicode_filename: ", e)
                logger.error(f"Error save_image_with_unicode_filename: {e}")
    else:
        print(f'{datetime.datetime.now()} 照片無人臉')
        logger.info('照片無人臉')

        reco_res_df.loc[0, 'origin_full_path'] = file_full_path
        reco_res_df.loc[0, 'thumbs_full_path'] = file_full_path.replace('origin','thumbs')
        reco_res_df.loc[0,'reco_count'] = 0
        reco_res_df.loc[0,'reco_unknow'] = 0
        reco_res_df.loc[0,'reco_res'] = ''
        reco_res_df.loc[0, 'reco_name'] = ''

    # 開始UPSERT 儲存 辨識結果
    upsert_query = """
    INSERT INTO reco_result (
        origin_full_path,
        thumbs_full_path,
        photo_taken_time,
        reco_count,
        reco_unknow,
        reco_res,
        reco_name,
        create_time,
        update_time
    )
    VALUES (
        %(origin_full_path)s,
        %(thumbs_full_path)s,
        %(photo_taken_time)s,
        %(reco_count)s,
        %(reco_unknow)s,
        %(reco_res)s,
        %(reco_name)s,
        NOW(),
        NOW()
    )
    ON DUPLICATE KEY UPDATE
        photo_taken_time = VALUES(photo_taken_time),
        reco_count = VALUES(reco_count),
        reco_unknow = VALUES(reco_unknow),
        reco_res = VALUES(reco_res),
        reco_name = VALUES(reco_name),
        update_time = NOW();
    """
    try:
        # 轉換 DataFrame 為字典列表
        data_dict = reco_res_df.to_dict('records')
        logger.info(f'{data_dict}\n')
        # 批次執行（使用 executemany 提升效率）
        db.executemany_query(sql_str=upsert_query, seq_of_params=data_dict, commit=True)
        # print(f"{datetime.datetime.now()} 成功寫入 {len(reco_res_df)} 筆資料！")
        logger.info(f'{upsert_query}\n')
        logger.info(f"成功寫入 {len(reco_res_df)} 筆資料！")
    except Exception as e:
        print(f"{datetime.datetime.now()} 資料庫操作失敗: {str(e)}")
        logger.error(f"資料庫操作失敗: {str(e)}")
    finally:
        db.close()



    

# activity_photo_reco(file_full_path='/mnt/activity/dev/thumbs/20250613_勇虎集合_003_C_1210511080962_thumbnail_IMG_2227.jpeg', LABEL_FACE_NAME=False)
