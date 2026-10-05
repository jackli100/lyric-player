@echo off
rem Double-click: open player.html (drag audio + .lrc/.srt into the page)
rem Drop an audio file or folder onto this file: play it with its subtitles
cd /d "%~dp0"
if "%~1"=="" (
  start "" "%~dp0player.html"
) else (
  python "%~dp0stt.py" play "%~1"
  if errorlevel 1 pause
)
