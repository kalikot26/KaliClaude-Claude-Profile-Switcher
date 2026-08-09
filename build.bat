@echo off
cd /d "%~dp0"
echo Building KaliClaude...
pyinstaller --clean --noconfirm KaliClaude.spec
if errorlevel 1 exit /b %errorlevel%
echo.
echo Done: dist\KaliClaude.exe
