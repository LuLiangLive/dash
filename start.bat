@echo off
chcp 65001 >nul
echo ========================================
echo   投研看板 - 启动脚本
echo ========================================
echo.

cd /d "%~dp0"

set PORT=8000
echo [1/3] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

echo [2/3] 检查依赖...
python -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo 安装依赖中...
    pip install -r requirements.txt
)

echo [3/3] 启动服务 (端口 %PORT%)...
echo.
echo 服务启动后访问: http://127.0.0.1:%PORT%/
echo 按 Ctrl+C 停止服务
echo.

python -m uvicorn main:app --host 127.0.0.1 --port %PORT%

pause
