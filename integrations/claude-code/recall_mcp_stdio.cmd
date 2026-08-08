@echo off
echo recall_mcp_stdio.cmd now ships inside the recall_mcp package; delegating to the packaged copy. 1>&2
call "%~dp0..\..\src\recall_mcp\integrations\claude_code\recall_mcp_stdio.cmd" %*
