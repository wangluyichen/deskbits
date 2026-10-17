@echo off
setlocal
rem R210(R11): 薄壳转发——双入口单实现，杜绝 60 行复制体漂移
call "%~dp0backup.bat" %*
