"""การเชื่อมต่อ PostgreSQL และ query ที่เกี่ยวกับตาราง members

ใช้ connection pool ของ psycopg 3 เพื่อไม่ต้องเปิด connection ใหม่ทุก request
(การเปิด connection ใหม่ทุกครั้งกิน CPU มาก โดยเฉพาะบน Raspberry Pi)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class DatabaseHealth:
    """ผลการตรวจสอบสุขภาพการเชื่อมต่อฐานข้อมูล"""

    connected: bool
    version: str | None = None  # เวอร์ชันของ PostgreSQL เช่น "PostgreSQL 16.4"
    error: str | None = None  # สาเหตุที่ต่อไม่ได้ (ต้องบอกเสมอเมื่อ connected=False)


class Database:
    """ห่อ connection pool ไว้ พร้อม query ที่ระบบต้องใช้"""

    def __init__(self) -> None:
        self._pool: ConnectionPool | None = None

    # ------------------------------------------------------------------
    # วงจรชีวิตของ pool
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """สร้าง connection pool

        open=False แล้วค่อยเรียก open() เอง เพื่อควบคุมจังหวะให้ชัด
        ไม่ใช้ wait() แบบบังคับ เพราะ compose รอ healthcheck ของ db ให้แล้ว
        และถ้า db ยังไม่พร้อมจริง เราอยากให้ /api/health รายงานสาเหตุออกมา
        มากกว่าจะให้ backend ตายตั้งแต่สตาร์ท
        """
        if self._pool is not None:
            return

        db = settings.database
        logger.info("กำลังเชื่อมต่อฐานข้อมูล: %s", db.safe_repr())

        self._pool = ConnectionPool(
            conninfo=db.conninfo,
            min_size=db.pool_min_size,
            max_size=db.pool_max_size,
            # คืน row เป็น dict จะได้อ่านโค้ดง่ายกว่าการอ้างด้วยเลข index
            kwargs={"row_factory": dict_row},
            open=False,
            name="cads-pool",
        )
        self._pool.open()

    def close(self) -> None:
        """ปิด pool ตอนแอปกำลังจะหยุด"""
        if self._pool is not None:
            logger.info("กำลังปิดการเชื่อมต่อฐานข้อมูล")
            self._pool.close()
            self._pool = None

    @property
    def pool(self) -> ConnectionPool:
        """ดึง pool ออกมาใช้ ถ้ายังไม่ได้เชื่อมต่อให้ฟ้องชัดเจน ไม่แอบเชื่อมต่อให้เอง"""
        if self._pool is None:
            raise RuntimeError(
                "ยังไม่ได้เรียก Database.connect() - "
                "ตรวจสอบ lifespan ของ FastAPI ใน app/main.py"
            )
        return self._pool

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def check_health(self) -> DatabaseHealth:
        """ตรวจว่าต่อฐานข้อมูลได้จริงหรือไม่ พร้อมดึงเวอร์ชันมาแสดง

        เมธอดนี้ต้องไม่โยน exception ออกไป เพราะ /api/health ต้องตอบได้เสมอ
        ถ้าต่อไม่ได้ ให้คืน connected=False พร้อม "สาเหตุ" ที่อ่านรู้เรื่อง
        """
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version() AS version")
                    row = cur.fetchone()
            raw_version = (row or {}).get("version", "")
            # ตัดเอาเฉพาะส่วนหน้า เช่น "PostgreSQL 16.4" ไม่เอารายละเอียด compiler ยาว ๆ
            short_version = " ".join(raw_version.split()[:2]) if raw_version else None
            return DatabaseHealth(connected=True, version=short_version)
        except Exception as exc:  # noqa: BLE001 - ต้องจับทุกกรณีแล้วรายงานสาเหตุ
            logger.warning("ตรวจสอบฐานข้อมูลไม่สำเร็จ: %s", exc)
            return DatabaseHealth(connected=False, error=str(exc))

    def fetch_members(self) -> list[dict[str, Any]]:
        """ดึงรายชื่อสมาชิกทั้งหมด เรียงตามนามสกุลแล้วชื่อ (ตรงกับ index idx_members_name)"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id,
                           student_id,
                           first_name,
                           last_name,
                           photo_left,
                           photo_front,
                           photo_right,
                           created_at,
                           updated_at
                      FROM members
                     ORDER BY last_name, first_name
                    """
                )
                return cur.fetchall()

    def count_members(self) -> int:
        """นับจำนวนสมาชิกทั้งหมด (ใช้ในหน้า health)"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS total FROM members")
                row = cur.fetchone()
                return int((row or {}).get("total", 0))


# instance เดียวใช้ร่วมกันทั้งแอป สร้าง/ปิดใน lifespan ของ FastAPI
database = Database()
