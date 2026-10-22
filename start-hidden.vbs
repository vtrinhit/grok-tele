' Chay GrokTele an (khong cua so). Dung bang stop.bat
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run """" & dir & "\start.bat""", 0, False
