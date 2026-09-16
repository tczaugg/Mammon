@echo off
setlocal
title Mammon Setup

rem Everything real happens in install.py, run by the Python bundled beside this
rem file. This wrapper only catches the one failure Python cannot report: being
rem run from inside the ZIP viewer, which extracts setup.bat and nothing else.

set "HERE=%~dp0"
if not exist "%HERE%python\python.exe" goto :not_extracted

"%HERE%python\python.exe" -B "%HERE%install.py" %*
exit /b %ERRORLEVEL%

:not_extracted
echo.
echo Mammon Setup cannot find the Python it brings with it.
echo.
echo Extract the WHOLE ZIP to a folder first - right-click the ZIP and choose
echo Extract All - then run setup.bat from the extracted folder.
echo.
pause
exit /b 1
