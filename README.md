# Robot Camera Inference Dashboard

RPi5 robot camera system with a real-time VLM inference pipeline.

## Architecture

```
Browser ──► RPi5 Web UI (FastAPI)
               │
               ├─ CameraManager   ← picamera2, 640×480 continuous capture
               ├─ SnapshotWorker  ← 1 frame/s from latest_frame → deque
               ├─ InferenceScheduler  ← every 3s, 2 frames → POST to VLM API
               ├─ ResultManager   ← stores result, fans out via WebSocket
               └─ stream.mjpeg    ← live MJPEG from latest_frame

                      ▼
             Inference API Server
               └─ POST /infer  { "frames": [{"timestamp": …, "image": "<b64>"}] }
               └─ returns any JSON
```

## File layout

| File | Role |
|---|---|
| `config.py` | All tuneable constants / env vars |
| `camera_manager.py` | Opens picamera2, keeps `latest_jpeg` fresh in a thread |
| `snapshot_worker.py` | Grabs 1 frame/s, stores in rolling `deque` |
| `inference_scheduler.py` | Picks 2 frames every 3 s, POSTs to VLM API |
| `result_manager.py` | Thread-safe result store + asyncio WebSocket broadcast |
| `web_app.py` | FastAPI routes: `/`, `/stream.mjpeg`, `/api/status`, `/ws/results` |
| `main.py` | Wires everything, starts uvicorn |

## Setup

### 1 — System dependencies (Raspberry Pi OS Bookworm)

```bash
sudo apt update
sudo apt install python3-picamera2
```

### 2 — Python environment

```bash
cd /home/drone/drone_inference

# Create a venv that can see the system picamera2
python3 -m venv .venv --system-site-packages
source .venv/bin/activate

pip install -r requirements.txt
```

### 3 — Configure

```bash
# cp .env.example .env
nano .env          # set INFERENCE_API_URL to your VLM server
```

### 4 — Run

```bash
source .venv/bin/activate
python main.py
```

Open `http://<rpi-ip>:8000` in a browser.

## Install as systemd service

```bash
sudo cp drone-inference.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable drone-inference
sudo systemctl start drone-inference
sudo journalctl -u drone-inference -f   # follow logs
```

## Inference API contract

**Request** (POST JSON):
```json
{
  "frames": [
    { "timestamp": 1741651200.123, "image": "<base64-jpeg>" },
    { "timestamp": 1741651201.456, "image": "<base64-jpeg>" }
  ]
}
```

**Response** — any JSON object.  
Internal keys prefixed with `_` are stripped before display.  
On error, include an `"error"` key for the UI to show a warning.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `INFERENCE_API_URL` | *(required)* | Remote VLM endpoint |
| `INFERENCE_INTERVAL_SEC` | `3` | Seconds between inference calls |
| `INFERENCE_FRAMES` | `2` | Frames per inference request |
| `INFERENCE_TIMEOUT_SEC` | `30` | HTTP timeout |
| `SNAPSHOT_INTERVAL_SEC` | `1.0` | Snapshot cadence |
| `SNAPSHOT_BUFFER_SIZE` | `10` | Rolling buffer depth |
| `CAMERA_WIDTH/HEIGHT` | `640 / 480` | Capture resolution |
| `CAMERA_FRAMERATE` | `30` | Camera framerate |
| `MJPEG_FPS` | `30` | MJPEG stream rate |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Server bind |
