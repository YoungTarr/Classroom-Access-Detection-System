"""จุดสลับ implementation ของการระบุตัวตน

ที่นี่เป็น "สวิตช์" เดียวของระบบ: ส่วนอื่นเรียก create_identifier() แล้วได้ตัวที่ตั้งไว้
โดยไม่ต้องรู้ว่าเบื้องหลังใช้ใบหน้า บัตร RFID หรืออย่างอื่น

กฎสำคัญ: **ห้ามมี fallback เงียบ ๆ**
ถ้าสร้างตัวที่ขอมาไม่ได้ ต้องโยน error ที่บอกสาเหตุชัดเจน
ห้ามแอบคืนตัวอื่นมาให้แทน เพราะผู้ใช้จะงงว่าทำไมตั้งค่าไว้อย่างหนึ่ง
แต่ระบบทำงานอีกอย่างหนึ่ง โดยไม่มีอะไรบอกเลย
"""

from __future__ import annotations

import logging

from app.config import settings
from app.identify.base import (
    STATUS_RECOGNIZED,
    STATUS_UNKNOWN,
    Identifier,
    IdentityResult,
    IndexBuildReport,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Identifier",
    "IdentityResult",
    "IndexBuildReport",
    "STATUS_RECOGNIZED",
    "STATUS_UNKNOWN",
    "create_identifier",
]


class IdentifierCreationError(RuntimeError):
    """สร้างตัวระบุตัวตนไม่สำเร็จ - ต้องหยุดและรายงาน ไม่ใช้ตัวอื่นแทนเงียบ ๆ"""


def create_identifier(mode: str | None = None) -> Identifier:
    """สร้างตัวระบุตัวตนตามค่าที่ตั้งไว้ใน environment IDENTIFIER

    mode=None หมายถึงใช้ค่าจาก config (ปกติจะเป็นแบบนี้)
    การส่ง mode เข้ามาตรง ๆ มีไว้สำหรับการทดสอบเท่านั้น
    """
    selected = (mode or settings.identify.mode).lower()

    if selected == "face":
        try:
            # import ตรงนี้ไม่ใช่บนหัวไฟล์ เพราะ face_identifier ดึง insightface
            # และ faiss เข้ามาด้วย ซึ่งใช้เวลา import นานพอสมควร
            from app.identify.face_identifier import FaceIdentifier

            identifier = FaceIdentifier()
        except Exception as exc:  # noqa: BLE001
            raise IdentifierCreationError(
                f"สร้างตัวระบุตัวตนแบบ 'face' ไม่สำเร็จ: {exc}"
            ) from exc

        logger.info("ใช้วิธีระบุตัวตน: %s", identifier.name)
        return identifier

    # ไม่รู้จักชื่อที่ขอมา - ต้องฟ้อง ไม่ใช่คืน face ให้เงียบ ๆ
    raise IdentifierCreationError(
        f"ไม่รู้จักวิธีระบุตัวตนชื่อ {selected!r} "
        f"(ตอนนี้รองรับเฉพาะ 'face' - ตรวจค่า IDENTIFIER ในไฟล์ .env)"
    )
