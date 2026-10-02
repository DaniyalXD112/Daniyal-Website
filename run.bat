@echo off
set "GAMEVAULT_HOST=0.0.0.0"
set "GAMEVAULT_PORT=4174"
cd /d "%~dp0"
python server.py > server-preview.log 2> server-preview-error.log
