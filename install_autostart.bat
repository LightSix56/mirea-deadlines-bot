@echo off
chcp 65001 > nul
set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_PATH=D:\mirea_deadlines_bot\start_hidden.vbs"

echo Добавление бота в автозагрузку Windows...
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%STARTUP_FOLDER%\MireaDeadlinesBot.lnk'); $s.TargetPath = '%VBS_PATH%'; $s.WorkingDirectory = 'D:\mirea_deadlines_bot'; $s.Save()"

echo [OK] Бот добавлен в автозагрузку!
echo Теперь он будет тихо запускаться в фоне при старте Windows.
pause
