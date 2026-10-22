@echo off
rem Cai dat lan dau: tao .venv, cai thu vien, tao .env tu mau. Chay lai bat ky luc nao de cap nhat thu vien.
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PY="
where python >nul 2>nul && (python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python")
if not defined PY (
  where py >nul 2>nul && (py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3")
)
if not defined PY (
  echo Can Python 3.11 tro len: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH" khi cai^)
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Tao moi truong ao .venv ...
  %PY% -m venv .venv || (pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e ".[dev]" || (echo Cai thu vien that bai. & pause & exit /b 1)
rem cryptg giup tai video nhanh hon; khong co cung chay duoc
".venv\Scripts\python.exe" -m pip install cryptg >nul 2>nul || echo (Bo qua cryptg - khong anh huong chuc nang)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Da tao .env - dien TG_API_ID va TG_API_HASH ^(lay tai https://my.telegram.org^) roi luu lai.
  notepad ".env"
)
echo.
echo Cai dat xong. Tiep theo: 02.login.bat de them tai khoan Telegram, roi 03.start.bat.
pause
