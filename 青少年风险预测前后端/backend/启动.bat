@echo off
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 "') do taskkill /PID %%a /F >nul 2>&1
timeout /t 1 /nobreak >nul
set PYTHONIOENCODING=utf-8
python -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
