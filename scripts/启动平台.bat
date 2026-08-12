@echo off
chcp 65001 >nul
title AI Media Platform 启动
echo ============================================
echo   AI Media Platform · 启动中...
echo ============================================
echo.

REM 双击调用 PowerShell 执行 start.ps1（绕过编辑器关联）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"