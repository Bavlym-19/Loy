# Guardian Server

FastAPI backend for ESP32 Guardian device.

## Deployment on Render
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn server:app --host 0.0.0.0 --port $PORT`

## Environment Variables
- `DEVICE_KEY`: Secret key shared with ESP32
- `ADMIN_TOKEN`: Admin password for dashboard & API

