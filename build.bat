@echo off
rem Builds client.exe from client.cs and client_gui.cs with the C# compiler that
rem comes with Windows (.NET Framework 4). Nothing needs installing.
rem Double-click it, or run "build" in this folder. Add /nopause to skip the pause.
setlocal
cd /d "%~dp0"
set FAILED=0

set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo Could not find the C# compiler that comes with the .NET Framework 4.
    echo Turn on ".NET Framework 4.8 Advanced Services" in "Turn Windows features on or off".
    set FAILED=1
    goto done
)

"%CSC%" /nologo /r:System.Web.Extensions.dll /out:client.exe client.cs client_gui.cs
if errorlevel 1 (
    echo.
    echo The build failed; see the errors above.
    set FAILED=1
) else (
    echo Built client.exe. Double-click it to open the window, or run "client.exe" with a command.
)

:done
if /i not "%~1"=="/nopause" pause
exit /b %FAILED%
