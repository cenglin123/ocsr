@echo off
rem OCSR Board launcher: double-click to start the read-only board.
rem Close this console window to stop the board process.
cd /d "%~dp0"
python scripts\ocsr_board.py
pause
