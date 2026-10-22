@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -m venv .venv
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo 创建 Python 虚拟环境失败，请确认已安装 Python 3.10 或更高版本。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not exist "config.json" copy "config.example.json" "config.json" >nul

".venv\Scripts\python.exe" run.py probe
".venv\Scripts\python.exe" run.py login
".venv\Scripts\python.exe" run.py list
".venv\Scripts\python.exe" run.py run --activity-index 0

echo.
echo 流程结束。运行记录位于 .state\runs\
pause
