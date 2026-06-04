import os
import requests
import datetime
import time

def activity_photo_reco(activity_full_path:str, label_face_name:str):
    url = "http://localhost:8000/async-recognize/"
    params = {
        "file_path": activity_full_path,
        "label_face_name": label_face_name
    }

    headers = {
        "accept": "application/json"
    }
    try:
        response = requests.post(url, params=params, headers=headers)
        response.raise_for_status()  # 檢查請求是否成功
        print(f"{datetime.datetime.now()} 狀態碼: {response.status_code}, 回應內容: {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"{datetime.datetime.now()} 請求錯誤: {e}")
    except ValueError:
        print("{datetime.datetime.now()} 回應內容解析錯誤")


folder_path = '/root/noob/database/photo_2'
file_list = os.listdir(folder_path)

for i in range(0,len(file_list)):
# for i in range(0,10):
    if file_list[i].__len__()>2:
        file_name=file_list[i]
        activity_full_path=f'{folder_path}/{file_name}'
        activity_photo_reco(activity_full_path, label_face_name=False)
        time.sleep(0.5)
