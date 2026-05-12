' 无窗口启动 auto_login.exe，用于计划任务
' Run auto_login.exe without showing a console window
CreateObject("Wscript.Shell").Run "auto_login.exe", 0, False
