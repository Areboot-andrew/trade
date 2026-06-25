@echo off
echo === Installing dependencies one by one ===

C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe -m pip install fastapi
C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe -m pip install "uvicorn[standard]"
C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe -m pip install websockets
C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe -m pip install pandas
C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe -m pip install python-binance
C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe -m pip install pydantic

echo.
echo === Attempting pandas-ta install (may fail on Python 3.10) ===
C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe -m pip install pandas-ta==0.3.14b0 || echo [WARN] pandas-ta failed - will use fallback

echo.
echo ==========================================
echo Starting Trading Bot Server on port 8000
echo ==========================================
C:\Users\user\AppData\Local\Programs\Python\Python310\python.exe server.py

pause
