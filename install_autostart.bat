@echo off
rem OPT-IN: creates a Startup shortcut so PIOS launches when you log in.
rem Run this yourself if you want always-on memory. Delete the shortcut at
rem   shell:startup  (Win+R) to undo.
set SCRIPT=%~dp0run_pios.bat
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\PIOS.lnk');" ^
  "$s.TargetPath='%SCRIPT%'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo PIOS will now start automatically at login. To undo: Win+R, shell:startup, delete PIOS.lnk
pause
