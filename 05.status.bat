@echo off
rem Xem tai khoan, han dung va thong ke job
cd /d "%~dp0"
chcp 65001 >nul
".venv\Scripts\python.exe" -m groktele status
pause
