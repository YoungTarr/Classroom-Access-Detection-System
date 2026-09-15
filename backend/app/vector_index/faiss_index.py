"""คลังเวกเตอร์ใบหน้า บน FAISS

FAISS คือไลบรารีค้นหาเวกเตอร์ที่ใกล้เคียงที่สุด เราใช้ IndexFlatIP
    Flat = เทียบกับทุกเวกเตอร์ในคลังแบบตรงไปตรงมา ไม่มีการประมาณ
    IP   = Inner Product (dot product)

ทำไม Flat ไม่ใช่ index แบบประมาณ (IVF/HNSW):
    index แบบประมาณมีไว้สำหรับข้อมูลระดับล้านเวกเตอร์ขึ้นไป
    ของเรามีนักศึกษาหลักสิบถึงหลักร้อย คูณ 3 มุมต่อคน = ไม่กี่ร้อยเวกเตอร์
    การเทียบทุกตัวเร็วกว่ามาก (ไม่ถึงมิลลิวินาที) และได้ผลที่ถูกต้อง 100%
    ไม่มีโอกาส "พลาดคนที่ใช่" แบบ index ประมาณ

ทำไม Inner Product ถึงเท่ากับ cosine similarity:
    เพราะเวกเตอร์ทุกตัวถูก normalize ให้ยาว 1 มาแล้วจาก face/recognizer.py
    dot product ของเวกเตอร์ยาว 1 สองตัว = cos ของมุมระหว่างกันพอดี

หนึ่งคนมีหลายเวกเตอร์ (ซ้าย/หน้า/ขวา) ตอนค้นจึงต้องจัดกลุ่มตามรหัสนักศึกษา
แล้วยึด "คะแนนสูงสุด" ของคนนั้น ไม่ใช่เอาคะแนนเฉลี่ย
เพราะถ้าเห็นหน้าตรง ควรแมตช์กับรูปหน้าตรงได้เต็ม ๆ โดยไม่ถูกรูปด้านข้างถ่วงคะแนนลง
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    """ผลการค้นหาหนึ่งรายการ (รวมคะแนนสูงสุดของคนคนนั้นแล้ว)"""

    student_id: str
    score: float

    # ป้ายกำกับของเวกเตอร์ที่ให้คะแนนสูงสุด เช่น "66200407/front.jpg"
    # มีไว้เพื่อ debug ว่าแมตช์กับรูปมุมไหน
    source: str


class FaceIndex:
    """คลังเวกเตอร์ใบหน้าของสมาชิกทั้งหมด"""

    def __init__(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError(f"มิติของเวกเตอร์ต้องมากกว่า 0 แต่ได้รับ {dimension}")

        self.dimension = dimension
        self._index = None

        # ข้อมูลกำกับของเวกเตอร์ลำดับที่ i อยู่ที่ _labels[i]
        # FAISS เก็บแค่ตัวเลข ไม่เก็บว่าเวกเตอร์ไหนเป็นของใคร เราจึงต้องเก็บเอง
        self._labels: list[tuple[str, str]] = []  # (student_id, source)

        self._build()

    def _build(self) -> None:
        import faiss

        self._index = faiss.IndexFlatIP(self.dimension)
        self._labels = []

    # ------------------------------------------------------------------
    # เพิ่มข้อมูล
    # ------------------------------------------------------------------
    def add(self, vector: np.ndarray, student_id: str, source: str) -> None:
        """เพิ่มเวกเตอร์หนึ่งตัวพร้อมข้อมูลกำกับ

        เวกเตอร์ต้อง normalize มาแล้ว (ความยาว 1) ไม่งั้นคะแนนที่ได้
        จะไม่ใช่ cosine similarity และเกณฑ์ที่ตั้งไว้จะไม่มีความหมาย
        """
        vec = np.asarray(vector, dtype=np.float32).reshape(1, -1)

        if vec.shape[1] != self.dimension:
            raise ValueError(
                f"เวกเตอร์มี {vec.shape[1]} มิติ แต่คลังนี้รับ {self.dimension} มิติ "
                f"(ของ {student_id} จาก {source})"
            )

        # ตรวจว่า normalize มาจริงไหม เพื่อจับบั๊กตั้งแต่ต้นทาง
        norm = float(np.linalg.norm(vec))
        if abs(norm - 1.0) > 0.01:
            raise ValueError(
                f"เวกเตอร์ยังไม่ถูก normalize (ความยาว {norm:.3f} ควรเป็น 1.0) "
                f"ของ {student_id} จาก {source}"
            )

        self._index.add(vec)
        self._labels.append((student_id, source))

    # ------------------------------------------------------------------
    # ค้นหา
    # ------------------------------------------------------------------
    def search(self, vector: np.ndarray, top_k: int = 5) -> list[SearchHit]:
        """ค้นหาคนที่ใบหน้าคล้ายที่สุด เรียงจากคะแนนสูงไปต่ำ

        คืนลิสต์ว่างถ้าคลังยังไม่มีเวกเตอร์เลย - ผู้เรียกต้องจัดการกรณีนี้
        โดยรายงานว่า "ยังไม่มีข้อมูลใบหน้าในระบบ" ไม่ใช่บอกว่า "ไม่รู้จักคนนี้"
        เพราะสองอย่างนี้คนละสาเหตุกันและแก้คนละวิธี
        """
        if self.is_empty:
            return []

        vec = np.asarray(vector, dtype=np.float32).reshape(1, -1)

        # ค้นเผื่อไว้มากกว่าที่ต้องการ เพราะเวกเตอร์หลายตัวอาจเป็นของคนเดียวกัน
        # (คนหนึ่งมี 3 มุม) ถ้าค้นแค่ top_k อาจได้คนเดียวซ้ำ 3 ครั้ง
        # แล้วไม่เห็นคนอันดับรองเลย ซึ่งจำเป็นตอนดูว่าคะแนนห่างกันพอไหม
        search_k = min(self.size, max(top_k * 3, 9))

        scores, indices = self._index.search(vec, search_k)

        # จัดกลุ่มตามรหัสนักศึกษา แล้วยึดคะแนนสูงสุดของแต่ละคน
        best: dict[str, tuple[float, str]] = {}

        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:  # FAISS ใส่ -1 เมื่อผลมีไม่ครบตามที่ขอ
                continue
            student_id, source = self._labels[idx]
            current = best.get(student_id)
            if current is None or score > current[0]:
                best[student_id] = (float(score), source)

        hits = [
            SearchHit(student_id=sid, score=score, source=source)
            for sid, (score, source) in best.items()
        ]
        hits.sort(key=lambda h: h.score, reverse=True)

        return hits[:top_k]

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        """จำนวนเวกเตอร์ทั้งหมดในคลัง"""
        return int(self._index.ntotal) if self._index is not None else 0

    @property
    def is_empty(self) -> bool:
        return self.size == 0

    @property
    def student_ids(self) -> set[str]:
        """รหัสนักศึกษาทั้งหมดที่มีเวกเตอร์อยู่ในคลัง"""
        return {student_id for student_id, _ in self._labels}

    def vector_count_of(self, student_id: str) -> int:
        """จำนวนเวกเตอร์ของคนคนหนึ่ง (ปกติ 3 ถ้ามีรูปครบทุกมุม)"""
        return sum(1 for sid, _ in self._labels if sid == student_id)
