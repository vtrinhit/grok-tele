@echo off
rem Cai dat lan dau: tao .venv, cai thu vien, tao .env tu mau
cd /d "%~dp0"
chcp 65001 >nul
where python >nul 2>nul || (echo Can cai Python 3.11+ truoc: https://www.python.org/downloads/ & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" python -m venv .venv || (pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e ".[dev]" || (pause & exit /b 1)
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Da tao .env - dien TG_API_ID va TG_API_HASH ^(lay tai https://my.telegram.org^)
  notepad ".env"
)
echo.
echo Cai dat xong. Tiep theo: login.bat de them tai khoan Telegram, roi start.bat.
pause
