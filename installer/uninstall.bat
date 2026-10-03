@echo off
setlocal EnableExtensions

rem Removes what setup put in place and NOTHING else: the program folders it
rem copied, the Start Menu shortcut, and the Settings > Apps entry. It never
rem deletes a folder recursively unless setup created that folder, and it never
rem touches the data folder (Documents\Mammon), where ledgers and backups live.
rem
rem   uninstall.bat           asks first
rem   uninstall.bat /quiet    does not ask and does not wait (Settings' quiet uninstall)
rem
rem MAMMON_START_MENU and MAMMON_SKIP_REGISTRY redirect it for the build's own
rem verification run, which must not touch the real Start Menu or registry.

rem cmd reads a batch file from disk as it runs, so this file cannot delete the
rem folder it lives in. Re-launch from a copy in %TEMP% and work from there.
if /i "%~1"=="--from-temp" goto :work
copy /y "%~f0" "%TEMP%\mammon-uninstall.bat" >nul
if not errorlevel 1 "%TEMP%\mammon-uninstall.bat" --from-temp "%~dp0." %1
echo Could not start the uninstaller from %TEMP%.
pause
exit /b 1

:work
set "INSTALL_DIR=%~f2"
set "QUIET="
if /i "%~3"=="/quiet" set "QUIET=1"

rem (No echo of a path inside a parenthesised block anywhere below: a path
rem containing ")" would end the block early.)
if exist "%INSTALL_DIR%\mammon-install.json" goto :installed
echo %INSTALL_DIR% is not a Mammon installation. Nothing was removed.
goto :finish

:installed

set "START_MENU=%MAMMON_START_MENU%"
if not defined START_MENU set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

rem Ask the installed package where the data lives (never re-derive the rule
rem here). Through a temp file, not for /f: the parentheses in the Python code
rem break for /f's parsing of a quoted command.
set "DATA_DIR="
"%INSTALL_DIR%\python\python.exe" -B -c "from mammon import paths; print(paths.data_dir())" > "%TEMP%\mammon-data-dir.txt" 2>nul
if exist "%TEMP%\mammon-data-dir.txt" set /p DATA_DIR=<"%TEMP%\mammon-data-dir.txt"
del /f /q "%TEMP%\mammon-data-dir.txt" >nul 2>&1
if not defined DATA_DIR set "DATA_DIR=your Documents\Mammon folder"

if defined QUIET goto :check_running
echo.
echo This removes Mammon's program files from
echo   %INSTALL_DIR%
echo and its Start Menu and Settings entries.
echo.
echo Your ledgers and backups in
echo   %DATA_DIR%
echo are NOT removed.
echo.
set "ANSWER="
set /p "ANSWER=Uninstall Mammon? [y/N] "
if /i "%ANSWER%"=="y" goto :check_running
if /i "%ANSWER%"=="yes" goto :check_running
echo Nothing was removed.
goto :finish

:check_running
rem A folder cannot be renamed while a running program holds a file inside it.
ren "%INSTALL_DIR%\python" python.inuse-probe >nul 2>&1
if not errorlevel 1 goto :not_running
echo.
echo Mammon is running. Close it, and any MCP client using mammon-mcp.bat,
echo then uninstall again. Nothing was removed.
goto :finish

:not_running
ren "%INSTALL_DIR%\python.inuse-probe" python

del /f /q "%START_MENU%\Mammon.lnk" >nul 2>&1
if not defined MAMMON_SKIP_REGISTRY reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Mammon" /f >nul 2>&1

rem The marker goes last: while it exists, any copy of the package left behind
rem still keeps its data outside this folder.
for %%P in (mammon site-packages python) do if exist "%INSTALL_DIR%\%%P" rmdir /s /q "%INSTALL_DIR%\%%P"
for %%F in (mammon-mcp.bat uninstall.bat mammon-install.json) do if exist "%INSTALL_DIR%\%%F" del /f /q "%INSTALL_DIR%\%%F"
rmdir "%INSTALL_DIR%" >nul 2>&1

echo.
echo Mammon was uninstalled.
if not exist "%INSTALL_DIR%" goto :data_note
echo %INSTALL_DIR% still holds files setup did not put there, so it was left in place.
:data_note
echo Your ledgers and backups are still in %DATA_DIR%

:finish
if not defined QUIET pause
rem Delete this temporary copy of the uninstaller as the last thing cmd does.
(goto) 2>nul & del "%~f0"
