@echo off
rem PIOS launcher: venv + deps + server, then browser once the server is up.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    python -m venv .venv
    del .venv\.deps_ok 2>nul
)

rem Install deps only when requirements.txt changed (skips slow pip on every launch)
if not exist .venv\.deps_ok goto :install
for /f %%i in ('powershell -NoProfile -Command "(Get-Item requirements.txt).LastWriteTime -gt (Get-Item .venv\.deps_ok).LastWriteTime"') do if %%i==True goto :install
goto :run
:install
.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
type nul > .venv\.deps_ok
:run

echo Starting PIOS on http://127.0.0.1:8321 ...
start "PIOS server" /min .venv\Scripts\python.exe -m pios.main

rem Open the browser only after the server answers (fixes the "0 events" race)
powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8321/api/status -TimeoutSec 1|Out-Null;break}catch{Start-Sleep -m 500}}"
start "" http://127.0.0.1:8321
