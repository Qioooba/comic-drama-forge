@echo off
chcp 65001 >nul
echo ========================================
echo   漫剧工坊 - 依赖环境安装（一次性）
echo ========================================
echo.
cd /d "%~dp0"

REM 需要本机装有 Python 3.11+（py 启动器）
set "PYHOME="
py -3 --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python 3，请先安装 Python 3.11+（安装时勾选 Add to PATH）
    pause
    exit /b 1
)

echo [1/2] 创建便携 venv（环境\venv，约 150-300MB，含依赖下载）...
py -3 -m venv "环境\venv"
if errorlevel 1 (
    echo [错误] venv 创建失败
    pause
    exit /b 1
)

echo [2/2] 安装后端依赖（app\requirements.txt）...
"环境\venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"环境\venv\Scripts\python.exe" -m pip install -r app\requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重跑本脚本
    pause
    exit /b 1
)

echo.
echo ========================================
echo   安装完成！现在双击「启动.bat」即可运行
echo ========================================
echo.
pause
