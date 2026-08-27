@echo off
setlocal
py -3.12 -m pip install -r requirements.txt
py -3.12 -m PyInstaller --noconfirm --clean --onefile --windowed --name WeChatGrabber app.py
echo.
echo Build complete: dist\WeChatGrabber.exe
endlocal
