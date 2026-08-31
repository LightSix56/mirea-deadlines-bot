Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\mirea_deadlines_bot"
WshShell.Run "python bot.py", 0, False
