@echo off
rem ダブルクリックで KaigoShiftVoice を開く（Windows）。止める時はこの窓を閉じるか Ctrl+C。
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>&1
if not errorlevel 1 goto run

echo 動かすのに uv（Python の部品をそろえるプログラム）が要ります。
echo 公式の方法（https://docs.astral.sh/uv/）で、このユーザーの中に入れます。
set "ans="
set /p "ans=入れますか [Y/n] "
if "%ans%"=="" goto install
if /i "%ans%"=="y" goto install
if /i "%ans%"=="yes" goto install
if "%ans%"=="はい" goto install
echo やめました。README の「動かし方」を見てください
pause
exit /b 0

:install
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
  echo 入れられませんでした。README の「動かし方」を見てください
  pause
  exit /b 1
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

:run
echo はじめての時は、部品をそろえるのに1〜2分かかります。
uv run shiftvoice demo %*
echo 終わりました。この窓は閉じてかまいません。
pause
