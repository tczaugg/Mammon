@echo off
setlocal
title Mammon Setup

rem Everything real happens in install.py, run by the Python bundled beside this
rem file. This wrapper only catches the failures Python cannot report, because
rem the bundled Python is exactly what is missing:
rem   1. the SOURCE archive was downloaded instead of the Setup ZIP (build.py is
rem      beside us: it is the script that BUILDS the payload, and it never ships
rem      inside one). Telling that user to "Extract All" loops them forever, so
rem      it gets its own message;
rem   2. setup.bat was run from inside the ZIP viewer, which extracts that one
rem      file and nothing else.

set "HERE=%~dp0"
if exist "%HERE%python\python.exe" goto :run
if exist "%HERE%build.py" goto :source_archive
goto :not_extracted

:run
"%HERE%python\python.exe" -B "%HERE%install.py" %*
exit /b %ERRORLEVEL%

:source_archive
echo.
echo This is the Mammon SOURCE CODE, not the installer.
echo.
echo It looks like you used the green "Code" button on GitHub and chose
echo "Download ZIP". That gives you the source (a folder named Mammon-main),
echo which does not contain Python or the program itself.
echo.
echo The installer is a separate download, about 170 MB, on the Releases page:
echo.
echo     https://github.com/tczaugg/Mammon/releases/latest
echo.
echo Download the Mammon-...-Setup.zip file listed under Assets, right-click
echo it and choose Extract All, then run setup.bat from inside the
echo Mammon_Setup folder it creates.
echo.
pause
exit /b 1

:not_extracted
echo.
echo Mammon Setup cannot find the Python it brings with it.
echo.
echo Extract the WHOLE ZIP to a folder first - right-click the ZIP and choose
echo Extract All - then run setup.bat from the extracted folder.
echo.
pause
exit /b 1
