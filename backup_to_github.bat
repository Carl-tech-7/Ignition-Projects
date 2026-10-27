
@echo off
setlocal
title Ignition GitHub Backup

cd /d "%~dp0"

echo ========================================
echo       IGNITION GITHUB BACKUP
echo ========================================
echo.

git status --short >nul 2>&1
if errorlevel 1 goto error

echo Checking for new or changed files...
git add .
if errorlevel 1 goto error

git diff --cached --quiet
if errorlevel 1 goto changes

echo.
echo No new or changed backup files detected.
echo Your local backups are already saved.
goto upload

:changes
echo.
echo Changes detected!
echo.
set /p "project=Enter project name: "
set /p "comment=Enter comment about changes: "

if not defined project set "project=Ignition"
if not defined comment set "comment=Updated backup"

git commit -m "%project% - %comment%"
if errorlevel 1 goto error

:upload
echo.
echo Checking GitHub for updates...
git fetch origin
if errorlevel 1 goto error

echo.
echo Uploading to GitHub...
git push origin main
if errorlevel 1 goto error

echo.
echo SUCCESS: Backup sync completed!
goto finish

:error
echo.
echo ERROR: Backup process failed.
echo Please check the messages above.

:finish
echo.
pause
endlocal
