"""
CameraStreamer — MJPEG stream from a USB camera via OpenCV.

Camera index selection:
  Tries index 1 first (USB camera on a laptop that already has a built-in
  webcam at index 0), then falls back to index 0.

The capture runs in a background daemon thread so the async event loop is
never blocked. The FastAPI /api/video endpoint just reads the latest JPEG
frame from memory and yields it as a multipart/x-mixed-replace stream.
"""

import os
import sys
import threading
import time
from typing import Generator, Optional

# Suppress OpenCV/V4L2 log noise before any cv2 import.
# OPENCV_LOG_LEVEL=SILENT stops the C-level "WARN: can't open camera" lines.
# OPENCV_VIDEOIO_DEBUG=0  stops the ffmpeg-style "[v4l2 @...] Not a device" lines.
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

# JPEG quality (0-100)
JPEG_QUALITY = 80
# Target frame interval in seconds (~20 fps)
FRAME_INTERVAL = 0.05
# Boundary string used in the MJPEG stream
BOUNDARY = b"frame"
# Camera indices to try in order
CAMERA_INDICES = (1, 0)


class CameraStreamer:
    """
    Background-thread MJPEG streamer.

    Usage:
        cam = CameraStreamer()          # auto-detects camera
        cam.available                   # True / False
        cam.camera_index                # int index used, or None
        for chunk in cam.frames(): ...  # yields MJPEG boundary chunks
        cam.release()                   # clean shutdown
    """

    def __init__(self):
        self.available: bool = False
        self.camera_index: Optional[int] = None
        self._cap = None
        self._latest_frame: Optional[bytes] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        if not _CV2_AVAILABLE:
            print("⚠  opencv-python not installed — camera tab will be unavailable")
            return

        self._open_camera()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _open_camera(self):
        """Try each index in CAMERA_INDICES. Start capture thread on first success."""
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_V4L2
        for idx in CAMERA_INDICES:
            cap = cv2.VideoCapture(idx, backend)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    self._cap = cap
                    self.camera_index = idx
                    self.available = True
                    self._running = True
                    self._thread = threading.Thread(target=self._capture_loop, daemon=True)
                    self._thread.start()
                    print(f"✓ Camera opened at index {idx}")
                    return
            cap.release()
        print("⚠  No USB camera found (tried indices 1 and 0)")

    # ── Capture loop (background thread) ─────────────────────────────────────

    def _capture_loop(self):
        """Runs in a daemon thread. Log noise is suppressed globally via
        OPENCV_LOG_LEVEL / OPENCV_VIDEOIO_DEBUG env vars set before the cv2 import."""
        while self._running and self._cap and self._cap.isOpened():
            ret, frame = self._cap.read()
            if ret and frame is not None:
                _, buf = cv2.imencode(
                    ".jpg", frame,
                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                )
                with self._lock:
                    self._latest_frame = buf.tobytes()
            else:
                time.sleep(0.1)   # brief pause on read failure

        # Camera lost — mark unavailable so /api/camera/status reflects reality
        self._running = False
        self.available = False
        print("⚠  Camera capture loop ended (device disconnected or released)")

    # ── Public API ────────────────────────────────────────────────────────────

    def frames(self) -> Generator[bytes, None, None]:
        """
        Yields MJPEG boundary chunks while the camera is running.
        Returns (ends the generator) on disconnect so the browser sees
        the stream close and the error overlay is triggered.
        """
        if not self.available:
            return

        while self._running:
            with self._lock:
                frame = self._latest_frame
            if frame is None:
                time.sleep(FRAME_INTERVAL)
                continue
            yield (
                b"--" + BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
            time.sleep(FRAME_INTERVAL)

    def reconnect(self):
        """
        Attempt to reopen the camera (blocking — called via run_in_executor).
        Signals the capture thread to stop, waits for it to fully exit (join),
        then releases the device and opens a fresh one. Joining instead of
        sleeping prevents the race between cap.release() and cap.read() that
        causes a segfault on Linux/V4L2.
        """
        # 1. Signal the capture thread to stop
        self._running = False

        # 2. Wait for the thread to finish — MUST happen before cap.release()
        #    so cap.read() is no longer executing when we free the device.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

        # 3. Now safe to release the device
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

        self.available = False
        self.camera_index = None
        with self._lock:
            self._latest_frame = None

        print("Camera: attempting reconnect…")
        self._open_camera()

    def release(self):
        """
        Stop the capture thread and release the OpenCV device.
        Joins the thread before releasing so cap.read() cannot race with
        cap.release() (which causes a segfault on Linux/V4L2).
        """
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        if self._cap:
            self._cap.release()
            self._cap = None
        print("Camera released")
