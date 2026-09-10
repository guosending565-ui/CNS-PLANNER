@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CNS 地图工作台
python map_app.py %*
if errorlevel 1 (
  echo.
  echo 启动未完成。错误日志位于 outputs\map-server.log。
  pause
)
