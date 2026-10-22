@echo off
rem Xem tai khoan, han dung va thong ke job
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
  echo Chua cai dat ^(khong thay .venv^). Chay 01.setup.bat truoc.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m groktele status
pause
