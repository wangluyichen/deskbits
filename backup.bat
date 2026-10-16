@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
rem R210(R11/R2): 延迟展开——含 & <> | 的项目路径不再被 cmd 二次解析
rem R210(R11): 探活层扩 314/313/312 三候选
echo.
echo ========================================
echo   DeskBits - 一键备份
echo ========================================
echo.
rem ---- R210(R2): 项目路径含 cmd 特殊字符时直接拒绝（延迟展开也
rem      救不了 set "PY=...&..." 之外的场景，宁可直接说明）----
set "_SELF=%~dp0"
set "PYEXE="& set "PYARGS="
if not "!_SELF:&=!"=="!_SELF!" goto bad_path
if not "!_SELF:<=!"=="!_SELF!" goto bad_path
if not "!_SELF:>=!"=="!_SELF!" goto bad_path
set "PYEXE="
if not exist "%~dp0.venv\Scripts\python.exe" goto try_local
set "PYEXE=%~dp0.venv\Scripts\python.exe"
set "PYARGS="
"!PYEXE!" -c "print()" >nul 2>nul
if not errorlevel 1 goto py_ok
echo [提示] 项目自带 .venv 不可用（整目录拷贝到新电脑的典型情况），
echo        自动改用系统 Python...
set "PYEXE="
:try_local
if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python314\python.exe"
rem ★R213(NEW5-G3)：if not defined PYEXE 短路——多版本并存时 314 优先
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" if not defined PYEXE set "PYEXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" if not defined PYEXE set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PYEXE goto test_local
goto try_py
:test_local
"!PYEXE!" -c "print()" >nul 2>nul
if not errorlevel 1 goto py_ok
set "PYEXE="
goto try_py
:try_py
py -3 -c "print()" >nul 2>nul
if errorlevel 1 goto try_bare
set "PYEXE=py"
set "PYARGS=-3"
goto py_ok
:try_bare
python -c "print()" >nul 2>nul
if errorlevel 1 goto no_python
set "PYEXE=python"
set "PYARGS="
goto py_ok
:bad_path
echo [错误] 项目路径含特殊字符（^& ^< ^> ^|），cmd 无法安全处理。
echo        请把项目移到不含这些字符的目录后再备份。
echo.
pause
exit /b 1
:no_python
echo [错误] 没有找到可用的 Python，请先安装 Python 3.x。
echo.
pause
exit /b 1
:py_ok
echo 使用解释器: !PYEXE! !PYARGS!
"!PYEXE!" !PYARGS! -c "import sys;print('Python version:', sys.version.split()[0])"
echo.
"!PYEXE!" !PYARGS! simple_backup.py
if %errorlevel% equ 0 goto ok
echo.
echo ========================================
echo   [错误] 备份失败！原因见上方输出。
echo ========================================
echo.
pause
exit /b 1
:ok
echo.
echo ========================================
echo   [OK] 备份成功！
echo ========================================
echo.
pause
exit /b 0
