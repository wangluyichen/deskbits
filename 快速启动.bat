@echo off
setlocal
cd /d "%~dp0"
set "PY="
set "STARTCMD="

rem ---- 1) PATH 里的 python（Microsoft Store 占位别名会返回 9009，被此检查过滤）----
python -c "print()" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    set "STARTCMD=pythonw launcher.pyw"
)

rem ---- 2) py 启动器 ----
if not defined PY (
    py -3 -c "print()" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
        set "STARTCMD=py -3w launcher.pyw"
    )
)

rem ---- 3) 常见安装路径 ----
if not defined PY (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python314"
        "%LOCALAPPDATA%\Programs\Python\Python313"
        "%ProgramFiles%\Python314"
        "%ProgramFiles%\Python313"
    ) do if not defined PY if exist "%%~P\python.exe" (
        set "PY="%%~P\python.exe""
        set "STARTCMD="%%~P\pythonw.exe" launcher.pyw"
    )
)

if not defined PY goto no_python

rem ---- 4) 依赖完整性检查（缺 pywin32/Pillow 时给提示，避免无声崩溃）----
%PY% -c "import win32gui, win32api, win32con, PIL" >nul 2>nul
if errorlevel 1 goto no_deps

start "" %STARTCMD%
exit /b 0

:no_python
echo.
echo  [错误] 未检测到可用的 Python 环境，程序无法启动！
echo.
echo  解决办法（二选一）：
echo    1. 双击运行本目录的「一键安装环境.bat」自动安装 Python 和依赖
echo    2. 手动安装 Python 3.14+：https://www.python.org/downloads/
echo       安装时务必勾选 "Add python.exe to PATH"
echo.
pause
exit /b 1

:no_deps
echo.
echo  [错误] Python 依赖未安装完整（缺少 pywin32 或 Pillow），程序无法启动！
echo.
echo  解决办法（二选一）：
echo    1. 双击运行本目录的「一键安装环境.bat」自动安装依赖
echo    2. 手动执行: %PY% -m pip install -r requirements.txt
echo.
pause
exit /b 1
