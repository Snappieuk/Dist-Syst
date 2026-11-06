@echo off
rem Builds the desktop app into clients\AssetApp.exe using the C# compiler that comes with
rem Windows (no Visual Studio or SDK needed).
set CSC=%windir%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%windir%\Microsoft.NET\Framework\v4.0.30319\csc.exe
"%CSC%" /nologo /target:winexe /out:"%~dp0AssetApp.exe" /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll "%~dp0app\*.cs"
