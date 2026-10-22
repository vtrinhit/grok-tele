@echo off
rem Tu chay GrokTele (an) moi khi dang nhap Windows. Go bo: 07.install-autostart.bat remove
cd /d "%~dp0"
if /i "%~1"=="remove" (
  schtasks /delete /tn "GrokTele" /f
  pause
  exit /b
)
schtasks /create /tn "GrokTele" /tr "wscript.exe \"%~dp004.start-hidden.vbs\"" /sc onlogon /rl limited /f
echo Da cai tu khoi dong. Log: %~dp0data\groktele.log
pause
