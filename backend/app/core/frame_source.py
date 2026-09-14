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

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import cv2
import numpy as np


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


def create_frame_source(source_name: str) -> FrameSource:
    """สร้างแหล่งภาพตามชื่อที่กำหนดใน config (FRAME_SOURCE)

    ห้ามมี fallback เงียบ ๆ: ถ้าชื่อไม่ตรงกับอะไรเลยต้องโยน error ให้เห็นชัด
    ไม่ใช่แอบคืน BrowserFrameSource มาให้แล้วผู้ใช้งงว่าทำไมกล้อง IP ไม่ทำงาน
    """
    if source_name == "browser":
        return BrowserFrameSource()

    if source_name == "rtsp":
        # เฟส 6 จะมา implement ตรงนี้
        raise NotImplementedError(
            "ยังไม่ได้ทำ RTSPFrameSource (มีในเฟส 6) "
            "ตอนนี้ตั้ง FRAME_SOURCE=browser ไปก่อน"
        )

    raise ValueError(
        f"ไม่รู้จักแหล่งภาพชื่อ {source_name!r} (รองรับเฉพาะ browser หรือ rtsp)"
    )
