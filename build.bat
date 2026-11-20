@echo off
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name BakuganAP --add-data "data;data" main.py
echo Done: dist\BakuganAP.exe
pause
