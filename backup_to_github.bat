
@echo off
setlocal EnableExtensions
title Ignition Project Backup Manager

cd /d "%~dp0"

echo ==========================================
echo       IGNITION PROJECT BACKUP MANAGER
echo ==========================================
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto error

echo Checking for project changes...
echo.

git add -A
if errorlevel 1 goto error

git diff --cached --quiet
if errorlevel 1 goto changes

echo No new, modified or deleted files found.
goto sync

:changes
echo ==========================================
echo          CHANGES DETECTED
echo ==========================================
echo.
echo Status codes:
echo A = NEW
echo M = MODIFIED
echo D = DELETED
echo R = RENAMED
echo.

git diff --cached --name-status
echo.
echo ==========================================

set /p "confirm=Save these changes? (Y/N): "
if /I not "%confirm%"=="Y" goto cancelled

echo.
set "project="
set "comment="
set /p "project=Project name: "
set /p "comment=Describe the changes: "

if not defined project set "project=Ignition"
if not defined comment set "comment=Project update"

git commit -m "%project% - %comment%"
if errorlevel 1 goto error

:sync
echo.
echo Checking GitHub connection...
git fetch origin
if errorlevel 1 goto error

echo.
echo Uploading to GitHub...
git push origin main
if errorlevel 1 goto error

echo.
echo ==========================================
echo          BACKUP COMPLETED
echo ==========================================
goto finish

:cancelled
echo.
echo Changes were not committed or uploaded.
goto finish

:error
echo.
echo ERROR: Backup process failed.
echo Check the messages above.

:finish
echo.
pause
endlocal
