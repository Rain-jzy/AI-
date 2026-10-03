@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto NOPY

echo Starting AI Werewolf ...
python "werewolf_ai.py"
if errorlevel 1 (
  echo.
  echo The program exited with an error. Please screenshot the message above.
  pause
)
goto :EOF

:NOPY
echo.
echo Python was not found in PATH.
echo Please install Python 3.8+ and check "Add Python to PATH" during setup.
echo.
pause
