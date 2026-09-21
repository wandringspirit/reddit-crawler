@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :error
)
set PYTHONUTF8=1
echo Starting Reddit Crawler panel (close this window to stop)...
".venv\Scripts\python.exe" -m reddit_crawler %*
goto :eof
:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed and on PATH (python --version).
pause
