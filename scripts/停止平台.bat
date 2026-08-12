@echo off
chcp 65001 >nul
title AI Media Platform 停止
echo ============================================
echo   AI Media Platform · 停止中...
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"