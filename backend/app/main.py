"""FastAPI application - จุดเริ่มต้นของ backend

เฟส 1 มีแค่ 2 endpoint:
    GET /api/health   ตรวจว่าต่อ PostgreSQL ได้ไหม + เวอร์ชันของฐานข้อมูล
    GET /api/members  คืนรายชื่อสมาชิกทั้งหมดเป็น JSON

หน้าเว็บเรียก endpoint พวกนี้ผ่าน nginx ที่ทำ reverse proxy ให้ (เส้นทาง /api/)
จึงมองเห็นเป็น origin เดียวกัน ไม่ติดปัญหา CORS
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.database import database

# -----------------------------------------------------------------------------
# ตั้งค่า log ให้ออกทาง stdout เพื่อให้เห็นผ่าน docker compose logs
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.app.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cads")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """จัดการทรัพยากรที่ต้องเปิด/ปิดพร้อมกับตัวแอป

    ตอนสตาร์ท : เปิด connection pool ของฐานข้อมูล
    ตอนปิด    : ปิด pool ให้เรียบร้อย ไม่ทิ้ง connection ค้างไว้
    """
    logger.info("=" * 70)
    logger.info("%s v%s กำลังเริ่มทำงาน", settings.app.name, settings.app.version)
    logger.info("ฐานข้อมูล: %s", settings.database.safe_repr())
    logger.info("โฟลเดอร์รูปใบหน้า: %s", settings.app.faces_dir)
    logger.info("=" * 70)

    database.connect()

    # ตรวจสุขภาพหนึ่งครั้งตอนสตาร์ท เพื่อให้เห็นปัญหาใน log ทันที ไม่ต้องรอ request แรก
    health = database.check_health()
    if health.connected:
        logger.info("เชื่อมต่อฐานข้อมูลสำเร็จ (%s)", health.version)
    else:
        # ไม่ทำให้แอปตาย เพื่อให้ /api/health ยังตอบได้และบอกสาเหตุให้ผู้ใช้เห็น
        logger.error("เชื่อมต่อฐานข้อมูลไม่สำเร็จ: %s", health.error)

    yield

    database.close()
    logger.info("ปิดระบบเรียบร้อย")


app = FastAPI(
    title=settings.app.name,
    version=settings.app.version,
    description="ระบบตรวจจับคนเข้าใช้ห้องของหลักสูตรวิศวกรรมคอมพิวเตอร์",
    lifespan=lifespan,
)

# เปิด CORS เฉพาะเมื่อกำหนด CORS_ORIGINS ไว้เท่านั้น
# ปกติไม่ต้องใช้เพราะเรียกผ่าน nginx อยู่แล้ว (ใส่ไว้เผื่อตอน debug จากเครื่องอื่น)
if settings.app.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.app.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# =============================================================================
# Endpoints
# =============================================================================


@app.get("/api/health", tags=["ระบบ"])
def health(response: Response) -> dict[str, Any]:
    """ตรวจสุขภาพระบบ: ต่อฐานข้อมูลได้ไหม เวอร์ชันอะไร มีสมาชิกกี่คน

    ถ้าต่อฐานข้อมูลไม่ได้จะตอบ HTTP 503 พร้อมสาเหตุใน field error
    (ไม่ตอบ 200 เฉย ๆ เพราะจะกลายเป็นการซ่อนปัญหา)
    """
    db_health = database.check_health()

    member_count: int | None = None
    if db_health.connected:
        try:
            member_count = database.count_members()
        except Exception as exc:  # noqa: BLE001
            # ต่อ DB ได้แต่ query ไม่ได้ = ตาราง members อาจยังไม่ถูกสร้าง
            logger.warning("นับจำนวนสมาชิกไม่สำเร็จ: %s", exc)
            db_health.error = f"ต่อฐานข้อมูลได้ แต่อ่านตาราง members ไม่ได้: {exc}"

    ok = db_health.connected and db_health.error is None
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if ok else "degraded",
        "app": {
            "name": settings.app.name,
            "version": settings.app.version,
            "phase": 1,
        },
        "database": {
            "connected": db_health.connected,
            "version": db_health.version,
            "target": settings.database.safe_repr(),  # ปิดบังรหัสผ่านไว้แล้ว
            "member_count": member_count,
            "error": db_health.error,
        },
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/members", tags=["สมาชิก"])
def list_members() -> dict[str, Any]:
    """คืนรายชื่อสมาชิกทั้งหมด เรียงตามนามสกุลแล้วชื่อ

    ชื่อ-นามสกุลที่หน้าเว็บแสดงต้องมาจากฐานข้อมูลเสมอ ห้าม hardcode ที่ฝั่ง frontend
    """
    rows = database.fetch_members()

    members = [
        {
            "id": row["id"],
            "student_id": row["student_id"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "photos": {
                "left": row["photo_left"],
                "front": row["photo_front"],
                "right": row["photo_right"],
            },
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]

    return {"total": len(members), "members": members}
