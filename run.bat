@echo off
REM ===========================================================================
REM  Supervised launcher for the Nexus stat-arb bot (Windows).
REM
REM  Relaunches the bot whenever it exits, so a crash OR the Telegram /restart
REM  command both recover WITHOUT needing a command prompt. Start the bot with
REM  this instead of `python app.py` (double-click it, or run it once and leave
REM  the window open / minimised).
REM
REM  STATARB_SUPERVISED=1 tells the app to EXIT (code 42) on /restart instead of
REM  re-exec'ing itself. Under a supervisor, self-exec would leave TWO live
REM  engines running at once (double OKX orders) — so this MUST be set whenever
REM  the bot is launched from a relaunch loop like this one.
REM
REM  NOTE: run this from your StatArb Anaconda prompt so `python` resolves to the
REM  StatArb env. If you double-click instead, replace `python` below with the
REM  full path, e.g.:
REM      C:\Users\Administrator\anaconda3\envs\StatArb\python.exe app.py
REM ===========================================================================
setlocal
set STATARB_SUPERVISED=1

:loop
echo(
echo [supervisor] starting bot at %date% %time%
python app.py
echo [supervisor] bot exited (code %errorlevel%) - relaunching in 3s. Close this window to stop.
timeout /t 3 /nobreak >nul
goto loop
