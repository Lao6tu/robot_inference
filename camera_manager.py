"""
Camera Manager
==============
Wraps picamera2 to deliver a continuously-updated JPEG frame in memory.
All other components read the latest frame via get_frame().
"""

import io
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class CameraManager:
    """Opens the Raspberry Pi camera and keeps `_latest_jpeg` up to date.

    The camera ISP is configured to downscale to (output_width, output_height)
    in hardware — no Python-side resize is needed.  This gives full 30 fps
    throughput without CPU overhead.
    """

    def __init__(
        self,
        framerate: int = 30,
        output_width: int = 640,
        output_height: int = 480,
    ) -> None:
        self.framerate = framerate
        self.output_width = output_width
        self.output_height = output_height

        self._latest_jpeg: Optional[bytes] = None
        self._lock = threading.Lock()
        self._frame_event = threading.Event()   # set whenever a new frame is ready
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cam = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the camera and the background capture thread."""
        try:
            from picamera2 import Picamera2  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "picamera2 is not installed. "
                "Run: sudo apt install python3-picamera2"
            ) from exc

        self._cam = Picamera2()
        # Specify a raw stream at the sensor's full resolution so picamera2
        # selects a sensor mode that covers the entire imaging area.  The ISP
        # then downscales that full-frame capture to (output_width, output_height)
        # for the main stream — giving a wide, uncropped field-of-view at any
        # output resolution.
        sensor_res = self._cam.sensor_resolution   # e.g. (4656, 3496) for IMX708
        cfg = self._cam.create_video_configuration(
            main={"size": (self.output_width, self.output_height), "format": "RGB888"},
            raw={"size": sensor_res},
            controls={"FrameRate": float(self.framerate)},
        )
        self._cam.configure(cfg)
        self._native_w, self._native_h = cfg["main"]["size"]
        self._cam.start()
        logger.info(
            "Sensor mode: %dx%d (full FOV) → ISP output: %dx%d @ %d fps",
            sensor_res[0], sensor_res[1],
            self._native_w, self._native_h, self.framerate,
        )

        # Enable continuous autofocus (equivalent to rpicam-vid --autofocus-mode continuous).
        # AfMode 2 = Continuous, AfSpeed 1 = Fast.  Wrapped in try/except so the
        # code still works on cameras that don't support AF (e.g. fixed-focus modules).
        try:
            from libcamera import controls as lc  # noqa: PLC0415
            self._cam.set_controls({
                "AfMode": lc.AfModeEnum.Continuous,
                "AfSpeed": lc.AfSpeedEnum.Fast,
            })
            logger.info("Continuous autofocus enabled")
        except Exception as exc:
            logger.warning("Autofocus not available: %s", exc)

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info(
            "Camera started: %dx%d @ %d fps (ISP hardware-scaled)",
            self._native_w, self._native_h, self.framerate,
        )

    def get_frame(self) -> Optional[bytes]:
        """Return the most recent JPEG frame as bytes, or None if not ready."""
        with self._lock:
            return self._latest_jpeg

    def wait_for_frame(self, timeout: float = 1.0) -> bool:
        """Block until a new frame is available (or timeout). Returns True if a frame arrived."""
        arrived = self._frame_event.wait(timeout)
        if arrived:
            self._frame_event.clear()
        return arrived

    def stop(self) -> None:
        """Stop the capture thread and release the camera."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self._cam:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass
        logger.info("Camera stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        nw, nh = self._native_w, self._native_h
        while self._running:
            try:
                # capture_array returns an ndarray that may have stride padding;
                # trim to the exact pixel rectangle before encoding.
                frame_rgb = self._cam.capture_array("main")
                frame_rgb = frame_rgb[:nh, :nw]   # trim stride padding
                jpeg_bytes = self._encode_jpeg(frame_rgb)
                with self._lock:
                    self._latest_jpeg = jpeg_bytes
                self._frame_event.set()           # wake any waiting consumers
            except Exception as exc:
                logger.error("Frame capture error: %s", exc)
                time.sleep(0.1)

    @staticmethod
    def _encode_jpeg(frame_rgb) -> bytes:
        """BGR→RGB channel swap + JPEG encode. No resize needed (ISP already output correct size)."""
        from PIL import Image  # noqa: PLC0415

        # picamera2 returns RGB888 data in BGR memory order (V4L2 convention).
        img = Image.fromarray(frame_rgb[:, :, ::-1], "RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
