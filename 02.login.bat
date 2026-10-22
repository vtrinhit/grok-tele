@echo off
rem Dang nhap 1 tai khoan Telegram (lam 1 lan cho moi tai khoan)
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
  echo Chua cai dat ^(khong thay .venv^). Chay 01.setup.bat truoc, doi no bao "Cai dat xong", roi chay lai file nay.
  pause
  exit /b 1
)
if not exist ".env" (
  echo Chua co file .env. Chay 01.setup.bat va dien TG_API_ID, TG_API_HASH truoc.
  pause
  exit /b 1
)
set "NAME="
set "PROXY="
set /p NAME=Ten tai khoan (vd acc1, acc2):
if "%NAME%"=="" exit /b 1
set /p PROXY=Proxy (Enter neu khong dung, vd socks5://user:pass@host:port):
if "%PROXY%"=="" (
  ".venv\Scripts\python.exe" -m groktele login "%NAME%"
) else (
  ".venv\Scripts\python.exe" -m groktele login "%NAME%" --proxy "%PROXY%"
)
if errorlevel 1 (
  echo.
  echo Dang nhap KHONG thanh cong, tai khoan chua duoc luu. Xem loi o tren.
) else (
  echo.
  echo Neu server dang chay: chay 06.stop.bat roi 03.start.bat de nap tai khoan moi.
)
pause
