@echo off
setlocal
set "OUT=%USERPROFILE%\Desktop\win11_env.txt"

echo 正在采集环境信息，请稍候...
echo ==== Windows 版本 ==== > "%OUT%"
ver >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== where python / pythonw / py ==== >> "%OUT%"
where python >> "%OUT%" 2>&1
where pythonw >> "%OUT%" 2>&1
where py >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== python --version ==== >> "%OUT%"
python --version >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== py -0p（py 启动器登记的所有 Python）==== >> "%OUT%"
py -0p >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== 常见安装目录 ==== >> "%OUT%"
dir /b "C:\Python*" >> "%OUT%" 2>&1
dir /b "%LOCALAPPDATA%\Programs\Python" >> "%OUT%" 2>&1
dir /b "%ProgramFiles%\Python*" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== PATH ==== >> "%OUT%"
echo %PATH% >> "%OUT%"

echo.
echo  已生成: %OUT%
echo  请把这个文件发给开发者（用于环境兼容适配）。
echo.
pause
