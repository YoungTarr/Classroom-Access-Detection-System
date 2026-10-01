"""แหล่งที่มาของภาพ (FrameSource)

จุดประสงค์ของไฟล์นี้คือ **ซ่อนที่มาของภาพ** ไม่ให้ส่วนประมวลผลรู้ว่าเฟรมมาจากไหน
ส่วนที่ทำ detect/identify/track จะเห็นแค่ "เฟรมล่าสุด" เหมือนกันหมด

    เฟส 2: BrowserFrameSource      - เบราว์เซอร์ส่งภาพ JPEG เข้ามาทาง WebSocket
                                     (ใช้เฉพาะโหมดเว็บแคมเดี่ยว FRAME_SOURCE=browser)
    เฟส 6: RTSPFrameSource         - อ่านจากกล้อง IP ด้วย thread ของตัวเอง
    เฟส 9: NotInstalledFrameSource - ตัวแทนของกล้องที่ยังไม่ได้ติดตั้ง (HOST ว่าง)
                                     ไม่ทำอะไรเลย มีไว้ให้หน้าเว็บรู้ว่ากล้องตัวนี้มีอยู่

แหล่งที่ส่งภาพได้จริงทั้งสองแบบมีพฤติกรรมร่วมกันข้อหนึ่งที่สำคัญมาก:
**เก็บแค่เฟรมล่าสุดเฟรมเดียว เฟรมเก่าทิ้งทันที**
ถ้าเก็บเป็นคิว เมื่อประมวลผลตามไม่ทัน ภาพที่เห็นจะช้ากว่าความจริงเรื่อย ๆ
(latency ถ่างขึ้นไม่มีที่สิ้นสุด) ซึ่งเป็นอาการที่ยอมรับไม่ได้สำหรับระบบตรวจจับ
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import cv2
import numpy as np

from app.config import settings
from app.core.capture_worker import (
    H_CONNECT_ATTEMPTS,
    H_CONNECTED,
    H_FRAME_ID,
    H_HEIGHT,
    H_LAST_FRAME_AT,
    H_READ_FPS,
    H_RECEIVED_AT,
    H_RECONNECTS,
    H_WIDTH,
    HEADER_FIELDS,
    NUM_SLOTS,
    SLOT_BYTES,
    SharedHeader,
    read_header,
)

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
    """รับภาพที่เบราว์เซอร์ส่งเข้ามาทาง WebSocket (โหมดเว็บแคมเดี่ยว)

    ใช้เฉพาะตอน FRAME_SOURCE=browser เท่านั้น ซึ่งเป็นโหมดของ "ทั้งระบบ"
    มีไว้พัฒนา/ทดสอบบนเครื่องที่ไม่มีกล้อง IP เลย
    ภาพมาทาง /ws/detect แล้วผลตรวจจับถูกส่งกลับไปให้เบราว์เซอร์วาดกรอบเอง

    หมายเหตุเฟส 9: เฟส 8 เคยใช้คลาสนี้เป็น "กล้องตัวหนึ่ง" ในระบบกล้องหลายตัวด้วย
    (เอาเว็บแคมของโน้ตบุ๊กมาแทนกล้องขาออกที่ยังไม่มี) ตอนนี้เอาออกไปแล้ว
    กล้องในระบบกล้องหลายตัวเป็น RTSP ทุกตัว ส่วนกล้องที่ยังไม่ได้ซื้อ
    ใช้ NotInstalledFrameSource แทน

    ต่างจาก RTSP ตรงที่แหล่งนี้เป็นแบบ "ถูกป้อน" (push) ไม่ใช่ "ไปดึง" (pull)
    ตัว WebSocket handler จะเรียก submit() ทุกครั้งที่ได้รับเฟรมใหม่
    """

    # ไม่ได้ผูกกับกล้องตัวใดในระบบกล้องหลายตัว
    # main.py ใช้ค่านี้แยกว่าแหล่งภาพไหนต้องมี CameraRunner
    camera = None

    def __init__(self) -> None:
        self.name = "browser"

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
        """แหล่งภาพถูกเปิดใช้งานอยู่หรือไม่

        จังหวะการส่งภาพเป็นของเบราว์เซอร์ล้วน ๆ และหน้า health ในโหมดนี้
        ไม่ได้ใช้ค่านี้ตัดสินว่ากล้องหลุด จึงดูแค่ว่าเริ่มทำงานแล้วหรือยัง
        """
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


# =============================================================================
# สถานะของกล้องหนึ่งตัว (เฟส 9)
# =============================================================================
#
# หน้าเว็บและ /api/health ใช้ค่าเหล่านี้ตัดสินว่าจะแสดงอะไร
#
#   not_installed  ยังไม่ได้ติดตั้งกล้อง (ไม่ได้ตั้ง HOST) -> สีเทา ไม่ใช่ความผิดพลาด
#   connecting     ตั้ง HOST แล้วแต่ยังไม่ได้ภาพ -> ถ้ามี error ด้วย = "กล้องหลุด"
#   connected      ได้ภาพใหม่อยู่จริง
#
# "กล้องหลุด" ไม่ได้แยกเป็นสถานะที่สี่ เพราะในมุมของระบบมันคือ "กำลังพยายามต่อ"
# เหมือนกันทุกอย่าง (thread ยังวน retry อยู่) ต่างกันแค่มีสาเหตุให้บอกผู้ใช้
# หน้าเว็บจึงดูจาก state + error คู่กัน
CAMERA_STATE_NOT_INSTALLED = "not_installed"
CAMERA_STATE_CONNECTING = "connecting"
CAMERA_STATE_CONNECTED = "connected"


class NotInstalledFrameSource(FrameSource):
    """ตัวแทนของกล้องที่ยังไม่ได้ติดตั้ง (HOST ว่าง) - เฟส 9

    ============================================================================
    ทำไมต้องมีคลาสนี้ แทนที่จะไม่สร้างอะไรเลย
    ============================================================================

    ถ้าไม่สร้างอะไรเลย กล้องตัวนี้จะหายไปจาก /api/health และหน้าเว็บ
    แล้วผู้ใช้จะแยกไม่ออกระหว่าง "ยังไม่ได้ติดตั้ง" กับ "ลืมตั้งค่า / ระบบพัง"
    คลาสนี้ทำให้กล้องตัวนั้นยังมีตัวตนอยู่ในทุกที่ พร้อมบอกสถานะ not_installed ชัด ๆ

    ============================================================================
    สิ่งที่คลาสนี้ "ไม่ทำ" โดยตั้งใจ
    ============================================================================

        - ไม่สร้าง thread     -> ไม่กิน CPU แม้แต่นิดเดียว
        - ไม่พยายามต่อ        -> ไม่มี retry ไม่มี backoff ไม่มี FFmpeg
        - ไม่เขียน log ซ้ำ ๆ   -> main.py เขียนบรรทัดเดียวตอนสตาร์ทเท่านั้น
        - ไม่มี CameraRunner  -> main.py ข้ามกล้องตัวนี้ ไม่มีลูปตรวจจับ/ลูปส่งภาพ

    ติดตั้งกล้องแล้ว: ใส่ HOST ใน .env แล้วรีสตาร์ท backend
    ระบบจะสร้าง RTSPFrameSource ให้แทนคลาสนี้เอง ไม่ต้องแก้โค้ด
    """

    def __init__(self, camera) -> None:  # camera: CameraSettings
        self.camera = camera
        self.name = f"not_installed:{camera.id}"

    def start(self) -> None:
        # ตั้งใจให้ว่าง - ไม่มีอะไรต้องเริ่ม
        pass

    def stop(self) -> None:
        pass

    def read(self) -> Frame | None:
        return None

    def is_alive(self) -> bool:
        return False

    def status(self) -> dict:
        """รูปแบบเหมือน RTSPFrameSource.status() ทุกกุญแจ

        หน้าเว็บใช้โค้ดชุดเดียวแสดงผลกล้องทุกตัว ถ้ากุญแจไม่ครบ
        จะต้องเขียน if แยกกรณีกระจายไปทั่ว ซึ่งเป็นต้นทางของบั๊ก
        """
        camera = self.camera
        return {
            "id": camera.id,
            "name": camera.name,
            "direction": camera.direction,
            "url": camera.safe_url(),
            "state": CAMERA_STATE_NOT_INSTALLED,
            "installed": False,
            # ชื่อตัวแปรที่ต้องไปตั้ง หน้าเว็บเอาไปบอกผู้ใช้ตรง ๆ ไม่ต้องเดา
            "host_env": f"{camera.env_prefix}HOST",
            "connected": False,
            "alive": False,
            "received_frames": 0,
            "reconnects": 0,
            "connect_attempts": 0,
            "read_fps": 0.0,
            "resolution": None,
            "last_frame_age_s": None,
            # ไม่ใช่ error - ไม่มีอะไรเสีย แค่ยังไม่มีกล้อง
            "error": None,
        }


class RTSPFrameSource(FrameSource):
    """อ่านภาพจากกล้อง IP ผ่าน RTSP โดยให้ "process แยก" เป็นคนอ่าน (เฟส 6, ย้ายเป็น process ในเฟส 11)

    ============================================================================
    ทำไมต้องอ่านทิ้งตลอดเวลา และเก็บแค่เฟรมล่าสุด
    ============================================================================

    กล้องส่งภาพมาเรื่อย ๆ ตามอัตราของมัน (เช่น 15 fps) ไม่สนว่าเราจะประมวลผลทัน
    ถ้าเราอ่านช้ากว่าที่กล้องส่ง ภาพที่ได้จะเป็น "ภาพเก่าที่ค้างในคิวของ FFmpeg"
    และช้ากว่าความจริงเรื่อย ๆ จึงต้องอ่านให้เร็วที่สุดแล้วทิ้งของเก่า เก็บเฟรมล่าสุดเฟรมเดียว
    (cv2.CAP_PROP_BUFFERSIZE ใช้ไม่ได้กับ backend FFMPEG)

    ============================================================================
    ทำไมต้องเป็น process แยก ไม่ใช่ thread (เฟส 11)
    ============================================================================

    แบบ thread อยู่ใน process เดียวกับงานตรวจจับใบหน้า ทั้งสองแย่งคิว GIL กัน
    กล้องส่ง 15 fps แต่อ่านได้จริงแค่ 4-8 fps ทั้งที่ CPU ว่าง (สคริปต์แยกดึงกล้องตัวเดียวกัน
    ได้ 17 fps ในขณะที่ระบบทำงานเต็มที่) ตอนนี้ capture_worker.py เป็นคนอ่านกล้อง
    ใน process ของมันเอง แล้วเขียนเฟรมลง memory-mapped file ใน /dev/shm
    คลาสนี้ทำแค่ "หยิบเฟรมล่าสุดจากไฟล์นั้น" ซึ่งเบามาก

    interface ภายนอกเหมือนเดิมทุกอย่าง (start/stop/read/is_alive/status)

    ============================================================================
    กล้องแต่ละตัวเป็นอิสระต่อกันโดยสิ้นเชิง (เฟส 8)
    ============================================================================

    หนึ่ง instance = กล้องหนึ่งตัว = worker process หนึ่งตัว + ไฟล์ shared memory หนึ่งไฟล์
    ห้ามมี state ที่ใช้ร่วมกันระหว่างกล้อง ถอดปลั๊กกล้องหนึ่งตัวต้องไม่กระทบอีกตัว
    """

    # ถ้า worker ตายเอง จะสตาร์ทใหม่ให้ แต่ไม่ถี่เกินครั้งละกี่วินาที
    RESTART_COOLDOWN = 3.0

    def __init__(self, camera) -> None:  # camera: CameraSettings
        self.camera = camera
        self.name = f"rtsp:{camera.id}"

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._shared: SharedHeader | None = None
        self._shm_path: str | None = None
        self._stopping = False
        self._last_spawn_at = 0.0

        # เฟรมล่าสุดที่คัดลอกออกมาแล้ว - read() ถูกเรียกถี่มาก (หลายสิบครั้งต่อวินาที)
        # ต้องคัดลอกภาพ (~6 MB) เฉพาะตอนมีเฟรมใหม่จริง ๆ
        self._latest: Frame | None = None

    # ------------------------------------------------------------------
    # วงจรชีวิต
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._proc is not None:
            return

        self._stopping = False

        # ชื่อไฟล์มี pid กันชนกันถ้ามีหลาย backend บนเครื่องเดียว
        shm_dir = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
        self._shm_path = os.path.join(shm_dir, f"cads_cam{self.camera.id}_{os.getpid()}.shm")
        self._shared = SharedHeader(self._shm_path, create=True)

        self._spawn()

    def _spawn(self) -> None:
        cfg = {
            "camera_id": self.camera.id,
            "url": self.camera.rtsp_url,
            "safe_url": self.camera.safe_url(),
            "password": self.camera.password,
            "shm_path": self._shm_path,
            "capture_fps": settings.rates.capture_fps,
            "ffmpeg_options": settings.rtsp.ffmpeg_options,
            "watchdog_timeout": settings.rtsp.watchdog_timeout,
            "reconnect_initial": settings.rtsp.reconnect_initial_delay,
            "reconnect_max": settings.rtsp.reconnect_max_delay,
        }

        # ส่งค่าผ่าน env ไม่ใช่ argv เพราะ URL มีรหัสผ่านกล้อง (argv เห็นได้ด้วย ps)
        env = dict(os.environ)
        env["CADS_CAPTURE_CONFIG"] = json.dumps(cfg)

        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.core.capture_worker"],
            env=env,
        )
        self._last_spawn_at = time.monotonic()
        logger.info(
            "เริ่ม process อ่านกล้อง %s (pid %d, %s)",
            self.camera.id, self._proc.pid, self.camera.safe_url(),
        )

    def stop(self) -> None:
        self._stopping = True
        proc, self._proc = self._proc, None

        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                # FFmpeg อาจค้างอยู่ใน grab() ไม่ตอบสัญญาณ - ฆ่าเลย
                proc.kill()
                proc.wait(timeout=3.0)

        shared, self._shared = self._shared, None
        if shared is not None:
            shared.close()
        if self._shm_path:
            try:
                os.unlink(self._shm_path)
            except OSError:
                pass
            self._shm_path = None

        with self._lock:
            self._latest = None
        logger.info("หยุด process อ่านกล้อง %s", self.camera.id)

    def _supervise(self) -> None:
        """worker ตายเองโดยไม่ได้สั่ง (เช่นถูก OOM kill) -> สตาร์ทใหม่ให้"""
        proc = self._proc
        if proc is None or self._stopping or proc.poll() is None:
            return
        if time.monotonic() - self._last_spawn_at < self.RESTART_COOLDOWN:
            return
        logger.error(
            "process อ่านกล้อง %s ตายเอง (exit code %s) กำลังสตาร์ทใหม่",
            self.camera.id, proc.returncode,
        )
        self._spawn()

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    def _header(self) -> tuple[np.ndarray, str | None] | None:
        shared = self._shared
        if shared is None:
            return None
        info = read_header(shared)
        return info["values"], info["error"]

    def is_alive(self) -> bool:
        """ยังได้รับภาพจากกล้องอยู่จริงหรือไม่

        ดูว่า "เพิ่งได้ภาพใหม่มาหรือเปล่า" ไม่ใช่แค่ว่า process ยังอยู่ เพราะ RTSP ค้างได้
        โดยที่ process ยังหมุนปกติ
        """
        self._supervise()
        header = self._header()
        if header is None or self._proc is None or self._proc.poll() is not None:
            return False
        values, _ = header
        if not values[H_CONNECTED]:
            return False
        return bool((time.monotonic() - values[H_LAST_FRAME_AT]) <= settings.rtsp.watchdog_timeout)

    # ------------------------------------------------------------------
    # อ่านออก
    # ------------------------------------------------------------------
    def read(self) -> Frame | None:
        self._supervise()

        shared = self._shared
        if shared is None:
            return None

        with self._lock:
            cached = self._latest

            for _ in range(3):
                values = read_header(shared)["values"]
                frame_id = int(values[H_FRAME_ID])
                if frame_id == 0:
                    return cached
                if cached is not None and cached.frame_id == frame_id:
                    return cached

                width, height = int(values[H_WIDTH]), int(values[H_HEIGHT])
                nbytes = width * height * 3
                if nbytes <= 0 or nbytes > SLOT_BYTES:
                    return cached

                slot = shared.slot(frame_id % NUM_SLOTS)
                image = slot[:nbytes].reshape(height, width, 3).copy()

                # worker ใช้ 3 ช่องวนกัน ช่องที่เราเพิ่งคัดลอกจะถูกเขียนทับเมื่อมีเฟรมใหม่
                # ถึงสองเฟรมซ้อน ถ้าเป็นแบบนั้นภาพที่ได้อาจฉีก ทิ้งแล้วอ่านใหม่
                if int(shared.fields[H_FRAME_ID]) - frame_id > NUM_SLOTS - 2:
                    continue

                self._latest = Frame(
                    frame_id=frame_id,
                    image=image,
                    received_at=float(values[H_RECEIVED_AT]),
                )
                return self._latest

            return cached

    def status(self) -> dict:
        """ข้อมูลสำหรับ /api/health และหน้าเว็บ

        ทุกค่าในนี้เป็นของ "กล้องตัวนี้ตัวเดียว" ห้ามมีค่าที่รวมกับกล้องตัวอื่น
        """
        alive = self.is_alive()
        header = self._header()

        if header is None:
            values, error = np.zeros(HEADER_FIELDS), None
        else:
            values, error = header

        if self._proc is not None and self._proc.poll() is not None and not error:
            error = f"process อ่านกล้องหยุดทำงาน (exit code {self._proc.returncode})"

        last_frame_at = values[H_LAST_FRAME_AT]
        age = (time.monotonic() - last_frame_at) if last_frame_at else None
        width, height = int(values[H_WIDTH]), int(values[H_HEIGHT])

        return {
            "id": self.camera.id,
            "name": self.camera.name,
            "direction": self.camera.direction,
            "url": self.camera.safe_url(),  # ปิดบังรหัสผ่านแล้ว
            # สามสถานะของเฟส 9 (ดูคำอธิบายที่ CAMERA_STATE_* ด้านบน)
            "state": CAMERA_STATE_CONNECTED if alive else CAMERA_STATE_CONNECTING,
            "installed": True,
            "host_env": f"{self.camera.env_prefix}HOST",
            "connected": bool(values[H_CONNECTED]),
            "alive": alive,
            "received_frames": int(values[H_FRAME_ID]),
            "reconnects": int(values[H_RECONNECTS]),
            "connect_attempts": int(values[H_CONNECT_ATTEMPTS]),
            "read_fps": round(float(values[H_READ_FPS]), 1),
            "resolution": [width, height] if width and height else None,
            "last_frame_age_s": round(age, 1) if age is not None else None,
            "error": error,
        }


def create_frame_sources(source_name: str) -> dict[str, FrameSource]:
    """สร้างแหล่งภาพทั้งหมดตามค่า FRAME_SOURCE

    คืนเป็น dict ที่ใช้ "ชื่อกล้อง" เป็นกุญแจ เพราะโหมดกล้องหลายตัว
    มีได้หลายตัวพร้อมกัน ส่วนโหมดเว็บแคมเดี่ยวมีตัวเดียวชื่อ "browser"

    ในโหมด rtsp กล้องแต่ละตัวได้แหล่งภาพตามสถานะการติดตั้ง (เฟส 9):

        CAMERA_1_HOST=192.168.1.158  -> RTSPFrameSource         (backend ไปดึงเอง)
        CAMERA_2_HOST=               -> NotInstalledFrameSource (ไม่ทำอะไรเลย)

    ทั้งสองชนิดคืน status() รูปแบบเดียวกัน ส่วนที่เหลือของระบบจึงแสดงผล
    กล้องทุกตัวได้ด้วยโค้ดชุดเดียว

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
            if camera.installed:
                sources[camera.id] = RTSPFrameSource(camera)
            else:
                sources[camera.id] = NotInstalledFrameSource(camera)
        return sources

    raise ValueError(
        f"ไม่รู้จักแหล่งภาพชื่อ {source_name!r} (รองรับเฉพาะ browser หรือ rtsp)"
    )
