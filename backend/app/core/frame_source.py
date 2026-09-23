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
from app.core.rate_meter import RateMeter

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

    ============================================================================
    ใช้สองแบบ (เฟส 8)
    ============================================================================

    1. โหมดเว็บแคมเดี่ยว (FRAME_SOURCE=browser, เฟส 2-5)
       สร้างโดยไม่ส่ง camera เข้ามา ใช้กับ /ws/detect ที่ตอบผลกลับไปให้เบราว์เซอร์
       วาดกรอบเอง ไม่มีเรื่องกล้องหลายตัวเข้ามาเกี่ยวเลย

    2. เป็น "กล้องตัวหนึ่ง" ในระบบกล้องหลายตัว (CAMERA_<ID>_SOURCE=browser)
       สร้างโดยส่ง camera เข้ามา แล้วทุกอย่างที่เหลือมองมันเหมือนกล้อง IP ทุกประการ
       (มี CameraRunner, เข้าคิวตรวจจับร่วมกัน, มีสถิติของตัวเอง, มีทิศทาง IN/OUT)
       ใช้ตอนที่ยังมีกล้อง Tapo ไม่ครบ แล้วเอาเว็บแคมของโน้ตบุ๊กมาแทนไปก่อน

    ============================================================================
    "ยังมีชีวิตอยู่" ของแหล่งแบบนี้หมายความว่าอะไร
    ============================================================================

    กล้อง IP ตายเมื่อ "เครือข่ายขาด" ส่วนแหล่งนี้ตายเมื่อ "ไม่มีใครส่งภาพมาแล้ว"
    ซึ่งเกิดได้ทั้งตอนปิดแท็บ ปิดกล้อง หรือเบราว์เซอร์หยุดส่งไปเฉย ๆ

    จึงวัดด้วยเกณฑ์เดียวกับ RTSP เป๊ะ คือ "เพิ่งได้เฟรมใหม่ภายในเวลาที่กำหนดไหม"
    ไม่ใช่ดูแค่ว่า WebSocket ยังต่ออยู่หรือเปล่า เพราะการเชื่อมต่อที่ยังเปิดค้าง
    แต่ไม่มีข้อมูลไหลเลย เป็นสถานะที่หน้าเว็บต้องเห็นว่า "หลุด" เหมือนกัน
    """

    def __init__(self, camera=None) -> None:  # camera: CameraSettings | None
        self.camera = camera
        self.name = f"browser:{camera.id}" if camera is not None else "browser"

        # ล็อกเพราะ submit() ถูกเรียกจาก thread ของ WebSocket
        # ส่วน read() อาจถูกเรียกจาก thread ประมวลผล (ตั้งแต่เฟส 3 เป็นต้นไป)
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._started = False

        # นับจำนวนเฟรมที่รับมาทั้งหมด ใช้ดูสถานะในหน้า health
        self._received_count = 0

        # ---- สถานะแบบเดียวกับกล้อง IP เพื่อให้หน้าเว็บแสดงผลด้วยโค้ดชุดเดียวกัน ----
        self._last_frame_at: float = 0.0
        self._frame_size: tuple[int, int] | None = None
        self._read_meter = RateMeter()

        # จำนวนเบราว์เซอร์ที่กำลังป้อนภาพให้กล้องตัวนี้อยู่
        # ใช้แยกสองกรณีที่ต่างกันมากให้ผู้ใช้เห็น:
        #     ไม่มีใครต่อเข้ามาเลย  -> "ยังไม่มีเบราว์เซอร์ส่งภาพ" (ปกติ ไม่ใช่ความผิดพลาด)
        #     ต่ออยู่แต่ภาพไม่มา    -> "ส่งภาพไม่ทัน / กล้องของเครื่องมีปัญหา"
        self._feeders = 0

        # นับจำนวนครั้งที่มีเบราว์เซอร์มาเริ่มป้อนภาพใหม่
        # เทียบเท่ากับตัวนับ reconnect ของกล้อง IP
        self._session_count = 0

        # ตั้งข้อความตั้งต้นไว้เลย ไม่ปล่อยเป็น None
        # เพราะถ้าว่างไว้ หน้าเว็บจะขึ้นว่า "ไม่ทราบสาเหตุ" ตั้งแต่เปิดหน้ามา
        # ทั้งที่จริงเรารู้สาเหตุแน่ชัดว่ายังไม่มีใครเริ่มส่งภาพ
        self._last_error: str | None = (
            "ยังไม่มีเบราว์เซอร์ส่งภาพเว็บแคมเข้ามา "
            "(กดปุ่มเริ่มดูภาพสดแล้วอนุญาตให้ใช้กล้องของเครื่อง)"
            if camera is not None else None
        )

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
        """ยังมีภาพใหม่ไหลเข้ามาอยู่จริงหรือไม่

        กล้องแบบ browser ที่เป็นส่วนหนึ่งของระบบกล้องหลายตัว ใช้เกณฑ์เดียวกับ RTSP
        คือดูว่า "เพิ่งได้เฟรมใหม่มาหรือเปล่า" ไม่ใช่ดูว่า WebSocket ยังต่ออยู่ไหม

        ส่วนโหมดเว็บแคมเดี่ยว (ไม่มี camera) ยังใช้ความหมายเดิมคือ
        "แหล่งภาพถูกเปิดใช้งานอยู่" เพราะจังหวะการส่งเป็นของเบราว์เซอร์ล้วน ๆ
        และหน้า health ในโหมดนั้นไม่ได้ใช้ค่านี้ตัดสินว่ากล้องหลุด
        """
        if not self._started:
            return False
        if self.camera is None:
            return True
        if not self._last_frame_at:
            return False
        return (time.monotonic() - self._last_frame_at) <= settings.rtsp.watchdog_timeout

    # ------------------------------------------------------------------
    # การเชื่อมต่อของเบราว์เซอร์ที่ป้อนภาพ
    # ------------------------------------------------------------------
    def attach_feeder(self) -> None:
        """เบราว์เซอร์เริ่มป้อนภาพให้กล้องตัวนี้"""
        with self._lock:
            self._feeders += 1
            self._session_count += 1
            self._last_error = None

    def detach_feeder(self) -> None:
        """เบราว์เซอร์เลิกป้อนภาพ (ปิดแท็บ / กดหยุด / การเชื่อมต่อหลุด)"""
        with self._lock:
            self._feeders = max(0, self._feeders - 1)
            if self._feeders == 0:
                self._last_error = (
                    "ไม่มีเบราว์เซอร์ส่งภาพเว็บแคมเข้ามาแล้ว "
                    "(กล้องแบบ browser จะมีภาพเฉพาะตอนเปิดหน้าเว็บค้างไว้)"
                )
                self._read_meter.reset()

    @property
    def feeder_count(self) -> int:
        with self._lock:
            return self._feeders

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

        now = time.monotonic()
        frame = Frame(
            frame_id=frame_id,
            image=image,
            received_at=now,
        )

        with self._lock:
            # ทับเฟรมเดิมทันที ไม่สะสมเป็นคิว
            self._latest = frame
            self._received_count += 1
            self._frame_size = (image.shape[1], image.shape[0])
            self._last_frame_at = now

        self._read_meter.tick()
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

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """ข้อมูลสำหรับ /api/health และหน้าเว็บ

        **รูปแบบต้องเหมือน RTSPFrameSource.status() ทุกกุญแจ**
        เพราะหน้าเว็บใช้โค้ดชุดเดียวกันแสดงผลกล้องทุกตัว ไม่แยกว่าเป็นชนิดไหน
        ถ้ารูปแบบต่างกัน จะต้องเขียน if แยกชนิดกล้องกระจายไปทั่วฝั่งหน้าเว็บ
        ซึ่งเป็นต้นทางของบั๊ก "กล้องตัวหนึ่งแสดงผลไม่เหมือนอีกตัว"
        """
        with self._lock:
            frame_size = self._frame_size
            feeders = self._feeders
            error = self._last_error
            received = self._received_count

        age = (
            time.monotonic() - self._last_frame_at
            if self._last_frame_at else None
        )

        camera = self.camera
        return {
            "id": camera.id if camera else "browser",
            "name": camera.name if camera else "เว็บแคมของเบราว์เซอร์",
            "direction": camera.direction if camera else None,
            "source": "browser",
            "url": camera.safe_url() if camera else "เว็บแคมของเบราว์เซอร์ (ไม่มี URL)",
            # "เชื่อมต่อแล้ว" ของกล้องแบบนี้ = มีเบราว์เซอร์กำลังป้อนภาพอยู่
            "connected": feeders > 0,
            "alive": self.is_alive(),
            "received_frames": received,
            # เทียบเท่าตัวนับ reconnect ของกล้อง IP คือจำนวนครั้งที่เริ่มป้อนภาพใหม่
            "reconnects": self._session_count,
            "feeders": feeders,
            "read_fps": round(self._read_meter.fps, 1),
            "resolution": list(frame_size) if frame_size else None,
            "last_frame_age_s": round(age, 1) if age is not None else None,
            "error": error,
        }


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

    ============================================================================
    กล้องแต่ละตัวเป็นอิสระต่อกันโดยสิ้นเชิง (เฟส 8)
    ============================================================================

    หนึ่ง instance ของคลาสนี้ = กล้องหนึ่งตัว และมีของเป็นของตัวเองครบทุกอย่าง:
        thread อ่านเฟรม / watchdog / ตัวนับ reconnect / สถิติ / เฟรมล่าสุด

    ผลที่ตั้งใจให้เกิด: ถอดปลั๊กกล้องตัวหนึ่ง thread ของตัวนั้นจะวนต่อใหม่ของมันเอง
    ส่วนอีกตัวไม่รู้เรื่องด้วยเลยและทำงานต่อได้ตามปกติ

    สิ่งที่ห้ามทำเด็ดขาดในคลาสนี้คือใส่ state ที่ใช้ร่วมกันระหว่างกล้อง
    (เช่นตัวแปรระดับโมดูล หรือ class attribute ที่เขียนค่าได้)
    เพราะกล้องที่มีปัญหาจะลากอีกตัวล้มตามไปด้วยทันที
    ส่วนของที่ "ต้องใช้ร่วมกันจริง ๆ" คือโมเดล AI ซึ่งอยู่คนละไฟล์ (face/, identify/)
    """

    def __init__(self, camera) -> None:  # camera: CameraSettings
        self.camera = camera
        self.name = f"rtsp:{camera.id}"

        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._frame_counter = 0

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # ---- สถานะสำหรับรายงานบนหน้าเว็บ (แยกกันคนละชุดต่อกล้อง) ----
        self._connected = False
        self._last_frame_at: float = 0.0
        self._last_error: str | None = None
        self._reconnect_count = 0
        self._received_count = 0

        # ความละเอียดที่กล้องตัวนี้ส่งมาจริง - ห้ามเดา เพราะกล้องคนละรุ่น
        # หรือคนละ path (/stream1 กับ /stream2) ให้ขนาดไม่เท่ากัน
        self._frame_size: tuple[int, int] | None = None

        # อัตราที่ "อ่านเฟรมจากกล้องตัวนี้ได้จริง" (เฟส 8: แยกรายกล้อง)
        # วัดที่ต้นทางเลย จะได้แยกออกว่าภาพกระตุกเพราะกล้อง/เครือข่าย
        # หรือเพราะ AI ตามไม่ทัน ซึ่งเป็นคนละปัญหาและแก้คนละวิธี
        self._read_meter = RateMeter()

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

            # ล้างค่าอัตราที่วัดไว้ ไม่งั้นหน้าเว็บจะยังโชว์ fps ของตอนที่ยังต่อติดอยู่
            # ค้างอยู่อีกหลายวินาที ทั้งที่กล้องหลุดไปแล้ว
            self._read_meter.reset()

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

        # ช่วงห่างขั้นต่ำระหว่างเฟรมที่เราจะ "เอามาใช้จริง" (เฟส 7)
        # ---- ตัวคุมอัตราแบบ token bucket ----
        #
        # ทำไมไม่ใช้วิธีง่าย ๆ อย่าง "ห่างจากเฟรมก่อนหน้าครบ 1/fps หรือยัง":
        # เพราะเฟรมจากกล้องมาไม่สม่ำเสมอ แต่มาเป็น "ช่อ" (burst)
        # เช่นสองเฟรมมาห่างกัน 20 ms แล้วเว้นไป 130 ms
        # วิธีวัดระยะห่างจะทิ้งเฟรมที่สองของทุกช่อ ทั้งที่อัตราเฉลี่ยยังไม่เกินที่ตั้งไว้เลย
        # วัดจริงแล้วกล้องส่งได้ 13 fps แต่เหลือถึงเราแค่ 9.6
        #
        # token bucket แก้ตรงนี้พอดี: เติม token ตามเวลาที่ผ่านไป
        # เฟรมหนึ่งใช้หนึ่ง token ถ้ามี token เหลือก็เก็บเฟรมได้แม้จะมาติดกัน
        # ส่วนเพดานของถัง (burst_size) กันไม่ให้สะสม token ไว้นานจนปล่อยรัวผิดปกติ
        capture_fps = max(1, settings.rates.capture_fps)
        burst_size = 2.0
        tokens = burst_size
        last_token_at = time.monotonic()

        while not self._stop_event.is_set():
            # ---- แยกการอ่านออกเป็นสองขั้น: grab แล้วค่อย retrieve ----
            #
            # grab()     = ดึงแพ็กเก็ตถัดไปออกจากสตรีม (ต้องทำทุกเฟรมเสมอ
            #              ไม่งั้นข้อมูลจะกองค้างอยู่ในบัฟเฟอร์แล้วภาพช้ากว่าจริง)
            # retrieve() = แปลงเป็นภาพ BGR ที่ใช้งานได้ ซึ่งเป็นขั้นที่กิน CPU
            #
            # เฟรมที่เกินอัตรา CAPTURE_FPS จึงถูก grab ทิ้งโดยไม่ต้อง retrieve
            # ประหยัด CPU ได้จริงเพราะไม่ต้องแปลงสีภาพที่ยังไงก็ไม่ได้ใช้
            ok = capture.grab()
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

            if not ok:
                # อ่านพลาดครั้งสองครั้งเป็นเรื่องปกติของเครือข่าย ยังไม่ต้องต่อใหม่
                # ปล่อยให้ watchdog ด้านบนเป็นคนตัดสินว่าพอแล้ว
                # หน่วงสั้น ๆ ไม่ให้วนกินซีพียูเปล่า ๆ ตอนกล้องมีปัญหา
                if self._stop_event.wait(0.05):
                    return
                continue

            # grab สำเร็จ = สตรีมยังมีชีวิตอยู่ นับเป็นเฟรมดีสำหรับ watchdog
            last_good_at = now

            # ---- เติม token ตามเวลาที่ผ่านไป แล้วดูว่ามีพอจะเก็บเฟรมนี้ไหม ----
            tokens = min(burst_size, tokens + (now - last_token_at) * capture_fps)
            last_token_at = now

            # token ไม่พอ = อัตราเฉลี่ยเกินที่ตั้งไว้แล้ว ทิ้งเฟรมนี้ไปเลย
            # ไม่ต้องเสียแรงแปลงเป็นภาพ BGR ซึ่งเป็นขั้นที่กิน CPU
            if tokens < 1.0:
                continue

            ok, image = capture.retrieve()
            if not ok or image is None:
                continue

            tokens -= 1.0

            with self._lock:
                self._frame_counter += 1
                # ทับเฟรมเดิมทันที ไม่สะสมเป็นคิว
                self._latest = Frame(
                    frame_id=self._frame_counter,
                    image=image,
                    received_at=now,
                )
                self._received_count += 1
                self._frame_size = (image.shape[1], image.shape[0])

            self._last_frame_at = now
            self._read_meter.tick()

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """ข้อมูลสำหรับ /api/health และหน้าเว็บ

        ทุกค่าในนี้เป็นของ "กล้องตัวนี้ตัวเดียว" ห้ามมีค่าที่รวมกับกล้องตัวอื่น
        เพราะจุดประสงค์คือให้ดูออกว่ากล้องตัวไหนมีปัญหา
        """
        age = (
            time.monotonic() - self._last_frame_at
            if self._last_frame_at else None
        )
        with self._lock:
            frame_size = self._frame_size

        return {
            "id": self.camera.id,
            "name": self.camera.name,
            "direction": self.camera.direction,
            "source": "rtsp",
            "url": self.camera.safe_url(),  # ปิดบังรหัสผ่านแล้ว
            "connected": self._connected,
            "alive": self.is_alive(),
            "received_frames": self._received_count,
            "reconnects": self._reconnect_count,
            "read_fps": round(self._read_meter.fps, 1),
            "resolution": list(frame_size) if frame_size else None,
            "last_frame_age_s": round(age, 1) if age is not None else None,
            "error": self._last_error,
        }


def create_frame_sources(source_name: str) -> dict[str, FrameSource]:
    """สร้างแหล่งภาพทั้งหมดตามค่า FRAME_SOURCE

    คืนเป็น dict ที่ใช้ "ชื่อกล้อง" เป็นกุญแจ เพราะโหมดกล้องหลายตัว
    มีได้หลายตัวพร้อมกัน ส่วนโหมดเว็บแคมเดี่ยวมีตัวเดียวชื่อ "browser"

    ตั้งแต่เฟส 8 กล้องแต่ละตัวในโหมด rtsp เลือกชนิดของตัวเองได้:

        CAMERA_DOOR_IN_SOURCE=rtsp      -> RTSPFrameSource   (backend ไปดึงเอง)
        CAMERA_DOOR_OUT_SOURCE=browser  -> BrowserFrameSource (เบราว์เซอร์ป้อนให้)

    ทั้งสองชนิดคืนออกไปในรูปแบบเดียวกัน ส่วนที่เหลือของระบบจึงไม่ต้องรู้ว่า
    กล้องตัวไหนเป็นชนิดไหนเลย

    ห้ามมี fallback เงียบ ๆ: ถ้าชื่อไม่ตรงกับอะไรเลยต้องโยน error ให้เห็นชัด
    ไม่ใช่แอบคืน BrowserFrameSource มาให้แล้วผู้ใช้งงว่าทำไมกล้อง IP ไม่ทำงาน
    """
    if source_name == "browser":
        # โหมดเว็บแคมเดี่ยวแบบเดิม (เฟส 2-5) ไม่ยุ่งกับ CAMERA_IDS เลย
        return {"browser": BrowserFrameSource()}

    if source_name == "rtsp":
        enabled = [c for c in settings.rtsp.cameras if c.enabled]
        if not enabled:
            raise ValueError(
                "ตั้ง FRAME_SOURCE=rtsp แต่ไม่มีกล้องที่เปิดใช้งานเลย "
                "(ตรวจ CAMERA_IDS และค่า ..._ENABLED ในไฟล์ .env)"
            )

        sources: dict[str, FrameSource] = {}
        for camera in enabled:
            if camera.is_rtsp:
                sources[camera.id] = RTSPFrameSource(camera)
            else:
                sources[camera.id] = BrowserFrameSource(camera)
        return sources

    raise ValueError(
        f"ไม่รู้จักแหล่งภาพชื่อ {source_name!r} (รองรับเฉพาะ browser หรือ rtsp)"
    )
