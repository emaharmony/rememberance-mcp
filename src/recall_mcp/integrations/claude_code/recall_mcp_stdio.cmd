@echo off
setlocal
if not defined RECALL_GATE_BACKENDS if not defined REMEMBRANCE_GATE_BACKENDS set "RECALL_GATE_BACKENDS=heuristic"
rem This file now lives 4 directories below the repo root
rem (src\recall_mcp\integrations\claude_code\), not 2 as before its move
rem into the package -- keep this path in sync if it moves again.
pushd "%~dp0..\..\..\.."
".venv\Scripts\python.exe" -m recall_mcp
set "RECALL_EXIT=%ERRORLEVEL%"
popd
exit /b %RECALL_EXIT%