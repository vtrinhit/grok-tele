@echo off
rem Huy moi job dang cho / dang chay (vd ReelFB da dung ma GrokTele van con job cu)
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
  echo Chua cai dat. Chay 01.setup.bat truoc.
  pause
  exit /b 1
)
set /p OK=Huy TOAN BO job dang cho va dang chay? (y/N):
if /i not "%OK%"=="y" exit /b 0
".venv\Scripts\python.exe" -m groktele cancel-all
pause
