import torch
print(torch.__version__)
# 2.6.0+cu124
print(torch.version.cuda)
# 12.4
print(torch.backends.cudnn.version())
# 90100

# pip install onnxruntime-gpu==1.22.0
import onnxruntime
print('應包含CUDAExecutionProvider: ', onnxruntime.get_available_providers())  # 應包含 'CUDAExecutionProvider'
print('應顯示GPU: ', onnxruntime.get_device())               # 應顯示 'GPU'