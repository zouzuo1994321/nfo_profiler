@echo off
REM ============================================================
REM  Stop NFO Profiler
REM  v1.1.0 起是原生桌面窗口（不占端口），因此先按进程名结束，
REM  再兜底清理仍在监听 9527 的旧版浏览器模式实例。
REM  Safe to run even when nothing is running.
REM ============================================================
set FOUND=0

echo Terminating NFO Profiler desktop window (if any) ...
taskkill /IM nfo_profiler.exe /T /F 2>nul
if %ERRORLEVEL%==0 set FOUND=1

set PORT=9527
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    echo Terminating legacy web instance on port %PORT%, PID %%a ...
    taskkill /PID %%a /T /F
    set FOUND=1
)

if %FOUND%==1 (
    echo Done.
) else (
    echo No NFO Profiler process found. It seems not running.
)
echo.
pause
