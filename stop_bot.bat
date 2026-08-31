@echo off
chcp 65001 > nul
echo Остановка фонового процесса бота...
wmic process where "commandline like '%%bot.py%%'" call terminate > nul 2>&1
echo Бот успешно остановлен.
pause
