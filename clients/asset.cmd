@echo off
rem Builds the C# client with the compiler that comes with Windows, then runs it with
rem whatever was typed after this command. Building takes about a second, so it is done
rem every time, which means the program can never be out of date.
rem   clients\asset.cmd list --user admin

set "CSC=%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%SystemRoot%\Microsoft.NET\Framework\v4.0.30319\csc.exe"

"%CSC%" /nologo /out:"%~dp0AssetClient.exe" /r:System.Web.Extensions.dll "%~dp0AssetClient.cs"
if errorlevel 1 exit /b 1

"%~dp0AssetClient.exe" %*
