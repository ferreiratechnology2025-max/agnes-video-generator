@echo off
cd /d D:\agnes-video-generator
set AGNES_MOCK_API=0
set AGNES_VIMAX_BRAIN=1
set AGNES_VIMAX_I2V_TUNNEL=1
set AGNES_WORKER_503_SUSPEND_MIN=5
.venv\Scripts\python.exe server.py
