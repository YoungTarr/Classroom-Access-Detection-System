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

from app.config import settings
from app.core.frame_source import Frame
from app.core.tracker import FaceTracker, Track
from app.face.detector import FaceDetector, face_detector
from app.identify.base import Identifier

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
    identify_ms: float = 0.0

    # จำนวนใบหน้าที่ส่งเข้าโมเดลจดจำในเฟรมนี้ (ไม่ใช่ทุกหน้าทุกเฟรม)
    identified_count: int = 0

    @property
    def total_ms(self) -> float:
        return self.detect_ms + self.track_ms + self.identify_ms


class Pipeline:
    """ประมวลผลเฟรมตั้งแต่ภาพดิบจนได้ผลที่พร้อมส่งให้หน้าเว็บ"""

    def __init__(
        self,
        detector: FaceDetector | None = None,
        identifier: Identifier | None = None,
    ) -> None:
        # ตัวตรวจจับใช้ร่วมกันได้ เพราะโมเดลไม่มีสถานะ (มีล็อกกันแย่ง CPU อยู่แล้ว)
        self.detector = detector or face_detector

        # ตัวระบุตัวตนก็ใช้ร่วมกันได้ เพราะคลังเวกเตอร์เป็นข้อมูลอ่านอย่างเดียว
        # (ส่วนที่เป็นสถานะคือ "ผลโหวต" ซึ่งเก็บอยู่ใน track ไม่ได้อยู่ในตัว identifier)
        self.identifier = identifier

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

        # ---- ขั้นที่ 2: ติดตามข้ามเฟรม ----
        # ต้องทำก่อนระบุตัวตน เพราะผลการระบุต้องผูกกับ track_id เพื่อเอาไปโหวต
        t1 = time.monotonic()
        tracks = self.tracker.update(faces, frame_width=width)
        track_ms = (time.monotonic() - t1) * 1000.0

        # ---- ขั้นที่ 3: ระบุตัวตน ----
        t2 = time.monotonic()
        identified = self._identify_tracks(frame.image, tracks, faces)
        identify_ms = (time.monotonic() - t2) * 1000.0

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
            identify_ms=identify_ms,
            identified_count=identified,
        )

    def _identify_tracks(
        self,
        image,
        tracks: list[Track],
        faces: list,
    ) -> int:
        """ระบุตัวตนให้ track ที่ถึงคิว แล้วคืนจำนวนที่ทำไปในเฟรมนี้

        ไม่ระบุทุก track ทุกเฟรม เพราะการสกัด embedding มีต้นทุน
        เลือกทำเฉพาะตัวที่ needs_identify และไม่เกินเพดานต่อเฟรม
        โดยให้ลำดับความสำคัญกับตัวที่ยังโหวตไม่ครบก่อน (เพื่อให้ชื่อขึ้นเร็ว)
        """
        if self.identifier is None or not faces:
            return 0

        cfg = settings.identify

        # เอาเฉพาะ track ที่เห็นอยู่จริงในเฟรมนี้
        # ตัวที่กำลังค้างกรอบไว้ไม่มีภาพใหม่ให้ตรวจ จึงข้ามไป
        candidates = [t for t in tracks if t.is_visible and t.needs_identify]

        if not candidates:
            return 0

        # เรียงลำดับ: ตัวที่มีผลโหวตน้อยสุดได้คิวก่อน
        # (คนที่เพิ่งเดินเข้ามาจะได้เห็นชื่อเร็ว ไม่ต้องรอคิวคนที่ระบบรู้จักแล้ว)
        candidates.sort(key=lambda t: len(t.identity_votes))

        # จับคู่ track กลับไปหา DetectedFace เพื่อเอาจุดสังเกตมาใช้จัดหน้าให้ตรง
        # (Track เก็บแค่กรอบ ไม่ได้เก็บจุดสังเกตไว้)
        face_by_box = {(f.x, f.y, f.w, f.h): f for f in faces}

        count = 0
        for track in candidates[: cfg.max_faces_per_frame]:
            face = face_by_box.get(track.bbox)
            if face is None:
                # ไม่ควรเกิด เพราะ track ที่ visible เพิ่งถูกอัปเดตจาก face ในเฟรมนี้
                logger.debug("track %d หา DetectedFace ที่ตรงกันไม่เจอ", track.track_id)
                continue

            result = self.identifier.identify(image, face)
            track.record_identity(result)
            count += 1

        return count

    def reset(self) -> None:
        """ล้างสถานะการติดตาม (ใช้เมื่อสลับกล้องหรือเริ่มการเชื่อมต่อใหม่)"""
        self.tracker.reset()

    @property
    def processed_frames(self) -> int:
        return self._processed_frames


def create_pipeline(identifier: Identifier | None = None) -> Pipeline:
    """สร้าง pipeline ใหม่หนึ่งตัวสำหรับหนึ่งแหล่งภาพ"""
    return Pipeline(identifier=identifier)
