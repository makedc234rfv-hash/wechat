@echo off
setlocal
cd /d "%~dp0"
"%~dp0app\WeChatExport\WeChatExport.exe" --self-test
set "tool_exit=%errorlevel%"
echo.
echo Exit code: %tool_exit%
pause
exit /b %tool_exit%
