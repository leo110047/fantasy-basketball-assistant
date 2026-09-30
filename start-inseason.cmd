@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "FBA_UV=uv"
where uv >nul 2>&1
if errorlevel 1 (
    set "FBA_UV=%USERPROFILE%\.local\bin\uv.exe"
    if not exist "%USERPROFILE%\.local\bin\uv.exe" goto missing_uv
)
set "PYTHONUTF8=1"
"%FBA_UV%" run --locked --no-dev fba-inseason %*
set "FBA_EXIT=%ERRORLEVEL%"
if not "%FBA_EXIT%"=="0" if not defined CI pause
popd
exit /b %FBA_EXIT%

:missing_uv
echo Install uv first, then reopen this command file:
echo https://docs.astral.sh/uv/getting-started/installation/
if not defined CI pause
popd
exit /b 127
