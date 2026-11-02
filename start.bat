@echo off
REM One-command start for Windows.
cd /d "%~dp0"
if not exist .venv (
  echo Setting up Agent Tillu (first run only)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q -e ".[browser]"
  .venv\Scripts\python -m playwright install chromium
)
where ollama >nul 2>nul
if %errorlevel%==0 (
  if "%TILLU_MODEL%"=="" set TILLU_MODEL=qwen3:8b
  ollama pull %TILLU_MODEL%
  ollama pull nomic-embed-text
) else (
  echo Ollama not found. Install it from https://ollama.com - starting with the 'stub' model for now.
  if "%TILLU_MODEL%"=="" set TILLU_MODEL=stub
)
.venv\Scripts\python -m tillu %*
