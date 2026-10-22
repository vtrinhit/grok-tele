@echo off
rem Chay GrokTele (API + worker). Tu khoi dong lai neu bi tat. Dung bang 06.stop.bat
rem 03.start.bat hidden = che do chay an (khong pause khi loi)
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title GrokTele
if not exist ".venv\Scripts\python.exe" (
  echo Chua cai dat. Chay 01.setup.bat truoc.
  if /i not "%~1"=="hidden" pause
  exit /b 1
)
if not exist "data" mkdir data
if exist "data\stop.flag" del "data\stop.flag"
:loop
".venv\Scripts\python.exe" -m groktele serve
set "CODE=%errorlevel%"
if exist "data\stop.flag" (
  del "data\stop.flag"
  echo GrokTele da dung.
  exit /b 0
)
if %CODE% GEQ 2 (
  echo.
  echo GrokTele khong chay duoc ^(ma loi %CODE%^): thieu cau hinh .env hoac cong dang ban. Xem thong bao o tren.
  if /i not "%~1"=="hidden" pause
  exit /b %CODE%
)
echo GrokTele dung lai, khoi dong lai sau 5 giay... (chay 06.stop.bat de dung han)
"%SystemRoot%\System32\timeout.exe" /t 5 /nobreak >nul
goto loop
