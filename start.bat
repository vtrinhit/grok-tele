@echo off
rem Chay GrokTele (API + worker). Tu khoi dong lai neu bi tat. Dung bang stop.bat
cd /d "%~dp0"
chcp 65001 >nul
title GrokTele
if not exist ".venv\Scripts\python.exe" (
  echo Chua cai dat. Chay setup.bat truoc.
  pause
  exit /b 1
)
if not exist "data" mkdir data
if exist "data\stop.flag" del "data\stop.flag"
:loop
".venv\Scripts\python.exe" -m groktele serve
if exist "data\stop.flag" (
  del "data\stop.flag"
  echo GrokTele da dung.
  exit /b 0
)
echo GrokTele dung lai, khoi dong lai sau 5 giay... (chay stop.bat de dung han)
"%SystemRoot%\System32\timeout.exe" /t 5 /nobreak >nul
goto loop
