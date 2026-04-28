@echo off
cd /d "%~dp0"

if not exist venv-win (
    echo Creating virtualenv...
    python -m venv venv-win
)

venv-win\Scripts\pip install -q -r requirements.txt

if not exist config.json (
    copy config.example.json config.json
    echo.
    echo  SETUP REQUIRED: Edit config.json and add your API tokens.
    echo.
)

if not exist cert.pem (
    echo  Generating self-signed TLS certificate for HTTPS...
    venv-win\Scripts\python generate_cert.py
)

echo.
echo  Starting Vinyl Inventory (HTTPS)...
echo  Local:  https://localhost:5001
echo  Phone:  https://YOUR-PC-IP:5001
echo.
echo  First visit: click 'Advanced' and 'Proceed' past the cert warning.
echo.

venv-win\Scripts\python app.py
