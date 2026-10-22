@echo off
cd /d "%~dp0"
title GrokTele
:loop
".venv\Scripts\python.exe" -m groktele serve
echo GrokTele dung lai, khoi dong lai sau 5 giay... (Ctrl+C de thoat)
timeout /t 5 /nobreak >nul
goto loop
