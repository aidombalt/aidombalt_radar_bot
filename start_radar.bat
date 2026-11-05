@echo off
rem Запуск Радара на Windows. Первый запуск сам создаст окружение. Перезапускается при сбое.
rem Коды выхода Радара: 0 - выключен (кнопка в веб-интерфейсе или Ctrl+C), 75 - перезапуск из веб-интерфейса, иначе - сбой.
chcp 65001 >nul
set PYTHONUTF8=1
set RADAR_LAUNCHER=bat
cd /d %~dp0
if not exist .venv (
  py -3.12 -m venv .venv || py -3 -m venv .venv || python -m venv .venv || (echo Установите Python 3.12 с python.org, галочка Add to PATH & pause & exit /b 1)
  .venv\Scripts\python -m pip install -r requirements.txt
)
rem С параметрами (--once hot, --brain-test, --report 24) - один прогон, без перезапуска
if not "%~1"=="" (
  .venv\Scripts\python run.py %*
  pause
  exit /b
)
:loop
.venv\Scripts\python run.py
set RC=%ERRORLEVEL%
if "%RC%"=="0" goto stopped
if "%RC%"=="75" goto restart
echo Радар остановился из-за сбоя, код %RC%. Перезапуск через 5 секунд... Закройте окно, чтобы выйти.
timeout /t 5 >nul
goto loop
:restart
echo Перезапуск по команде из веб-интерфейса...
goto loop
:stopped
echo Радар выключен. Окно закроется через 5 секунд.
timeout /t 5 >nul
exit /b 0
