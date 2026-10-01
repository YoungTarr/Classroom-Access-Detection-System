"""บันทึกประวัติการเข้า-ออกห้องลง PostgreSQL (เฟส 12)

กติกาของระบบ:
    กล้องที่ตั้งทิศทาง IN  จดจำสมาชิกได้ -> บันทึก "เข้า"
    กล้องที่ตั้งทิศทาง OUT จดจำสมาชิกได้ -> บันทึก "ออก"
ทิศทางมาจากการตั้งค่ากล้อง ไม่ได้ดูว่าคนเดินซ้ายหรือขวาในภาพ

============================================================================
ทำไมต้องมีคิวและ thread แยก
============================================================================

ลูปตรวจจับเดินใน event loop เดียวกับการส่งภาพให้หน้าเว็บ ถ้าเขียนฐานข้อมูลตรงนั้น
ฐานข้อมูลช้าหรือล่มเมื่อไหร่ ภาพของทุกกล้องจะค้างตามไปด้วย
จึงให้ลูปตรวจจับแค่ "หย่อนรายการลงคิว" (ไม่บล็อก) แล้วให้ thread นี้ค่อย ๆ เขียนให้

============================================================================
กันบันทึกซ้ำ
============================================================================

สองชั้น: (1) CameraRunner บันทึกครั้งเดียวต่อ track  (2) คลาสนี้เว้นช่วง cooldown
สำหรับ "คนเดิม + ทิศทางเดิม" เผื่อ track ขาดแล้วสร้างใหม่ตอนยืนหน้ากล้องนาน ๆ
คนเดิมแต่ทิศทางต่างกัน (เข้าแล้วออก) ไม่ถูกกันไว้ เพราะเป็นเหตุการณ์จริงคนละอย่าง
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import settings
from app.db.database import Database

logger = logging.getLogger(__name__)

# ถ้าเขียนไม่สำเร็จ ลองซ้ำกี่ครั้ง (ห่างกันครั้งละ RETRY_DELAY วินาที)
MAX_ATTEMPTS = 3
RETRY_DELAY = 1.0


@dataclass(frozen=True)
class AccessEvent:
    """เหตุการณ์ที่ต้องบันทึกหนึ่งรายการ"""

    student_id: str
    first_name: str
    last_name: str
    direction: str  # "IN" หรือ "OUT"
    camera_id: str
    camera_name: str
    logged_at: datetime  # เวลาที่ตรวจเจอ (มี timezone)
    bbox: tuple[int, int, int, int]  # x, y, w, h เป็นพิกเซลของเฟรมต้นฉบับ
    frame_size: tuple[int, int]  # กว้าง, สูง
    confidence: float | None
    track_id: int | None

    def as_row(self) -> dict[str, Any]:
        x, y, w, h = self.bbox
        return {
            "student_id": self.student_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "logged_at": self.logged_at,
            "direction": self.direction,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "bbox_x": int(x),
            "bbox_y": int(y),
            "bbox_w": int(w),
            "bbox_h": int(h),
            "frame_width": int(self.frame_size[0]),
            "frame_height": int(self.frame_size[1]),
            "confidence": self.confidence,
            "track_id": self.track_id,
        }


class AccessLogger:
    """รับเหตุการณ์จากกล้องทุกตัว แล้วเขียนลงฐานข้อมูลใน thread ของตัวเอง"""

    def __init__(self, database: Database) -> None:
        cfg = settings.access_log
        self._database = database
        self._enabled = cfg.enabled
        self._cooldown = cfg.cooldown_seconds

        self._queue: queue.Queue[AccessEvent] = queue.Queue(maxsize=cfg.queue_size)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # เวลา (monotonic) ที่บันทึกล่าสุดของ (student_id, direction)
        # เข้าถึงจาก event loop thread (submit) เท่านั้น จึงไม่ต้องล็อก
        self._last_logged: dict[tuple[str, str], float] = {}

        # สถิติ (เขียนจากหลาย thread ใช้ล็อกกัน)
        self._stats_lock = threading.Lock()
        self._written = 0
        self._skipped_cooldown = 0
        self._dropped_queue_full = 0
        self._failed = 0
        self._last_error: str | None = None
        self._last_written: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # วงจรชีวิต
    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self._enabled:
            logger.info("การบันทึกประวัติเข้า-ออกถูกปิดไว้ (ACCESS_LOG_ENABLED=false)")
            return
        if self._thread is not None:
            return

        try:
            self._database.ensure_access_logs_table()
        except Exception as exc:  # noqa: BLE001
            # ฐานข้อมูลยังไม่พร้อมไม่ใช่เหตุให้ระบบล้ม thread จะลองเขียนและรายงาน error เอง
            self._last_error = f"สร้างตาราง access_logs ไม่สำเร็จ: {exc}"
            logger.error(self._last_error)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="access-logger", daemon=True)
        self._thread.start()
        logger.info(
            "เริ่มบันทึกประวัติเข้า-ออก (cooldown %.0f วินาที, คิวสูงสุด %d รายการ)",
            self._cooldown, self._queue.maxsize,
        )

    def stop(self) -> None:
        """หยุด โดยเขียนรายการที่ค้างในคิวให้เสร็จก่อน (รอไม่เกิน ~5 วินาที)"""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("หยุดบันทึกประวัติเข้า-ออก (เขียนแล้ว %d รายการ)", self._written)

    # ------------------------------------------------------------------
    # รับเหตุการณ์ (เรียกจาก event loop ต้องไม่บล็อก)
    # ------------------------------------------------------------------
    def submit(self, event: AccessEvent) -> bool:
        """ส่งเหตุการณ์เข้าคิว คืน True ถ้ารับไว้ / False ถ้าข้าม

        ข้ามเมื่อ: ปิดการบันทึกไว้, อยู่ในช่วง cooldown, หรือคิวเต็ม
        """
        if not self._enabled or self._thread is None:
            return False

        key = (event.student_id, event.direction)
        now = time.monotonic()
        last = self._last_logged.get(key)
        if last is not None and now - last < self._cooldown:
            with self._stats_lock:
                self._skipped_cooldown += 1
            return False

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._stats_lock:
                self._dropped_queue_full += 1
                self._last_error = "คิวบันทึกประวัติเต็ม (ฐานข้อมูลช้าหรือล่ม) ทิ้งรายการใหม่"
            logger.error(
                "คิวบันทึกประวัติเต็ม ทิ้งรายการของ %s (%s)", event.student_id, event.direction
            )
            return False

        # นับ cooldown ตั้งแต่ตอนรับเข้าคิว (ไม่รอเขียนสำเร็จ) กันส่งซ้ำระหว่างรอคิว
        self._last_logged[key] = now
        return True

    # ------------------------------------------------------------------
    # thread เขียนฐานข้อมูล
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            self._write(event)

    def _write(self, event: AccessEvent) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                log_id = self._database.insert_access_log(event.as_row())
            except Exception as exc:  # noqa: BLE001 - ต้องไม่ให้ thread ตาย
                last_exc = exc
                logger.warning(
                    "บันทึกประวัติไม่สำเร็จ (ครั้งที่ %d/%d): %s", attempt, MAX_ATTEMPTS, exc
                )
                if attempt < MAX_ATTEMPTS and not self._stop_event.wait(RETRY_DELAY):
                    continue
                break
            else:
                with self._stats_lock:
                    self._written += 1
                    self._last_written = {
                        "id": log_id,
                        "student_id": event.student_id,
                        "name": f"{event.first_name} {event.last_name}",
                        "direction": event.direction,
                        "camera_id": event.camera_id,
                    }
                logger.info(
                    "บันทึกประวัติ #%d: %s %s %s (%s) กล้อง %s",
                    log_id, event.student_id, event.first_name, event.last_name,
                    "เข้า" if event.direction == "IN" else "ออก", event.camera_id,
                )
                return

        with self._stats_lock:
            self._failed += 1
            self._last_error = f"เขียนฐานข้อมูลไม่สำเร็จ: {last_exc}"
        logger.error(
            "ทิ้งรายการของ %s (%s) หลังลอง %d ครั้งแล้วยังเขียนไม่ได้",
            event.student_id, event.direction, MAX_ATTEMPTS,
        )

    # ------------------------------------------------------------------
    # สถานะ (สำหรับ /api/health)
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._stats_lock:
            return {
                "enabled": self._enabled,
                "running": self._thread is not None and self._thread.is_alive(),
                "cooldown_seconds": self._cooldown,
                "queued": self._queue.qsize(),
                "written": self._written,
                "skipped_cooldown": self._skipped_cooldown,
                "dropped_queue_full": self._dropped_queue_full,
                "failed": self._failed,
                "last_written": self._last_written,
                "last_error": self._last_error,
            }
