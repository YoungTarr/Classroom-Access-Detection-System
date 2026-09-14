"""สายการประมวลผลหนึ่งเฟรม (pipeline)

ลำดับการทำงานเต็มรูปแบบตามที่วางไว้:

    detect  ->  identify  ->  track  ->  direction
    (เฟส 2)     (เฟส 4)       (เฟส 3)     (เฟส 5)

ตอนนี้ทำถึงขั้น track แล้ว ส่วน identify กับ direction จะมาเสียบเพิ่มในเฟสถัดไป
โดยไม่ต้องรื้อโครงสร้างนี้

สำคัญ: Pipeline มี "สถานะ" อยู่ข้างใน (ตัวติดตามจำ track ไว้)
จึงต้องสร้างแยกกันหนึ่งตัวต่อหนึ่งแหล่งภาพ เช่น
    - หนึ่งการเชื่อมต่อ WebSocket จากเบราว์เซอร์ (เฟส 2-5)
    - หนึ่งกล้อง IP (เฟส 6 ที่จะมีสองกล้อง)
ถ้าใช้ตัวเดียวร่วมกัน ใบหน้าจากคนละแหล่งจะถูกจับคู่กันมั่ว
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.core.frame_source import Frame
from app.core.tracker import FaceTracker, Track
from app.face.detector import FaceDetector, face_detector

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """ผลลัพธ์ของการประมวลผลหนึ่งเฟรม"""

    frame_id: int

    # track ที่ควรแสดงผล (รวมตัวที่หายชั่วคราวด้วย เพื่อให้หน้าเว็บค้างกรอบไว้ได้)
    tracks: list[Track]

    # ขนาดของภาพที่ประมวลผล (กว้าง, สูง) - หน้าเว็บใช้แปลงพิกัด
    source_size: tuple[int, int]

    # จำนวนใบหน้าที่ "ตรวจเจอจริง" ในเฟรมนี้ (ไม่รวมกรอบที่ค้างไว้)
    # แยกจากจำนวน track เพื่อให้ดูออกว่าตัวตรวจจับพลาดไปกี่ใบ
    detected_count: int

    # เวลาที่ใช้แต่ละขั้น (มิลลิวินาที) สำหรับแสดงบนหน้าเว็บและใช้จูนประสิทธิภาพ
    detect_ms: float
    track_ms: float

    @property
    def total_ms(self) -> float:
        return self.detect_ms + self.track_ms


class Pipeline:
    """ประมวลผลเฟรมตั้งแต่ภาพดิบจนได้ผลที่พร้อมส่งให้หน้าเว็บ"""

    def __init__(self, detector: FaceDetector | None = None) -> None:
        # ตัวตรวจจับใช้ร่วมกันได้ เพราะโมเดลไม่มีสถานะ (มีล็อกกันแย่ง CPU อยู่แล้ว)
        self.detector = detector or face_detector

        # ส่วนตัวติดตามต้องเป็นของใครของมัน เพราะจำ track ไว้ข้างใน
        self.tracker = FaceTracker()

        self._processed_frames = 0

    def process(self, frame: Frame) -> PipelineResult:
        """ประมวลผลหนึ่งเฟรม

        เมธอดนี้เป็นงาน blocking ที่ใช้เวลานาน (หลักสิบถึงร้อย ms)
        ผู้เรียกที่เป็น async ต้องเรียกผ่าน thread แยกเสมอ
        """
        width, height = frame.size

        # ---- ขั้นที่ 1: ตรวจจับใบหน้า ----
        t0 = time.monotonic()
        faces = self.detector.detect(frame.image)
        detect_ms = (time.monotonic() - t0) * 1000.0

        # ---- ขั้นที่ 2: (เฟส 4) ระบุตัวตน ----
        # จะมาเสียบตรงนี้ โดยจะไม่เรียกทุกเฟรมทุกหน้า แต่เรียกเท่าที่จำเป็น
        # แล้วเก็บผลผูกกับ track_id เพื่อโหวตย้อนหลัง

        # ---- ขั้นที่ 3: ติดตามข้ามเฟรม ----
        t1 = time.monotonic()
        tracks = self.tracker.update(faces, frame_width=width)
        track_ms = (time.monotonic() - t1) * 1000.0

        # ---- ขั้นที่ 4: (เฟส 5) คำนวณทิศทางการเคลื่อนที่ ----
        # จะอ่านจาก track.history ที่ตัวติดตามเก็บไว้ให้แล้ว

        self._processed_frames += 1

        return PipelineResult(
            frame_id=frame.frame_id,
            tracks=tracks,
            source_size=(width, height),
            detected_count=len(faces),
            detect_ms=detect_ms,
            track_ms=track_ms,
        )

    def reset(self) -> None:
        """ล้างสถานะการติดตาม (ใช้เมื่อสลับกล้องหรือเริ่มการเชื่อมต่อใหม่)"""
        self.tracker.reset()

    @property
    def processed_frames(self) -> int:
        return self._processed_frames


def create_pipeline() -> Pipeline:
    """สร้าง pipeline ใหม่หนึ่งตัวสำหรับหนึ่งแหล่งภาพ"""
    return Pipeline()
