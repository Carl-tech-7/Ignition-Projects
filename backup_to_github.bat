
@echo off
title Ignition GitHub Backup

cd /d "C:\Users\CarlVenter\OneDrive - Concera\Desktop\Ignition-Projects"

echo Checking Git repository...
git status

if errorlevel 1 (
    echo.
    echo ERROR: Git repository not found.
    echo Check whether Git has been initialized.
    pause
    exit /b 1
)

echo.
echo Adding changed files...
git add .

echo.
echo Committing changes...
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "Updated Ignition backup"
)

echo.
echo Uploading to GitHub...
git push

echo.
echo Finished.
pause
