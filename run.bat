@echo off
REM 一键启动股票策略回测平台
set PY="C:\Users\lancerchen\.workbuddy\binaries\python\versions\3.14.3\python.exe"
cd /d %~dp0
echo 启动中... 浏览器请打开 http://127.0.0.1:5000
%PY% app.py
pause
