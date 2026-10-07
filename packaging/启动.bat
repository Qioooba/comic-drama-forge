@echo off
chcp 65001 >nul
echo ========================================
echo   漫剧工坊 - 便携版 启动器
echo ========================================
echo.
cd /d "%~dp0"

REM ---- 1/3 依赖环境 ----
set "PY=%~dp0环境\venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [首次运行] 未检测到依赖环境，请先双击「安装依赖.bat」
    pause
    exit /b 1
)

REM ---- 2/3 可选：随包自带 ffmpeg（bin\ffmpeg\ffmpeg.exe）----
if exist "%~dp0bin\ffmpeg\ffmpeg.exe" (
    set "MJSCXT_FFMPEG=%~dp0bin\ffmpeg\ffmpeg.exe"
    echo [OK] 使用随包 ffmpeg: %MJSCXT_FFMPEG%
) else (
    echo [提示] 未检测到随包 ffmpeg，将使用系统 PATH 上的 ffmpeg/ffprobe
)

REM ---- 3/3 ComfyUI 连接检查（不阻断：ComfyUI 离线时前端可展示状态，启动照旧）----
curl -s http://127.0.0.1:8188/system_stats >nul 2>&1
if errorlevel 1 (
    echo [警告] ComfyUI 未运行（默认 http://127.0.0.1:8188）
    echo        请先到 ComfyUI 目录启动它（需自备 ComfyUI + 模型权重，详见「环境准备清单.txt」）
    echo        本服务仍会启动，图像/视频生成需 ComfyUI 在线。
) else (
    echo [OK] ComfyUI 已连接
)

echo.
echo ========================================
echo   启动 Web 服务: http://127.0.0.1:45871
echo   按 Ctrl+C 停止
echo ========================================
echo.
"%PY%" app\serve.py
pause
