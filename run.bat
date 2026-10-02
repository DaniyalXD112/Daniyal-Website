@echo off
set "RAWG_API_KEY=4b4c93a0cb2e4cd7ab734df6825760b8"
set "PLAYSCAPE_DB=C:\Users\DK\Documents\Codex\2026-09-28\slack-plugin-slack-openai-curated-remote-3\work\playscape.sqlite3"
set "GAMEVAULT_DB=C:\Users\DK\Documents\Codex\2026-09-28\slack-plugin-slack-openai-curated-remote-3\work\playscape.sqlite3"
set "GAMEVAULT_HOST=0.0.0.0"
set "GAMEVAULT_PORT=4174"
cd /d "C:\Users\DK\Documents\Codex\2026-09-28\slack-plugin-slack-openai-curated-remote-3\outputs\game-discovery"
"C:\Users\DK\AppData\Local\Python\bin\python.exe" server.py > server-preview.log 2> server-preview-error.log
