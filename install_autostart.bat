@echo off
cd /d "%~dp0"
set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%STARTUP_FOLDER%\MireaDeadlinesBot.lnk'); $s.TargetPath = '%~dp0start_hidden.vbs'; $s.WorkingDirectory = '%~dp0'; $s.Save()"
echo [OK] Added to Startup!
pause
