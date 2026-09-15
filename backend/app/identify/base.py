"""สัญญา (interface) ของตัวระบุตัวตน

จุดประสงค์ของไฟล์นี้คือแยก "วิธีระบุตัวตน" ออกจาก "ส่วนที่เรียกใช้"
ตอนนี้มีวิธีเดียวคือดูจากใบหน้า แต่ในอนาคตอาจเพิ่มบัตร RFID หรือ QR code
ซึ่งจะมาเสียบเพิ่มได้โดยไม่ต้องแก้ pipeline เลย

กฎเหล็กสองข้อของไฟล์นี้:

1. **identify() ต้องไม่โยน exception ออกมาไม่ว่ากรณีใด**
   เพราะมันถูกเรียกทุกเฟรมระหว่างสตรีมสด ถ้าโยน exception ออกมา
   สตรีมจะขาดทันทีเพราะใบหน้าใบเดียวที่มีปัญหา
   ทุกความผิดพลาดต้องแปลงเป็น IdentityResult.unknown() พร้อมบอกสาเหตุ

2. **ผลที่ระบุไม่ได้ ต้องบอกเหตุผลเสมอ**
   จึงออกแบบให้ unknown() บังคับให้ส่ง detail เข้ามา (เป็น argument ที่ไม่มีค่า default)
   ผู้เขียนโค้ดจะ "ลืม" ใส่เหตุผลไม่ได้ เพราะ Python จะฟ้องตั้งแต่ตอนเรียก
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.face.detector import DetectedFace

# สถานะที่เป็นไปได้ของผลการระบุตัวตน
STATUS_RECOGNIZED = "recognized"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class IdentityResult:
    """ผลการระบุตัวตนของใบหน้าหนึ่งใบ"""

    # ชื่อวิธีที่ให้ผลนี้ เช่น "face" - มีไว้เพื่อให้ debug ได้ว่าใครเป็นคนตอบ
    identified_by: str

    # "recognized" = รู้ว่าเป็นใคร / "unknown" = ไม่รู้ (ต้องมี detail บอกเหตุผลเสมอ)
    status: str

    # ข้อมูลของคนที่ระบุได้ - ต้องมาจากฐานข้อมูลเท่านั้น ห้าม hardcode ในโค้ด
    student_id: str | None

    first_name: str | None
    last_name: str | None

    # คะแนนความคล้าย (cosine similarity 0.0-1.0) ยิ่งสูงยิ่งมั่นใจ
    confidence: float

    # คำอธิบายที่มนุษย์อ่านรู้เรื่อง ใช้ทั้งตอน debug และแสดงใน log
    # กรณี unknown ต้องบอกให้ได้ว่า "ทำไมถึงไม่รู้"
    detail: str

    @property
    def is_recognized(self) -> bool:
        return self.status == STATUS_RECOGNIZED

    @property
    def full_name(self) -> str | None:
        """ชื่อเต็มสำหรับแสดงผล คืน None ถ้าไม่รู้ว่าเป็นใคร"""
        if not self.is_recognized:
            return None
        return f"{self.first_name} {self.last_name}".strip()

    @classmethod
    def recognized(
        cls,
        identified_by: str,
        student_id: str,
        first_name: str,
        last_name: str,
        confidence: float,
        detail: str,
    ) -> IdentityResult:
        """สร้างผลแบบ 'รู้ว่าเป็นใคร'"""
        return cls(
            identified_by=identified_by,
            status=STATUS_RECOGNIZED,
            student_id=student_id,
            first_name=first_name,
            last_name=last_name,
            confidence=confidence,
            detail=detail,
        )

    @classmethod
    def unknown(
        cls,
        identified_by: str,
        detail: str,
        confidence: float = 0.0,
    ) -> IdentityResult:
        """สร้างผลแบบ 'ไม่รู้ว่าเป็นใคร'

        detail เป็น argument บังคับ (ไม่มีค่า default) โดยตั้งใจ
        เพื่อไม่ให้มีผล unknown ที่ลอยมาโดยไม่มีเหตุผลกำกับ
        ตัวอย่างเหตุผลที่ควรใส่:
            "คะแนนความคล้ายสูงสุด 0.21 ต่ำกว่าเกณฑ์ 0.35"
            "ยังไม่มีเวกเตอร์ใบหน้าในระบบเลย"
            "ครอปใบหน้าไม่สำเร็จ: กรอบอยู่นอกภาพ"
        """
        if not detail or not detail.strip():
            raise ValueError("IdentityResult.unknown() ต้องระบุ detail เสมอ ห้ามส่งค่าว่าง")

        return cls(
            identified_by=identified_by,
            status=STATUS_UNKNOWN,
            student_id=None,
            first_name=None,
            last_name=None,
            confidence=confidence,
            detail=detail,
        )

    def as_dict(self) -> dict[str, Any]:
        """แปลงเป็น JSON ที่ส่งให้เบราว์เซอร์"""
        return {
            "identified_by": self.identified_by,
            "status": self.status,
            "student_id": self.student_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "confidence": round(float(self.confidence), 4),
            "detail": self.detail,
        }


@dataclass
class IndexBuildReport:
    """สรุปผลการสร้างคลังเวกเตอร์ใบหน้า

    ใช้ทั้งตอนสตาร์ทระบบและตอนเรียก /api/faces/reload
    ต้องบอกให้ครบว่าอะไรสำเร็จ อะไรไม่สำเร็จ และไม่สำเร็จเพราะอะไร
    """

    member_count: int = 0          # จำนวนสมาชิกทั้งหมดในฐานข้อมูล
    enrolled_count: int = 0        # จำนวนสมาชิกที่ได้เวกเตอร์อย่างน้อยหนึ่งตัว
    vector_count: int = 0          # จำนวนเวกเตอร์ทั้งหมดในคลัง
    build_ms: float = 0.0

    # ปัญหาที่พบรายไฟล์ เช่น "66200407/left.jpg: ไม่พบไฟล์"
    # ต้องบอกให้ได้ว่าไฟล์ไหนของรหัสอะไร ไม่ใช่แค่ "มีบางไฟล์ใช้ไม่ได้"
    problems: list[str] = None  # type: ignore[assignment]

    # สมาชิกที่ไม่ได้เวกเตอร์เลยสักตัว (ระบบจะจำคนเหล่านี้ไม่ได้)
    members_without_vectors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.problems is None:
            self.problems = []
        if self.members_without_vectors is None:
            self.members_without_vectors = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "member_count": self.member_count,
            "enrolled_count": self.enrolled_count,
            "vector_count": self.vector_count,
            "build_ms": round(self.build_ms, 1),
            "problem_count": len(self.problems),
            "problems": self.problems,
            "members_without_vectors": self.members_without_vectors,
        }


class Identifier(ABC):
    """สัญญาที่ทุกวิธีระบุตัวตนต้องทำตาม"""

    #: ชื่อวิธี ใช้เป็นค่า identified_by และใช้เลือกผ่าน env IDENTIFIER
    name: str = "unknown"

    @abstractmethod
    def identify(self, image: np.ndarray, face: DetectedFace) -> IdentityResult:
        """ระบุว่าใบหน้าในกรอบนี้เป็นใคร

        ห้ามโยน exception ออกมาไม่ว่ากรณีใด - ทุกความผิดพลาดต้องแปลงเป็น
        IdentityResult.unknown() พร้อมเหตุผล
        """

    @abstractmethod
    def reload(self) -> IndexBuildReport:
        """สร้างคลังข้อมูลใหม่จากต้นทาง (ฐานข้อมูล + ไฟล์รูป)

        เมธอดนี้โยน exception ได้ เพราะผู้เรียกคือ endpoint ที่รอผลอยู่
        และควรเห็นสาเหตุเต็ม ๆ ต่างจาก identify() ที่อยู่ในสตรีมสด
        """

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """ข้อมูลสรุปสำหรับหน้า /api/health"""
