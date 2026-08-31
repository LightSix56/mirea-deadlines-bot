Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\mirea_deadlines_bot"
WshShell.Run """D:\prog\python_3_10\pythonw.exe"" ""D:\mirea_deadlines_bot\bot.py""", 0, False