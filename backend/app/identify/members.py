"""ทะเบียนสมาชิก - แหล่งความจริงเรื่อง "ใครเป็นใคร"

กฎเหล็กของโปรเจกต์: ชื่อ-นามสกุลที่แสดงบนหน้าจอต้องมาจากฐานข้อมูลเสมอ
ห้าม hardcode ในโค้ดเด็ดขาด เพราะ
    - เพิ่ม/ลบ/แก้ชื่อสมาชิกต้องทำได้โดยไม่ต้องแก้โค้ดและ deploy ใหม่
    - ชื่อที่อยู่ในโค้ดจะไม่ตรงกับฐานข้อมูลในที่สุด แล้วไม่มีใครรู้ว่าอันไหนถูก

รูปใบหน้าก็อยู่ในฐานข้อมูลเช่นกัน (ตาราง member_photos เก็บตัวไฟล์ JPEG)
โฟลเดอร์ data/faces/<รหัส>/<มุม>.jpg เหลือไว้เป็นแค่ "ทางนำเข้า" รูปที่วางไว้ตรงนั้น
จะถูกย้ายเข้าฐานข้อมูลทุกครั้งที่โหลดสมาชิก (เฉพาะมุมที่ในฐานข้อมูลยังไม่มี)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
    student_id: str
    image: bytes        # ตัวไฟล์ JPEG จากตาราง member_photos

    @property
    def label(self) -> str:
        """ป้ายกำกับสั้น ๆ ไว้ใช้ใน log และเป็น source ของเวกเตอร์"""
        return f"{self.student_id}/{self.angle}"


@dataclass(frozen=True)
class Member:
    """สมาชิกหนึ่งคนพร้อมรูปที่มีในฐานข้อมูล"""

    student_id: str
    first_name: str
    last_name: str
    photos: list[MemberPhoto]

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class MemberDirectory:
    """อ่านรายชื่อสมาชิกและรูปจากฐานข้อมูล"""

    def load(self) -> list[Member]:
        """ดึงสมาชิกทั้งหมดพร้อมรูปทุกมุม

        ก่อนดึงจะย้ายรูปที่วางไว้ใน data/faces/ เข้าฐานข้อมูลก่อน (ถ้ามี)
        เมธอดนี้ไม่ตรวจว่ารูปใช้ได้หรือไม่ - ปล่อยให้ผู้เรียก (FaceIdentifier)
        เป็นคนตรวจและรายงาน รวมกับปัญหาอื่น ๆ เช่นเปิดรูปได้แต่หาใบหน้าไม่เจอ
        """
        database.ensure_member_photos_table()
        try:
            database.import_photos_from_folder(settings.app.faces_dir)
        except OSError as exc:
            # โฟลเดอร์นำเข้าอ่านไม่ได้ไม่ใช่เหตุให้โหลดสมาชิกไม่ได้ รูปในฐานข้อมูลยังใช้ได้อยู่
            logger.warning("อ่านรูปจาก %s ไม่สำเร็จ: %s", settings.app.faces_dir, exc)

        photos_by_member: dict[str, dict[str, bytes]] = {}
        for row in database.fetch_member_photos():
            photos_by_member.setdefault(row["student_id"], {})[row["angle"]] = bytes(row["image"])

        members: list[Member] = []
        for row in database.fetch_members():
            sid = row["student_id"]
            have = photos_by_member.get(sid, {})
            members.append(
                Member(
                    student_id=sid,
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    photos=[
                        MemberPhoto(angle=angle, student_id=sid, image=have[angle])
                        for angle in PHOTO_ANGLES
                        if angle in have
                    ],
                )
            )

        return members

    def build_name_map(self, members: list[Member]) -> dict[str, Member]:
        """ทำ dict ให้ค้นชื่อจากรหัสนักศึกษาได้เร็ว ๆ ตอนแสดงผล"""
        return {m.student_id: m for m in members}
