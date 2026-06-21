@echo off
cd /d "%~dp0"
echo Building KaliClaude...
pyinstaller --onefile --windowed --icon=app.ico --add-data "app.ico;." --collect-all cryptography --name KaliClaude gui\app.py
echo.
echo Done: dist\KaliClaude.exe
pause
