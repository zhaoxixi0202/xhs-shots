#!/bin/bash
# 启动小红书笔记截图工具（Streamlit GUI）
cd "$(dirname "$0")"
PY="/Users/zhaoxixi/.workbuddy/binaries/python/envs/xhs/bin/python"
exec "$PY" -m streamlit run app.py --server.port 8501 --server.headless true
