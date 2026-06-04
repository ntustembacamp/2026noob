import os
import pandas as pd

ppl_list=pd.read_csv('/root/noob/database/114_ppl_list.csv',encoding='utf-8-sig')

folder_name='photo_1'
folder_path = f'/root/noob/database/{folder_name}'
file_list = os.listdir(folder_path)
for file_name in file_list:
    tmp = file_name.split('-')
    if tmp.__len__() == 3:
        tmp_ext = file_name.split('.')[1]
        ppl_index = ppl_list[ppl_list['name'] == tmp[1]].index[0]
        file_name_origin = f'{folder_path}/{ppl_list.loc[ppl_index, folder_name]}'
        file_name_new = f'{folder_path}/{ppl_list.loc[ppl_index, 'photo_name']}.{tmp_ext}'
        os.rename(file_name_origin,file_name_new)


folder_name='photo_2'
folder_path = f'/root/noob/database/{folder_name}'
file_list = os.listdir(folder_path)
for file_name in file_list:
    tmp = file_name.split('-')
    if tmp.__len__() == 3:
        tmp_ext = file_name.split('.')[1]
        ppl_index = ppl_list[ppl_list['name'] == tmp[1]].index[0]
        file_name_origin = f'{folder_path}/{ppl_list.loc[ppl_index, folder_name]}'
        file_name_new = f'{folder_path}/{ppl_list.loc[ppl_index, 'photo_name']}.{tmp_ext}'
        os.rename(file_name_origin,file_name_new)