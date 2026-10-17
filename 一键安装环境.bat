@echo off
cd /d "%~dp0"
echo ============================================
echo   桌面超强管理助手 - 一键安装环境
echo ============================================
echo.

REM P1-4 同款前置检查（参考 打包_目录.bat）
where python >nul 2>nul || (echo [错误] 未找到 python，请先安装 Python 3.8+ 并勾选 Add to PATH & pause & exit /b 1)
python -c "import sys; assert sys.version_info >= (3,8)" || (echo [错误] Python 版本过低，需要 3.8+ & pause & exit /b 1)

if not exist requirements.txt (
    echo [错误] 未找到 requirements.txt，请在本程序目录下运行本脚本
    pause
    exit /b 1
)

echo [1/2] 正在安装依赖（pywin32 / Pillow / psutil / pynput）...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败，请检查网络后重试，
    echo        或手动执行: python -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo.
echo [2/2] 依赖安装完成！正在验证...
python -c "import win32gui, win32api, win32con, PIL, pynput" || (echo [错误] 依赖验证未通过，请把上方报错发给作者 & pause & exit /b 1)

echo.
echo ============================================
echo   环境就绪！双击「快速启动.bat」即可运行
echo ============================================
echo.
pause
exit /b 0
