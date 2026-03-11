import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Server ──────────────────────────────────────────────────────────────────
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ── Camera ───────────────────────────────────────────────────────────────────
# Native capture resolution — use the sensor's full/preferred resolution for
# best image quality.  Frames are resized to OUTPUT_WIDTH×OUTPUT_HEIGHT before
# being stored as JPEG (used for MJPEG stream, snapshots, and inference).
CAMERA_FRAMERATE = int(os.getenv("CAMERA_FRAMERATE", "30"))
MJPEG_FPS = int(os.getenv("MJPEG_FPS", "30"))

# Output (resized) resolution delivered to the stream and snapshot buffer
OUTPUT_WIDTH  = int(os.getenv("OUTPUT_WIDTH",  "640"))
OUTPUT_HEIGHT = int(os.getenv("OUTPUT_HEIGHT", "480"))

# ── Snapshot worker ───────────────────────────────────────────────────────────
SNAPSHOT_INTERVAL_SEC = float(os.getenv("SNAPSHOT_INTERVAL_SEC", "1.0"))
SNAPSHOT_BUFFER_SIZE = int(os.getenv("SNAPSHOT_BUFFER_SIZE", "10"))

# ── Inference scheduler ───────────────────────────────────────────────────────
# Base URL for an OpenAI-compatible server, e.g. http://192.168.1.100:8080
# The scheduler will call  {INFERENCE_API_URL}/v1/chat/completions
INFERENCE_API_URL = os.getenv("INFERENCE_API_URL", "http://192.168.1.100:8080")
INFERENCE_API_KEY = os.getenv("INFERENCE_API_KEY", "none")
INFERENCE_MODEL = os.getenv("INFERENCE_MODEL", "llava")
INFERENCE_PROMPT = os.getenv(
    "INFERENCE_PROMPT",
    "You are a robot vision system. Describe what you observe in the image(s) concisely.",
).encode().decode("unicode_escape")
INFERENCE_INTERVAL_SEC = int(os.getenv("INFERENCE_INTERVAL_SEC", "3"))
INFERENCE_FRAMES = int(os.getenv("INFERENCE_FRAMES", "2"))   # frames per request
INFERENCE_TIMEOUT_SEC = int(os.getenv("INFERENCE_TIMEOUT_SEC", "30"))

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
