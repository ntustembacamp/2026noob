import os
import pandas as pd

ppl_list=pd.read_csv('/root/noob/database/114_ppl_list.csv',encoding='utf-8-sig')

folder_name='photo_1'
folder_path = f'/root/noob/database/{folder_name}'
file_list = os.listdir(folder_path)
for file_name in file_list:
    tmp = file_name.split('-')
    if tmp.__len__() == 3:
        ppl_index = ppl_list[ppl_list['name'] == tmp[1]].index[0]
        ppl_list.loc[ppl_index, folder_name] = file_name


folder_name = 'photo_2'
folder_path = f'/root/noob/database/{folder_name}'
file_list = os.listdir(folder_path)
for file_name in file_list:
    tmp = file_name.split('-')
    if tmp.__len__() == 3:
        ppl_index = ppl_list[ppl_list['name'] == tmp[1]].index[0]
        ppl_list.loc[ppl_index, folder_name] = file_name


ppl_list.to_csv('/root/noob/database/114_ppl_list.csv',encoding='utf-8-sig', index=False)
