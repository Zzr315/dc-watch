@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Data Center Watch
echo.
echo   [1] 打开看板（用现有数据，秒开）
echo   [2] 先更新数据再打开（约 2-3 分钟）
echo.
set /p c=选择 [1/2]，直接回车=1 :
if "%c%"=="2" goto refresh

:open
if not exist "docs\index.html" (
  echo 还没有看板文件，先跑一次更新...
  goto refresh
)
start "" "docs\index.html"
exit /b

:refresh
echo.
echo 正在采集十层数据 + Opus 5 点评，请稍候...
python run_daily.py
if errorlevel 1 (
  echo.
  echo 有步骤失败了，日志在 state\run.log
  echo 页面仍会用上一次的数据打开。
  pause
)
if exist "docs\index.html" start "" "docs\index.html"
exit /b
