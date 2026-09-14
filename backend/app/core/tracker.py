"""ติดตามใบหน้าข้ามเฟรม (tracking)

ปัญหาที่ไฟล์นี้แก้: ตัวตรวจจับ (SCRFD) ทำงานทีละเฟรมแบบไม่มีความจำ
เฟรมนี้เจอ 2 หน้า เฟรมหน้าก็เจอ 2 หน้า แต่มันไม่รู้ว่าหน้าไหนคือหน้าเดิม
ผลคือถ้าเอาผลดิบไปใช้ตรง ๆ จะเกิดปัญหา 3 อย่าง

    1. กรอบกะพริบ - ตัวตรวจจับพลาดไปเฟรมเดียว (หันหน้า เอามือบัง แสงเปลี่ยน)
       กรอบจะหายวับแล้วโผล่กลับมา
    2. เฟส 4 โหวตผลไม่ได้ - ถ้าไม่รู้ว่าเป็นคนเดิม ก็เก็บผลย้อนหลังมาโหวตไม่ได้
    3. เฟส 5 คำนวณทิศทางไม่ได้ - ต้องรู้ว่า "คนเดิม" เคลื่อนจากตรงไหนไปตรงไหน

วิธีแก้: ให้แต่ละใบหน้ามี track_id ที่คงที่ตลอดเวลาที่ยังอยู่ในภาพ
โดยจับคู่ใบหน้าที่เจอในเฟรมใหม่เข้ากับ track เดิมด้วย IoU (พื้นที่ทับซ้อน)
และถ้าหายไปไม่กี่เฟรมให้ค้าง track ไว้ก่อน ยังไม่ลบทิ้งทันที
"""

from __future__ import annotations

import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from app.config import settings
from app.face.detector import DetectedFace

logger = logging.getLogger(__name__)

# ชนิดของกรอบที่ใช้ภายในไฟล์นี้: (x, y, w, h)
BBox = tuple[int, int, int, int]


def iou(a: BBox, b: BBox) -> float:
    """คำนวณ Intersection over Union ของกรอบสองอัน

    IoU = พื้นที่ที่ทับกัน / พื้นที่รวมของทั้งสองกรอบ
    ได้ค่า 0.0 (ไม่ทับกันเลย) ถึง 1.0 (ทับกันสนิท)

    ใช้ค่านี้เป็นตัวชี้วัดว่า "น่าจะเป็นใบหน้าเดียวกันไหม" เพราะคนเราขยับ
    ระหว่างสองเฟรมที่ห่างกันแค่ ~100 ms ได้ไม่มาก กรอบจึงควรทับกันเป็นส่วนใหญ่
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b

    # หาขอบของพื้นที่ที่ทับกัน
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)

    inter_w = right - left
    inter_h = bottom - top
    if inter_w <= 0 or inter_h <= 0:
        return 0.0

    intersection = inter_w * inter_h
    union = (aw * ah) + (bw * bh) - intersection
    if union <= 0:
        return 0.0

    return intersection / union


def center_of(box: BBox) -> tuple[float, float]:
    """จุดกึ่งกลางของกรอบ"""
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


def center_distance(a: BBox, b: BBox) -> float:
    """ระยะห่างระหว่างจุดกึ่งกลางของกรอบสองอัน"""
    ax, ay = center_of(a)
    bx, by = center_of(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


@dataclass
class Track:
    """ใบหน้าหนึ่งใบที่ระบบกำลังติดตามอยู่"""

    track_id: int
    bbox: BBox
    score: float

    # จำนวนเฟรมที่เจอใบหน้านี้ทั้งหมด (ยิ่งมากยิ่งน่าเชื่อถือ ไม่ใช่สัญญาณรบกวน)
    hits: int = 1

    # จำนวนเฟรม "ติดต่อกัน" ล่าสุดที่หาไม่เจอ
    # 0 = เจอในเฟรมล่าสุด, >0 = กำลังค้างกรอบไว้รอดูอีกสักครู่
    missing: int = 0

    created_at: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.monotonic)

    # ประวัติจุดกึ่งกลางย้อนหลัง (เฟส 5 จะใช้คำนวณทิศทางการเคลื่อนที่)
    history: deque = field(default_factory=lambda: deque(maxlen=15))

    @property
    def is_visible(self) -> bool:
        """เจอในเฟรมล่าสุดหรือไม่ (ไม่ใช่กรอบที่ค้างไว้)"""
        return self.missing == 0

    def update(self, face: DetectedFace) -> None:
        """อัปเดตด้วยผลตรวจจับใหม่ที่จับคู่กันได้"""
        self.bbox = (face.x, face.y, face.w, face.h)
        self.score = face.score
        self.hits += 1
        self.missing = 0
        self.last_seen_at = time.monotonic()
        self.history.append(center_of(self.bbox))

    def mark_missing(self) -> None:
        """เฟรมนี้หาไม่เจอ - ยังไม่ลบ แค่นับไว้"""
        self.missing += 1

    def as_dict(self) -> dict:
        """แปลงเป็นรูปแบบที่ส่งกลับไปให้เบราว์เซอร์"""
        x, y, w, h = self.bbox
        return {
            "track_id": self.track_id,
            "bbox": [x, y, w, h],
            "score": round(float(self.score), 4),
            # หน้าเว็บใช้ 2 ค่านี้ตัดสินใจว่าจะวาดกรอบแบบทึบหรือแบบจาง
            "visible": self.is_visible,
            "missing": self.missing,
            "hits": self.hits,
        }


class FaceTracker:
    """จับคู่ใบหน้าข้ามเฟรมเพื่อให้ track_id คงที่

    หมายเหตุเรื่องการใช้งาน: ตัวติดตามมี "สถานะ" อยู่ข้างใน
    จึงต้องสร้างแยกกันต่อหนึ่งแหล่งภาพ (หนึ่ง WebSocket / หนึ่งกล้อง)
    ถ้าใช้ตัวเดียวร่วมกันหลายแหล่ง ใบหน้าจากคนละกล้องจะถูกจับคู่กันมั่ว
    """

    def __init__(self) -> None:
        cfg = settings.tracking

        self.iou_threshold = cfg.iou_threshold
        self.max_center_distance_ratio = cfg.max_center_distance_ratio
        self.max_missing = cfg.max_missing
        self.history_size = cfg.history_size

        self._tracks: dict[int, Track] = {}

        # ตัวนับ id ที่ไม่ซ้ำ เริ่มที่ 1 เพื่อให้ id 0 ไม่ถูกเข้าใจผิดว่าเป็นค่าว่าง
        self._id_counter = itertools.count(1)

    # ------------------------------------------------------------------
    # การอัปเดตหลัก
    # ------------------------------------------------------------------
    def update(self, faces: list[DetectedFace], frame_width: int) -> list[Track]:
        """รับผลตรวจจับของเฟรมหนึ่ง แล้วคืนรายการ track ที่ควรแสดงผล

        รายการที่คืนมารวม track ที่ "หายชั่วคราว" ด้วย (missing > 0)
        เพื่อให้หน้าเว็บค้างกรอบไว้ได้ ไม่กะพริบ
        """
        matches, unmatched_faces, unmatched_tracks = self._match(faces, frame_width)

        # 1) track ที่จับคู่ได้ -> อัปเดตตำแหน่งใหม่
        for track_id, face in matches.items():
            self._tracks[track_id].update(face)

        # 2) ใบหน้าที่จับคู่ไม่ได้ -> เป็นคนใหม่ที่เพิ่งเข้ามาในภาพ
        for face in unmatched_faces:
            self._create_track(face)

        # 3) track ที่ไม่มีใบหน้ามาจับคู่ -> นับว่าหายไปหนึ่งเฟรม
        #    ถ้าหายเกินที่กำหนดค่อยลบทิ้ง (นี่คือส่วนที่กันกรอบกะพริบ)
        for track_id in unmatched_tracks:
            track = self._tracks[track_id]
            track.mark_missing()
            if track.missing > self.max_missing:
                del self._tracks[track_id]
                logger.debug("ลบ track %d (หายไป %d เฟรม)", track_id, track.missing)

        # เรียงตาม track_id เพื่อให้ลำดับที่ส่งไปคงที่ ไม่สลับไปมาทุกเฟรม
        return [self._tracks[tid] for tid in sorted(self._tracks)]

    def reset(self) -> None:
        """ล้าง track ทั้งหมด (ใช้เมื่อเปลี่ยนกล้องหรือเริ่มการเชื่อมต่อใหม่)"""
        self._tracks.clear()

    @property
    def active_count(self) -> int:
        """จำนวน track ที่เห็นอยู่จริงในเฟรมล่าสุด"""
        return sum(1 for t in self._tracks.values() if t.is_visible)

    # ------------------------------------------------------------------
    # ภายใน
    # ------------------------------------------------------------------
    def _create_track(self, face: DetectedFace) -> Track:
        track_id = next(self._id_counter)
        bbox = (face.x, face.y, face.w, face.h)

        track = Track(
            track_id=track_id,
            bbox=bbox,
            score=face.score,
            history=deque(maxlen=self.history_size),
        )
        track.history.append(center_of(bbox))

        self._tracks[track_id] = track
        logger.debug("สร้าง track ใหม่ %d ที่ตำแหน่ง %s", track_id, bbox)
        return track

    def _match(
        self,
        faces: list[DetectedFace],
        frame_width: int,
    ) -> tuple[dict[int, DetectedFace], list[DetectedFace], list[int]]:
        """จับคู่ใบหน้าที่เพิ่งตรวจเจอ เข้ากับ track ที่มีอยู่

        ใช้วิธี greedy: เรียงคู่ทั้งหมดตามคะแนนความเข้ากันจากมากไปน้อย
        แล้วหยิบคู่ที่ดีที่สุดก่อน ตัดตัวที่ถูกจองแล้วออก ทำซ้ำจนหมด

        เหตุผลที่ไม่ใช้วิธีจับคู่แบบ optimal (Hungarian algorithm):
        จำนวนใบหน้าในห้องเรียนต่อเฟรมมีไม่มาก (หลักหน่วยถึงสิบต้น ๆ)
        greedy ให้ผลเหมือนกันในทางปฏิบัติ แต่โค้ดสั้นกว่าและไม่ต้องพึ่ง scipy
        """
        if not self._tracks:
            return {}, list(faces), []

        if not faces:
            return {}, [], list(self._tracks.keys())

        max_distance = frame_width * self.max_center_distance_ratio

        # สร้างรายการคู่ที่เป็นไปได้ทั้งหมดพร้อมคะแนน
        candidates: list[tuple[float, int, int]] = []  # (คะแนน, track_id, ลำดับใบหน้า)

        for track_id, track in self._tracks.items():
            for face_index, face in enumerate(faces):
                face_box: BBox = (face.x, face.y, face.w, face.h)
                overlap = iou(track.bbox, face_box)

                if overlap >= self.iou_threshold:
                    # ทับกันพอ = มั่นใจ ให้คะแนนสูงไว้ก่อน (บวก 1 เพื่อให้ชนะกลุ่มระยะทางเสมอ)
                    candidates.append((1.0 + overlap, track_id, face_index))
                    continue

                # ทับกันไม่พอ แต่ยังอาจเป็นคนเดิมที่ขยับเร็ว หรือกรอบขนาดเปลี่ยนมาก
                # (เช่นเดินเข้าใกล้กล้อง) จึงลองวัดจากระยะจุดกึ่งกลางเป็นทางสำรอง
                distance = center_distance(track.bbox, face_box)
                if distance <= max_distance:
                    # ใกล้กว่า = คะแนนสูงกว่า แปลงให้อยู่ในช่วง 0-1
                    candidates.append((1.0 - (distance / max_distance), track_id, face_index))

        # เรียงจากคู่ที่มั่นใจที่สุดลงมา
        candidates.sort(reverse=True)

        matches: dict[int, DetectedFace] = {}
        used_tracks: set[int] = set()
        used_faces: set[int] = set()

        for _score, track_id, face_index in candidates:
            if track_id in used_tracks or face_index in used_faces:
                continue
            matches[track_id] = faces[face_index]
            used_tracks.add(track_id)
            used_faces.add(face_index)

        unmatched_faces = [f for i, f in enumerate(faces) if i not in used_faces]
        unmatched_tracks = [tid for tid in self._tracks if tid not in used_tracks]

        return matches, unmatched_faces, unmatched_tracks


def tracks_to_dicts(tracks: Iterable[Track]) -> list[dict]:
    """แปลงรายการ track เป็น JSON ที่ส่งให้เบราว์เซอร์"""
    return [track.as_dict() for track in tracks]
