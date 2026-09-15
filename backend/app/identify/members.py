"""ทะเบียนสมาชิก - แหล่งความจริงเรื่อง "ใครเป็นใคร"

กฎเหล็กของโปรเจกต์: ชื่อ-นามสกุลที่แสดงบนหน้าจอต้องมาจากฐานข้อมูลเสมอ
ห้าม hardcode ในโค้ดเด็ดขาด เพราะ
    - เพิ่ม/ลบ/แก้ชื่อสมาชิกต้องทำได้โดยไม่ต้องแก้โค้ดและ deploy ใหม่
    - ชื่อที่อยู่ในโค้ดจะไม่ตรงกับฐานข้อมูลในที่สุด แล้วไม่มีใครรู้ว่าอันไหนถูก

ไฟล์นี้ยังรับผิดชอบเรื่องการแปลง path ของรูปด้วย ซึ่งมีความซับซ้อนซ่อนอยู่
(ดูคำอธิบายใน resolve_photo_path)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.db.database import database

logger = logging.getLogger(__name__)

# ชื่อมุมของรูปที่ระบบใช้ เรียงตามลำดับที่อยากลองสกัด embedding
# เอาหน้าตรงขึ้นก่อนเพราะเป็นมุมที่ได้เวกเตอร์คุณภาพดีที่สุด
PHOTO_ANGLES = ("front", "left", "right")


@dataclass(frozen=True)
class MemberPhoto:
    """รูปใบหน้าหนึ่งรูปของสมาชิกหนึ่งคน"""

    angle: str          # "front" / "left" / "right"
    raw_path: str       # path ตามที่บันทึกไว้ในฐานข้อมูล
    resolved: Path      # path จริงที่เปิดได้ภายใน container

    @property
    def label(self) -> str:
        """ป้ายกำกับสั้น ๆ ไว้ใช้ใน log และเป็น source ของเวกเตอร์"""
        return f"{self.resolved.parent.name}/{self.resolved.name}"


@dataclass(frozen=True)
class Member:
    """สมาชิกหนึ่งคนพร้อมรายการรูปที่กำหนดไว้"""

    student_id: str
    first_name: str
    last_name: str
    photos: list[MemberPhoto]

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def resolve_photo_path(raw_path: str) -> Path:
    """แปลง path ที่เก็บในฐานข้อมูล ให้เป็น path จริงภายใน container

    ที่ต้องแปลงเพราะทั้งสองฝั่งมองไฟล์เดียวกันคนละมุม:

        ฐานข้อมูลเก็บ  : data/faces/66200407/front.jpg   (เทียบจากรากโปรเจกต์)
        ใน container   : /data/faces/66200407/front.jpg  (จุด mount ของ volume)

    เก็บแบบเทียบจากรากโปรเจกต์เพราะอ่านแล้วเข้าใจง่ายสำหรับคนที่เปิดดูใน Adminer
    และไม่ผูกกับว่า container จะ mount ไว้ตรงไหน

    รองรับ 3 รูปแบบ:
        1. path สัมบูรณ์ (ขึ้นต้นด้วย /) -> ใช้ตามนั้นเลย
        2. ขึ้นต้นด้วย data/faces/      -> ตัดส่วนหน้าออกแล้วต่อกับ FACES_DIR
        3. รูปแบบอื่น                    -> ต่อกับ FACES_DIR ตรง ๆ
    """
    faces_dir = settings.app.faces_dir
    cleaned = raw_path.strip().replace("\\", "/")

    path = Path(cleaned)

    if path.is_absolute():
        return path

    prefix = "data/faces/"
    if cleaned.startswith(prefix):
        return faces_dir / cleaned[len(prefix):]

    return faces_dir / cleaned


class MemberDirectory:
    """อ่านรายชื่อสมาชิกและรูปจากฐานข้อมูล"""

    def load(self) -> list[Member]:
        """ดึงสมาชิกทั้งหมดพร้อม path รูปที่แปลงแล้ว

        เมธอดนี้ไม่ตรวจว่าไฟล์รูปมีอยู่จริงหรือไม่ - ปล่อยให้ผู้เรียก
        (FaceIdentifier) เป็นคนตรวจและรายงาน เพราะมันต้องรายงานรวมกับ
        ปัญหาอื่น ๆ เช่นเปิดไฟล์ได้แต่หาใบหน้าไม่เจอ
        """
        rows = database.fetch_members()
        members: list[Member] = []

        for row in rows:
            photos: list[MemberPhoto] = []

            for angle in PHOTO_ANGLES:
                raw = row.get(f"photo_{angle}")
                if not raw:
                    # ไม่ได้กำหนด path ไว้ในฐานข้อมูล - ไม่ใช่ error
                    # (อาจตั้งใจมีแค่รูปหน้าตรง) ผู้เรียกจะรายงานเองถ้าไม่มีสักรูป
                    continue

                photos.append(
                    MemberPhoto(
                        angle=angle,
                        raw_path=raw,
                        resolved=resolve_photo_path(raw),
                    )
                )

            members.append(
                Member(
                    student_id=row["student_id"],
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    photos=photos,
                )
            )

        return members

    def build_name_map(self, members: list[Member]) -> dict[str, Member]:
        """ทำ dict ให้ค้นชื่อจากรหัสนักศึกษาได้เร็ว ๆ ตอนแสดงผล"""
        return {m.student_id: m for m in members}
