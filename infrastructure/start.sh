#!/bin/sh
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

gunicorn --chdir /root/noob/service/ \
  -w 1 \
  -k uvicorn.workers.UvicornWorker \
  --timeout 600 \
  --graceful-timeout 60 \
  main:app \
  --bind 0.0.0.0:8000 \
  --log-level info \
  --access-logfile /var/log/fastapi.log
