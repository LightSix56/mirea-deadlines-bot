@echo off
chcp 65001 > nul
set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_PATH=D:\mirea_deadlines_bot\start_hidden.vbs"

echo Создание ярлыка в автозагрузке Windows...
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%STARTUP_FOLDER%\MireaDeadlinesBot.lnk'); $s.TargetPath = '%VBS_PATH%'; $s.WorkingDirectory = 'D:\mirea_deadlines_bot'; $s.Save()"

echo [OK] Бот добавлен в автозагрузку Windows!
echo Теперь он будет автоматически запускаться в скрытом фоне при каждом включении ПК.
pause
