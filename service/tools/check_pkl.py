import pandas as pd
res = pd.read_pickle('C:\\Users\\Test\\Desktop\\faces_embedding_antelopev2.pkl')
#res = pd.read_pickle('/root/noob/service/embedding/faces_embedding_antelopev2.pkl')

for i in range(0,len(res)):
# for i in range(0,1):
    #if res[i]['user_name'] =='工管_114_1_林辛瑋':
    print(res[i]['user_name'])
