@echo off
chcp 65001 >nul
setlocal

REM ═══════════════════════════════════════════════════════════
REM  DiskCleaner 打包脚本
REM  需要：Python 3.9+ 且已安装 pyinstaller（pip install pyinstaller）
REM ═══════════════════════════════════════════════════════════

set PY=%1
if "%PY%"=="" set PY=python

echo [1/4] 检查环境...
%PY% -c "import tkinter, PyInstaller" 2>nul
if errorlevel 1 (
    echo 缺少 tkinter 或 PyInstaller，请先执行: pip install pyinstaller
    pause
    exit /b 1
)

echo [2/4] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [3/4] 开始打包...
%PY% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name DiskCleaner ^
  --add-data "signatures.json;." ^
  --hidden-import scanner ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module PIL ^
  --exclude-module lxml ^
  --exclude-module openpyxl ^
  --exclude-module docx ^
  --exclude-module pptx ^
  disk_cleaner.pyw

if errorlevel 1 (
    echo 打包失败！
    pause
    exit /b 1
)

echo [4/4] 完成！
echo.
echo 产物: dist\DiskCleaner.exe
dir /b dist
echo.
pause
