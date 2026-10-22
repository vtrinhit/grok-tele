@echo off
rem Dang nhap 1 tai khoan Telegram (lam 1 lan cho moi tai khoan)
cd /d "%~dp0"
chcp 65001 >nul
set NAME=
set PROXY=
set /p NAME=Ten tai khoan (vd acc1, acc2):
if "%NAME%"=="" exit /b 1
set /p PROXY=Proxy (Enter neu khong dung, vd socks5://user:pass@host:port):
if "%PROXY%"=="" (
  ".venv\Scripts\python.exe" -m groktele login %NAME%
) else (
  ".venv\Scripts\python.exe" -m groktele login %NAME% --proxy "%PROXY%"
)
echo Neu server dang chay: chay 06.stop.bat roi 03.start.bat de nap tai khoan moi.
pause
