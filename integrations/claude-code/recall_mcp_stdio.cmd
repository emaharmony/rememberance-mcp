@echo off
setlocal
if not defined RECALL_GATE_BACKENDS if not defined REMEMBRANCE_GATE_BACKENDS set "RECALL_GATE_BACKENDS=heuristic"
pushd "%~dp0..\.."
".venv\Scripts\python.exe" -m recall_mcp
set "RECALL_EXIT=%ERRORLEVEL%"
popd
exit /b %RECALL_EXIT%