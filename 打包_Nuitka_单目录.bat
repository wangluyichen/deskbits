@echo off
cd /d "%~dp0"
rem ★ R176：Nuitka 默认打包路线（--zig C 编译，防反编译）
rem 产物：dist_待上传\deskbits_beta_<版本>\*.zip + 校验码（不进仓库）
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================
echo   DeskBits Nuitka 打包 - 单目录
echo ============================================
echo.
echo %PY%| findstr /i "\\" >nul || goto py_ok
if not exist "%PY%" (echo [错误] 未找到 python & pause & exit /b 1)
:py_ok
"%PY%" 打包_Nuitka.py standalone
if errorlevel 1 (echo [FAIL] 打包失败，见上方输出 & pause & exit /b 1)
echo.
echo [OK] 打包完成
pause
