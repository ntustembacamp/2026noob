# base on this docker image 
FROM python:3.12

WORKDIR /install
COPY requirements.txt .
COPY start.sh .

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8

RUN apt update -y && \
    apt install -y libgl1 && \
    apt install -y vim && \
    python -m pip install --upgrade pip && \
    pip install --no-cache-dir --retries 20 --timeout 300 -r requirements.txt && \
    pip install --no-cache-dir --retries 20 --timeout 300 openpyxl==3.1.5 python-multipart==0.0.20 && \
    apt clean && \
    rm -rf /var/lib/apt/lists/* && \
    chmod +x /install/start.sh
    
# CMD ["sh", "/install/start.sh"]
