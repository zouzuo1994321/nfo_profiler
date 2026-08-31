@echo off
REM ============================================================
REM  Stop NFO Profiler
REM  Find every process listening on port 9527 and kill its
REM  process tree (/T kills children, /F forces). Safe to run
REM  even when nothing is listening.
REM ============================================================
set PORT=9527
set FOUND=0

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    echo Terminating NFO Profiler on port %PORT%, PID %%a ...
    taskkill /PID %%a /T /F
    set FOUND=1
)

if %FOUND%==1 (
    echo Done.
) else (
    echo No process is listening on port %PORT%. NFO Profiler seems not running.
)
echo.
pause
