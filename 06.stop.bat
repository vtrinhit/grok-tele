@echo off
rem Dung GrokTele (ca ban chay an khong cua so)
setlocal EnableDelayedExpansion
cd /d "%~dp0"
if not exist "data" mkdir data
echo stop> "data\stop.flag"
set "STOPPED="

rem 1) Theo file PID, chi kill neu dung la python.exe / pythonw.exe
if exist "data\groktele.pid" (
  set /p PID=<"data\groktele.pid"
  for %%n in (python.exe pythonw.exe) do (
    tasklist /FI "PID eq !PID!" /FI "IMAGENAME eq %%n" 2>nul | find /i "%%n" >nul && (
      taskkill /PID !PID! /F >nul 2>nul && set "STOPPED=1"
    )
  )
  del "data\groktele.pid" >nul 2>nul
)

rem 2) Du phong: process dang nghe dung cong cua GrokTele (so sanh dia chi local, khong phu thuoc ngon ngu Windows)
if not defined STOPPED (
  set "PORT=8888"
  for /f "tokens=2 delims==" %%a in ('findstr /b /c:"GROKTELE_PORT=" .env 2^>nul') do set "PORT=%%a"
  for /f "tokens=2,5" %%a in ('netstat -ano -p tcp') do (
    if "%%a"=="127.0.0.1:!PORT!" taskkill /PID %%b /F >nul 2>nul && set "STOPPED=1"
    if "%%a"=="0.0.0.0:!PORT!" taskkill /PID %%b /F >nul 2>nul && set "STOPPED=1"
  )
)

if defined STOPPED (echo Da dung GrokTele.) else (echo GrokTele khong chay.)
"%SystemRoot%\System32\timeout.exe" /t 2 >nul 2>nul
