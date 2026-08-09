@echo off
cd /d "%~dp0"
echo Building KaliClaude...
pyinstaller --onefile --windowed --paths gui --hidden-import desktop_backend --icon=app.ico --add-data "app.ico;." --collect-all cryptography --name KaliClaude gui\app.py
echo.
echo Done: dist\KaliClaude.exe
pause
