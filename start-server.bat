@echo off
title GameVault Server - Port 4174
set "RAWG_API_KEY=4b4c93a0cb2e4cd7ab734df6825760b8"
set "PLAYSCAPE_DB=%~dp0..\..\work\playscape.sqlite3"
set "GAMEVAULT_DB=%~dp0..\..\work\playscape.sqlite3"
set "GAMEVAULT_HOST=0.0.0.0"
set "GAMEVAULT_PORT=4174"
cd /d "%~dp0"
echo ====================================================
echo  GameVault Server is running on 0.0.0.0:4174
echo  PC URL:      http://127.0.0.1:4174
echo  Android URL: http://192.168.3.108:4174
echo ====================================================
echo.
python server.py
pause
