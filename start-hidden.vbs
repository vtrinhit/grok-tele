' Chay GrokTele an (khong cua so). Dung bang stop.bat. Log: data\groktele.log
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run "cmd /c """"" & dir & "\start.bat"" hidden""", 0, False
