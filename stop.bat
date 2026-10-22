@echo off
rem Dung GrokTele (ca ban chay an khong cua so)
cd /d "%~dp0"
if not exist "data" mkdir data
echo stop> "data\stop.flag"
set PORT=17891
for /f "tokens=2 delims==" %%a in ('findstr /b "GROKTELE_PORT=" .env 2^>nul') do set PORT=%%a
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do taskkill /PID %%p /F >nul 2>nul
echo Da dung GrokTele (cong %PORT%).
timeout /t 2 >nul
