@echo off
cd /d "%~dp0"
rem ★ R176b：优先项目 .venv（uv 隔离环境）
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"
echo ====== DeskBits 自动测试（全量约 40 秒）======
"%PY%" 自动测试.py
if errorlevel 1 (echo [FAIL] 有测试未过，见上与报告文件) else (echo [PASS] 全部通过)
pause
