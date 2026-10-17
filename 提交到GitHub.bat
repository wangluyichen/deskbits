@echo off
rem ============================================================
rem  提交到 GitHub 一键脚本 v1（外壳）
rem  分工：本脚本只负责找到 Python 并调 sync_github.py 引擎；
rem  所有安全门（剥离 代码存档/测试_ui、过滤校验、隐私检查、
rem  推送后回读核对）都在引擎里。中文路径过滤不放在 bat 里做
rem  （cmd 中文参数是 R65 踩过的雷），所以用这个双层结构。
rem  日常用法：先跑 提交到Gitee.bat 提交，再跑本脚本同步镜像。
rem ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul

rem ---- 记录原代码页，脚本结束恢复 ----
set "_OLDCP="
for /f "tokens=2 delims=:" %%x in ('chcp') do set "_OLDCP=%%x"
set "_OLDCP=%_OLDCP: =%"

rem ---- 定位 python（两层回退：python 命令、绝对路径）----
set "PY="
python --version >nul 2>nul
if not errorlevel 1 goto have_py
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
goto py_check
:have_py
set "PY=python"
:py_check
if not defined PY goto no_py

rem ---- 引擎在位检查 ----
if not exist "sync_github.py" goto no_engine

"%PY%" sync_github.py %*
goto end

:no_py
echo [错误] 找不到 python（PATH 里没有，C:\Python314\python.exe 也不在）。
goto end
:no_engine
echo [错误] 缺少 sync_github.py（安全引擎），两者要放在一起。
goto end

:end
if defined _OLDCP chcp %_OLDCP% >nul
endlocal
pause
