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

    # ------------------------------------------------------------------
    # ประวัติเข้า-ออก (เฟส 12)
    # ------------------------------------------------------------------
    # ต้องตรงกับ db/schema.sql (ไฟล์นั้นรันเฉพาะตอนสร้าง volume ใหม่
    # ส่วนตรงนี้รันทุกครั้งที่สตาร์ท จึงใช้กับฐานข้อมูลที่มีข้อมูลอยู่แล้วได้ด้วย)
    _ACCESS_LOGS_DDL = """
        CREATE TABLE IF NOT EXISTS access_logs (
            id           BIGSERIAL    PRIMARY KEY,
            student_id   VARCHAR(20)  NOT NULL,
            first_name   VARCHAR(100) NOT NULL,
            last_name    VARCHAR(100) NOT NULL,
            logged_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            direction    VARCHAR(3)   NOT NULL CHECK (direction IN ('IN', 'OUT')),
            camera_id    VARCHAR(20)  NOT NULL,
            camera_name  VARCHAR(100),
            bbox_x       INTEGER      NOT NULL,
            bbox_y       INTEGER      NOT NULL,
            bbox_w       INTEGER      NOT NULL,
            bbox_h       INTEGER      NOT NULL,
            frame_width  INTEGER      NOT NULL,
            frame_height INTEGER      NOT NULL,
            confidence   REAL,
            track_id     INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_access_logs_logged_at ON access_logs (logged_at DESC);
        CREATE INDEX IF NOT EXISTS idx_access_logs_student   ON access_logs (student_id, logged_at DESC);

        -- เฟส 13: บันทึกคนที่ไม่รู้จักด้วย (ไม่มีรหัส/ชื่อ แต่มีรูปใบหน้า)
        -- ทุกคำสั่งรันซ้ำได้ จึงใช้อัปเกรดตารางของเฟส 12 ที่มีข้อมูลอยู่แล้วได้เลย
        ALTER TABLE access_logs ADD COLUMN IF NOT EXISTS is_unknown BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE access_logs ADD COLUMN IF NOT EXISTS face_jpeg  BYTEA;
        ALTER TABLE access_logs ALTER COLUMN student_id DROP NOT NULL;
        ALTER TABLE access_logs ALTER COLUMN first_name DROP NOT NULL;
        ALTER TABLE access_logs ALTER COLUMN last_name  DROP NOT NULL;
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'access_logs_identity_check'
            ) THEN
                ALTER TABLE access_logs ADD CONSTRAINT access_logs_identity_check
                    CHECK (is_unknown OR student_id IS NOT NULL);
            END IF;
        END $$;
    """

    def ensure_access_logs_table(self) -> None:
        """สร้างตาราง access_logs ถ้ายังไม่มี (ปลอดภัยที่จะเรียกซ้ำ)"""
        with self.pool.connection() as conn:
            conn.execute(self._ACCESS_LOGS_DDL)

    def insert_access_log(self, row: dict[str, Any]) -> int:
        """เพิ่มประวัติหนึ่งรายการ คืนค่า id ที่ได้"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO access_logs
                        (is_unknown, face_jpeg,
                         student_id, first_name, last_name, logged_at, direction,
                         camera_id, camera_name, bbox_x, bbox_y, bbox_w, bbox_h,
                         frame_width, frame_height, confidence, track_id)
                    VALUES
                        (%(is_unknown)s, %(face_jpeg)s,
                         %(student_id)s, %(first_name)s, %(last_name)s, %(logged_at)s,
                         %(direction)s, %(camera_id)s, %(camera_name)s,
                         %(bbox_x)s, %(bbox_y)s, %(bbox_w)s, %(bbox_h)s,
                         %(frame_width)s, %(frame_height)s, %(confidence)s, %(track_id)s)
                    RETURNING id
                    """,
                    row,
                )
                return int(cur.fetchone()["id"])

    # หลังลบทุกครั้งให้เลขลำดับถัดไป = เลขมากสุดที่เหลืออยู่ + 1 (ว่างเปล่า = เริ่มที่ 1 ใหม่)
    # ทำในธุรกรรมเดียวกับการลบ ผลคือ ลบทั้งหมดแล้วรายการต่อไปได้ลำดับที่ 1
    # และลบรายการล่าสุดแล้วเลขนั้นถูกใช้ซ้ำ (ลบรายการกลางแถวจะเหลือช่องว่างตามปกติ
    # เพราะเลขลำดับเป็นตัวอ้างอิงถาวรของรายการ ไม่ถูกเรียงเลขใหม่)
    _RESET_SEQUENCE_SQL = """
        SELECT setval(
            pg_get_serial_sequence('access_logs', 'id'),
            COALESCE((SELECT MAX(id) FROM access_logs), 0) + 1,
            false
        )
    """

    def delete_access_log(self, log_id: int) -> bool:
        """ลบประวัติหนึ่งรายการ คืน True ถ้ามีรายการนั้นอยู่จริงและถูกลบแล้ว"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM access_logs WHERE id = %s", (log_id,))
                deleted = cur.rowcount > 0
                cur.execute(self._RESET_SEQUENCE_SQL)
                return deleted

    def delete_all_access_logs(self) -> int:
        """ลบประวัติทั้งหมด คืนจำนวนรายการที่ถูกลบ และเริ่มนับลำดับที่ 1 ใหม่"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM access_logs")
                count = cur.rowcount
                cur.execute(self._RESET_SEQUENCE_SQL)
                return count

    def fetch_access_logs(
        self,
        limit: int = 50,
        direction: str | None = None,
        student_id: str | None = None,
        unknown: bool | None = None,
    ) -> list[dict[str, Any]]:
        """ดึงประวัติล่าสุดก่อน (กรองตามทิศทาง/รหัสนักศึกษา/เฉพาะคนที่ไม่รู้จักได้)

        ไม่ดึงตัวรูปใบหน้ามาด้วย (หนัก) ส่งแค่ has_face ไว้บอกว่ามีรูปให้เปิดดูไหม
        """
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if direction:
            conditions.append("direction = %(direction)s")
            params["direction"] = direction
        if unknown is not None:
            conditions.append("is_unknown = %(unknown)s")
            params["unknown"] = unknown
        if student_id:
            conditions.append("student_id = %(student_id)s")
            params["student_id"] = student_id
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, is_unknown, face_jpeg IS NOT NULL AS has_face,
                           student_id, first_name, last_name, logged_at, direction,
                           camera_id, camera_name, bbox_x, bbox_y, bbox_w, bbox_h,
                           frame_width, frame_height, confidence, track_id
                      FROM access_logs
                      {where}
                     ORDER BY logged_at DESC, id DESC
                     LIMIT %(limit)s
                    """,
                    params,
                )
                return cur.fetchall()

    def fetch_access_log_face(self, log_id: int) -> bytes | None:
        """รูปใบหน้า (JPEG) ของประวัติรายการหนึ่ง หรือ None ถ้าไม่มีรายการ/ไม่มีรูป"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT face_jpeg FROM access_logs WHERE id = %s", (log_id,))
                row = cur.fetchone()
                if row is None or row["face_jpeg"] is None:
                    return None
                return bytes(row["face_jpeg"])


# instance เดียวใช้ร่วมกันทั้งแอป สร้าง/ปิดใน lifespan ของ FastAPI
database = Database()
