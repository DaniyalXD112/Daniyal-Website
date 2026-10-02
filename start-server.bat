@echo off
title GameVault Server - Port 4174
if "%RAWG_API_KEY%"=="" (
  echo [Notice] RAWG_API_KEY is not set in environment. Set it with: set "RAWG_API_KEY=your_key"
)
set "GAMEVAULT_HOST=0.0.0.0"
set "GAMEVAULT_PORT=4174"
cd /d "%~dp0"
echo ====================================================
echo  GameVault Server running on http://127.0.0.1:4174
echo ====================================================
echo.
python server.py
pause
