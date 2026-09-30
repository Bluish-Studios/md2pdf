@echo off
rem md2pdf: Markdown to PDF. Runs md2pdf.ps1, which finds or installs Python, with the same arguments.
rem Drag Markdown files onto this file in Explorer, or run: md2pdf notes.md   (md2pdf --help for options)
setlocal
set "MD2PDF_LAUNCHER=%~nx0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0md2pdf.ps1" %*
exit /b %ERRORLEVEL%
