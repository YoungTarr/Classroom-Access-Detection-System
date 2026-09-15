"""แหล่งที่มาของภาพ (FrameSource)

จุดประสงค์ของไฟล์นี้คือ **ซ่อนที่มาของภาพ** ไม่ให้ส่วนประมวลผลรู้ว่าเฟรมมาจากไหน
ส่วนที่ทำ detect/identify/track จะเห็นแค่ "เฟรมล่าสุด" เหมือนกันหมด

    เฟส 2: BrowserFrameSource  - เบราว์เซอร์ส่งภาพ JPEG เข้ามาทาง WebSocket
    เฟส 6: RTSPFrameSource     - อ่านจากกล้อง IP ด้วย thread ของตัวเอง

ทั้งสองแบบมีพฤติกรรมร่วมกันข้อหนึ่งที่สำคัญมาก:
**เก็บแค่เฟรมล่าสุดเฟรมเดียว เฟรมเก่าทิ้งทันที**
ถ้าเก็บเป็นคิว เมื่อประมวลผลตามไม่ทัน ภาพที่เห็นจะช้ากว่าความจริงเรื่อย ๆ
(latency ถ่างขึ้นไม่มีที่สิ้นสุด) ซึ่งเป็นอาการที่ยอมรับไม่ได้สำหรับระบบตรวจจับ
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import cv2
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)


class FrameDecodeError(RuntimeError):
    """ถอดรหัสภาพที่รับมาไม่สำเร็จ (ไฟล์เสีย / ไม่ใช่ JPEG / ส่งมาไม่ครบ)"""


@dataclass(frozen=True)
class Frame:
    """เฟรมภาพหนึ่งเฟรมพร้อมข้อมูลกำกับ"""

    # เลขลำดับเฟรม ฝั่งที่ส่งมาเป็นคนกำหนด ใช้จับคู่ผลลัพธ์กลับไปหาเฟรมต้นทาง
    frame_id: int

    # ภาพในรูปแบบ numpy array ระบบสี BGR ตามมาตรฐานของ OpenCV
    image: np.ndarray

    # เวลาที่ได้รับเฟรมนี้ (time.monotonic) ใช้วัด latency และทำ watchdog ในเฟส 6
    # ใช้ monotonic ไม่ใช่ time.time() เพราะต้องไม่กระโดดเมื่อนาฬิกาเครื่องถูกปรับ
    received_at: float

    @property
    def size(self) -> tuple[int, int]:
        """ขนาดภาพเป็น (กว้าง, สูง)"""
        height, width = self.image.shape[:2]
        return width, height


class FrameSource(ABC):
    """interface ของแหล่งภาพทุกชนิด"""

    #: ชื่อที่ใช้แสดงใน log และหน้า health
    name: str = "unknown"

    @abstractmethod
    def start(self) -> None:
        """เริ่มรับภาพ (RTSP จะเริ่ม thread อ่านกล้องตรงนี้)"""

    @abstractmethod
    def stop(self) -> None:
        """หยุดรับภาพและคืนทรัพยากรทั้งหมด"""

    @abstractmethod
    def read(self) -> Frame | None:
        """คืนเฟรมล่าสุด หรือ None ถ้ายังไม่เคยได้รับเฟรมเลย

        ต้องไม่บล็อกรอเฟรมใหม่ และต้องไม่คืนเฟรมที่เคยถูกอ่านไปแล้วซ้ำ ๆ
        โดยไม่มีทางรู้ว่าซ้ำ (ผู้เรียกดูได้จาก frame_id)
        """

    @abstractmethod
    def is_alive(self) -> bool:
        """แหล่งภาพยังทำงานปกติอยู่หรือไม่ (ใช้รายงานใน /api/health)"""


class BrowserFrameSource(FrameSource):
    """รับภาพที่เบราว์เซอร์ส่งเข้ามาทาง WebSocket

    ต่างจาก RTSP ตรงที่แหล่งนี้เป็นแบบ "ถูกป้อน" (push) ไม่ใช่ "ไปดึง" (pull)
    ตัว WebSocket handler จะเรียก submit() ทุกครั้งที่ได้รับเฟรมใหม่
    """

    name = "browser"

    def __init__(self) -> None:
        # ล็อกเพราะ submit() ถูกเรียกจาก thread ของ WebSocket
        # ส่วน read() อาจถูกเรียกจาก thread ประมวลผล (ตั้งแต่เฟส 3 เป็นต้นไป)
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._started = False

        # นับจำนวนเฟรมที่รับมาทั้งหมด ใช้ดูสถานะในหน้า health
        self._received_count = 0

    # ------------------------------------------------------------------
    # วงจรชีวิต
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        with self._lock:
            self._latest = None

    def is_alive(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # รับภาพเข้า
    # ------------------------------------------------------------------
    def submit(self, frame_id: int, jpeg_bytes: bytes) -> Frame:
        """ถอดรหัส JPEG ที่รับมาแล้วเก็บเป็นเฟรมล่าสุด

        โยน FrameDecodeError เมื่อถอดรหัสไม่ได้ ผู้เรียกต้องรายงานกลับไปให้
        เบราว์เซอร์เห็น ไม่ใช่กลืนเงียบ ๆ แล้วปล่อยให้ภาพหายไปเฉย ๆ
        """
        if not jpeg_bytes:
            raise FrameDecodeError("ได้รับข้อมูลภาพว่างเปล่า (0 ไบต์)")

        buffer = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)

        if image is None:
            raise FrameDecodeError(
                f"ถอดรหัสภาพไม่สำเร็จ (ขนาดข้อมูล {len(jpeg_bytes)} ไบต์) "
                "อาจไม่ใช่ไฟล์ JPEG หรือส่งมาไม่ครบ"
            )

        frame = Frame(
            frame_id=frame_id,
            image=image,
            received_at=time.monotonic(),
        )

        with self._lock:
            # ทับเฟรมเดิมทันที ไม่สะสมเป็นคิว
            self._latest = frame
            self._received_count += 1

        return frame

    # ------------------------------------------------------------------
    # อ่านออก
    # ------------------------------------------------------------------
    def read(self) -> Frame | None:
        with self._lock:
            return self._latest

    @property
    def received_count(self) -> int:
        """จำนวนเฟรมที่รับมาแล้วทั้งหมดนับตั้งแต่สตาร์ท"""
        with self._lock:
            return self._received_count


class RTSPFrameSource(FrameSource):
    """อ่านภาพจากกล้อง IP ผ่าน RTSP ด้วย thread ของตัวเอง (เฟส 6)

    ============================================================================
    ทำไมต้องมี thread แยกที่อ่านทิ้งตลอดเวลา
    ============================================================================

    กล้องส่งภาพมาเรื่อย ๆ ตามอัตราของมัน (เช่น 15 fps) ไม่สนว่าเราจะประมวลผลทัน
    ภาพที่ยังไม่ได้อ่านจะกองอยู่ในบัฟเฟอร์ของ FFmpeg

    ถ้าเราอ่านช้ากว่าที่กล้องส่ง (ซึ่งเกิดแน่นอน เพราะ AI ใช้เวลา ~100 ms ต่อเฟรม
    แต่กล้องส่งทุก 66 ms) ทุกครั้งที่เรียก read() จะได้ "ภาพเก่าที่ค้างในคิว"
    ไม่ใช่ภาพปัจจุบัน และคิวจะยาวขึ้นเรื่อย ๆ

    อาการที่เห็นคือ ตอนเริ่มภาพช้ากว่าจริง 1 วินาที ผ่านไป 10 นาทีช้ากว่าจริงเป็นนาที
    ซึ่งใช้งานไม่ได้เลยสำหรับระบบตรวจจับคนเข้า-ออก

    วิธีแก้คือให้ thread นี้ "อ่านให้เร็วที่สุดเท่าที่กล้องส่งมา" แล้วทิ้งของเก่า
    เก็บแค่เฟรมล่าสุดเฟรมเดียว ส่วน pipeline มาหยิบเฟรมล่าสุดไปใช้เมื่อพร้อม

    หมายเหตุ: cv2.CAP_PROP_BUFFERSIZE ใช้แก้ปัญหานี้ไม่ได้กับ backend FFMPEG
    (ตั้งได้แต่ไม่มีผล) จึงต้องใช้วิธี thread อ่านทิ้งเท่านั้น
    """

    def __init__(self, camera) -> None:  # camera: CameraSettings
        self.camera = camera
        self.name = f"rtsp:{camera.id}"

        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._frame_counter = 0

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # ---- สถานะสำหรับรายงานบนหน้าเว็บ ----
        self._connected = False
        self._last_frame_at: float = 0.0
        self._last_error: str | None = None
        self._reconnect_count = 0
        self._received_count = 0

    # ------------------------------------------------------------------
    # วงจรชีวิต
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"rtsp-{self.camera.id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "เริ่ม thread อ่านกล้อง %s (%s)",
            self.camera.id,
            self.camera.safe_url(),  # ปิดบังรหัสผ่านไว้แล้ว
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            # รอไม่นาน ถ้า read() ยังค้างอยู่ก็ปล่อยไป เพราะเป็น daemon thread
            self._thread.join(timeout=3.0)
            self._thread = None
        self._connected = False
        logger.info("หยุด thread อ่านกล้อง %s", self.camera.id)

    def is_alive(self) -> bool:
        """ยังได้รับภาพจากกล้องอยู่จริงหรือไม่

        ไม่ได้ดูแค่ว่า thread ยังทำงานอยู่ไหม แต่ดูว่า "เพิ่งได้ภาพใหม่มาหรือเปล่า"
        เพราะ RTSP ค้างได้โดยที่ thread ยังวนอยู่ปกติ
        """
        if not self._connected:
            return False
        return (time.monotonic() - self._last_frame_at) <= settings.rtsp.watchdog_timeout

    # ------------------------------------------------------------------
    # อ่านออก
    # ------------------------------------------------------------------
    def read(self) -> Frame | None:
        with self._lock:
            return self._latest

    # ------------------------------------------------------------------
    # thread หลัก
    # ------------------------------------------------------------------
    def _run(self) -> None:
        delay = settings.rtsp.reconnect_initial_delay

        while not self._stop_event.is_set():
            capture = self._open_capture()

            if capture is None:
                # ต่อไม่ติด - รอแล้วลองใหม่ โดยเพิ่มเวลารอเป็นเท่าตัว
                # (1, 2, 4, 8, ... จนถึงเพดาน) เพื่อไม่ให้ยิงถี่ ๆ ใส่กล้องที่ยังไม่พร้อม
                logger.warning(
                    "กล้อง %s ต่อไม่ได้ จะลองใหม่ในอีก %.0f วินาที (%s)",
                    self.camera.id, delay, self._last_error,
                )
                if self._stop_event.wait(delay):
                    break
                delay = min(delay * 2, settings.rtsp.reconnect_max_delay)
                continue

            # ต่อติดแล้ว รีเซ็ตเวลารอกลับไปค่าเริ่มต้น
            delay = settings.rtsp.reconnect_initial_delay
            self._read_loop(capture)

            # หลุดออกจาก _read_loop แปลว่าสตรีมมีปัญหา ต้องปิดแล้วต่อใหม่
            capture.release()
            self._connected = False

            if not self._stop_event.is_set():
                self._reconnect_count += 1
                logger.warning(
                    "กล้อง %s สตรีมหลุด (%s) กำลังต่อใหม่ครั้งที่ %d",
                    self.camera.id, self._last_error, self._reconnect_count,
                )

    def _open_capture(self):
        """เปิดการเชื่อมต่อ RTSP

        **สำคัญมาก** ต้องตั้ง OPENCV_FFMPEG_CAPTURE_OPTIONS ก่อนสร้าง VideoCapture
        เพราะ FFmpeg อ่านค่านี้ตอน "เปิดสตรีม" เท่านั้น
        ถ้าตั้งหลังจากสร้างไปแล้ว จะไม่มีผลใด ๆ และหาสาเหตุยากมาก
        เพราะภาพจะแตกเป็นบล็อกโดยไม่มี error อะไรออกมาเลย
        """
        import cv2

        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = settings.rtsp.ffmpeg_options

        try:
            capture = cv2.VideoCapture(self.camera.rtsp_url, cv2.CAP_FFMPEG)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"สร้าง VideoCapture ไม่สำเร็จ: {exc}"
            return None

        if not capture.isOpened():
            capture.release()
            self._last_error = (
                f"เปิดสตรีมไม่ได้ที่ {self.camera.safe_url()} - "
                "ตรวจว่ากล้องเปิดอยู่ ต่อเน็ตเวิร์กเดียวกัน "
                "และบัญชีผู้ใช้/รหัสผ่านถูกต้อง"
            )
            return None

        self._last_error = None
        self._connected = True
        self._last_frame_at = time.monotonic()

        logger.info(
            "ต่อกล้อง %s สำเร็จ (%s) ความละเอียด %dx%d",
            self.camera.id,
            self.camera.safe_url(),
            int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        return capture

    def _read_loop(self, capture) -> None:
        """วนอ่านเฟรมให้เร็วที่สุด เก็บแค่เฟรมล่าสุด

        ออกจากลูปเมื่อสตรีมมีปัญหา เพื่อให้ตัวเรียกไปต่อใหม่
        """
        timeout = settings.rtsp.watchdog_timeout
        last_good_at = time.monotonic()

        while not self._stop_event.is_set():
            ok, image = capture.read()
            now = time.monotonic()

            # ---- watchdog: ไม่ได้เฟรมใหม่นานเกินไป ----
            #
            # นี่คือจุดที่สำคัญที่สุดของการ reconnect
            # **ห้ามเช็คแค่ว่า read() คืน False** เพราะ RTSP ค้างได้แบบไม่มี error เลย
            # อาการที่เจอจริงคือ read() บล็อกนานมากแล้วค่อยคืนเฟรมเก่า ๆ กลับมา
            # หรือคืน True เรื่อย ๆ แต่ภาพไม่ขยับ
            # การวัดจาก "เวลาที่ผ่านไปตั้งแต่ได้เฟรมดีล่าสุด" จับได้ทุกกรณี
            if now - last_good_at > timeout:
                self._last_error = (
                    f"ไม่ได้เฟรมใหม่เกิน {timeout:.0f} วินาที "
                    "(กล้องอาจถูกถอดปลั๊ก หรือหลุดจากเครือข่าย)"
                )
                return

            if not ok or image is None:
                # อ่านพลาดครั้งสองครั้งเป็นเรื่องปกติของเครือข่าย ยังไม่ต้องต่อใหม่
                # ปล่อยให้ watchdog ด้านบนเป็นคนตัดสินว่าพอแล้ว
                # หน่วงสั้น ๆ ไม่ให้วนกินซีพียูเปล่า ๆ ตอนกล้องมีปัญหา
                if self._stop_event.wait(0.05):
                    return
                continue

            last_good_at = now

            with self._lock:
                self._frame_counter += 1
                # ทับเฟรมเดิมทันที ไม่สะสมเป็นคิว
                self._latest = Frame(
                    frame_id=self._frame_counter,
                    image=image,
                    received_at=now,
                )
                self._received_count += 1

            self._last_frame_at = now

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """ข้อมูลสำหรับ /api/health และหน้าเว็บ"""
        age = (
            time.monotonic() - self._last_frame_at
            if self._last_frame_at else None
        )
        return {
            "id": self.camera.id,
            "name": self.camera.name,
            "direction": self.camera.direction,
            "url": self.camera.safe_url(),  # ปิดบังรหัสผ่านแล้ว
            "connected": self._connected,
            "alive": self.is_alive(),
            "received_frames": self._received_count,
            "reconnects": self._reconnect_count,
            "last_frame_age_s": round(age, 1) if age is not None else None,
            "error": self._last_error,
        }


def create_frame_sources(source_name: str) -> dict[str, FrameSource]:
    """สร้างแหล่งภาพทั้งหมดตามค่า FRAME_SOURCE

    คืนเป็น dict เพราะโหมด rtsp มีได้หลายกล้องพร้อมกัน
    ส่วนโหมด browser มีตัวเดียวชื่อ "browser"

    ห้ามมี fallback เงียบ ๆ: ถ้าชื่อไม่ตรงกับอะไรเลยต้องโยน error ให้เห็นชัด
    ไม่ใช่แอบคืน BrowserFrameSource มาให้แล้วผู้ใช้งงว่าทำไมกล้อง IP ไม่ทำงาน
    """
    if source_name == "browser":
        return {"browser": BrowserFrameSource()}

    if source_name == "rtsp":
        enabled = [c for c in settings.rtsp.cameras if c.enabled]
        if not enabled:
            raise ValueError(
                "ตั้ง FRAME_SOURCE=rtsp แต่ไม่มีกล้องที่เปิดใช้งานเลย "
                "(ตรวจ CAMERA_IDS และค่า ..._ENABLED ในไฟล์ .env)"
            )
        return {camera.id: RTSPFrameSource(camera) for camera in enabled}

    raise ValueError(
        f"ไม่รู้จักแหล่งภาพชื่อ {source_name!r} (รองรับเฉพาะ browser หรือ rtsp)"
    )
