@echo off
rem Talk server (voice line to your agents). Run at logon by a scheduled task, hidden.
rem
rem OPTIONAL: if you installed the Python packages from inside the Claude desktop app, remove "rem " from the next line
rem and replace <PACKAGE_FOLDER> with the folder name under %LOCALAPPDATA%\Packages that starts with "Claude_".
rem Claude desktop's MSIX package installs "pip --user" packages into a virtualized AppData copy that Task Scheduler
rem cannot see, so a scheduled job needs this path. Adjust Python314 to your Python version. Not needed for a normal
rem Python install.
rem set PYTHONPATH=%LOCALAPPDATA%\Packages\<PACKAGE_FOLDER>\LocalCache\Roaming\Python\Python314\site-packages
py -3 "%~dp0talk_server.py" >> "%~dp0talk_server.log" 2>&1
