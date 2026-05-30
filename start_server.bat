@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "run_server.py" > "flask-out.log" 2> "flask-err.log"
