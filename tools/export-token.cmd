@echo off
REM 一键导出本机长效令牌并注入 GitHub Secret（Windows CMD 版）。
REM 解释器不写死版本号：where python 优先，退回 WorkBuddy 托管 python 目录。
setlocal
set "SELF_DIR=%~dp0"
set "PYTHON_BIN="
for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PYTHON_BIN set "PYTHON_BIN=%%i"
)
if not defined PYTHON_BIN (
  for /f "delims=" %%i in ('dir /b /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions\*\python.exe" 2^>nul') do (
    if not defined PYTHON_BIN set "PYTHON_BIN=%USERPROFILE%\.workbuddy\binaries\python\versions\%%i"
  )
)
if not defined PYTHON_BIN (
  echo [FAIL] 未找到 python 解释器 & exit /b 1
)
echo [ok] 解释器：%PYTHON_BIN%
"%PYTHON_BIN%" "%SELF_DIR%export_token.py" %*
endlocal
