"""FastAPI application - จุดเริ่มต้นของ backend

เฟส 1:
    GET  /api/health      ตรวจว่าต่อ PostgreSQL ได้ไหม + เวอร์ชันของฐานข้อมูล
    GET  /api/members     คืนรายชื่อสมาชิกทั้งหมดเป็น JSON

เฟส 2:
    GET  /api/config      ส่งค่า config ที่ frontend ต้องใช้ (กฎ: config อยู่ที่เดียว)
    WS   /ws/detect       รับเฟรม JPEG จากเบราว์เซอร์ ตรวจจับใบหน้า แล้วส่งผลกลับ

หน้าเว็บเรียกทุกอย่างผ่าน nginx ที่ทำ reverse proxy ให้ (/api/ และ /ws/)
จึงมองเห็นเป็น origin เดียวกัน ไม่ติดปัญหา CORS
"""

from __future__ import annotations

import asyncio
import logging
import struct
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.frame_source import BrowserFrameSource, FrameDecodeError, create_frame_source
from app.db.database import database
from app.face.detector import FaceModelError, face_detector

# -----------------------------------------------------------------------------
# ตั้งค่า log ให้ออกทาง stdout เพื่อให้เห็นผ่าน docker compose logs
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.app.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cads")

# -----------------------------------------------------------------------------
# รูปแบบข้อความ WebSocket ที่ตกลงกันระหว่างเบราว์เซอร์กับ backend
#
# เบราว์เซอร์ส่งมาเป็น binary frame หน้าตาแบบนี้:
#
#     [ 4 ไบต์ ][ ..... ข้อมูล JPEG ..... ]
#       frame_id
#       uint32 little-endian
#
# เหตุผลที่ต้องแนบ frame_id มากับภาพ: ผลลัพธ์ที่ส่งกลับไปต้องบอกได้ว่าเป็นของเฟรมไหน
# ถ้าส่งเป็น 2 ข้อความแยกกัน (JSON + binary) จะมีโอกาสสลับลำดับกันได้
# และถ้าใช้ base64 ใส่ใน JSON ข้อมูลจะบวมขึ้นประมาณ 33% โดยไม่จำเป็น
# -----------------------------------------------------------------------------
FRAME_HEADER_FORMAT = "<I"  # uint32 little-endian
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER_FORMAT)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """จัดการทรัพยากรที่ต้องเปิด/ปิดพร้อมกับตัวแอป

    ตอนสตาร์ท : เปิด connection pool + โหลดโมเดลใบหน้า + เตรียมแหล่งภาพ
    ตอนปิด    : ปิดทุกอย่างให้เรียบร้อย ไม่ทิ้ง connection หรือ thread ค้างไว้
    """
    logger.info("=" * 70)
    logger.info("%s v%s กำลังเริ่มทำงาน", settings.app.name, settings.app.version)
    logger.info("ฐานข้อมูล: %s", settings.database.safe_repr())
    logger.info("โฟลเดอร์รูปใบหน้า: %s", settings.app.faces_dir)
    logger.info("แหล่งภาพ: %s", settings.stream.source)
    logger.info("=" * 70)

    database.connect()

    # ตรวจสุขภาพหนึ่งครั้งตอนสตาร์ท เพื่อให้เห็นปัญหาใน log ทันที ไม่ต้องรอ request แรก
    health = database.check_health()
    if health.connected:
        logger.info("เชื่อมต่อฐานข้อมูลสำเร็จ (%s)", health.version)
    else:
        # ไม่ทำให้แอปตาย เพื่อให้ /api/health ยังตอบได้และบอกสาเหตุให้ผู้ใช้เห็น
        logger.error("เชื่อมต่อฐานข้อมูลไม่สำเร็จ: %s", health.error)

    # ---- โหลดโมเดลใบหน้า ----
    # โหลดครั้งเดียวตรงนี้ ไม่ใช่ตอนมี request เข้ามา
    # ถ้าโหลดไม่ได้ให้ปล่อยให้แอปขึ้นต่อ แต่จดสาเหตุไว้รายงานทาง /api/health
    # (ถ้าตายไปเลย ผู้ใช้จะเห็นแค่ container restart วน หาสาเหตุไม่เจอ)
    app.state.face_model_error = None
    try:
        # to_thread เพราะการโหลดโมเดลใช้เวลาหลายวินาทีและเป็นงาน blocking
        await asyncio.to_thread(face_detector.load)
    except FaceModelError as exc:
        app.state.face_model_error = str(exc)
        logger.error("โหลดโมเดลใบหน้าไม่สำเร็จ:\n%s", exc)

    # ---- เตรียมแหล่งภาพ ----
    app.state.frame_source = create_frame_source(settings.stream.source)
    app.state.frame_source.start()

    yield

    app.state.frame_source.stop()
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
# REST endpoints
# =============================================================================


@app.get("/api/health", tags=["ระบบ"])
def health(response: Response) -> dict[str, Any]:
    """ตรวจสุขภาพระบบ: ฐานข้อมูล + โมเดลใบหน้า + แหล่งภาพ

    ตอบ HTTP 503 เมื่อมีส่วนใดใช้งานไม่ได้ พร้อมบอกสาเหตุ
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

    face_status = face_detector.status()
    face_status["error"] = getattr(app.state, "face_model_error", None)

    frame_source = getattr(app.state, "frame_source", None)
    source_status: dict[str, Any] = {
        "type": settings.stream.source,
        "alive": frame_source.is_alive() if frame_source else False,
    }
    if isinstance(frame_source, BrowserFrameSource):
        source_status["received_frames"] = frame_source.received_count

    db_ok = db_health.connected and db_health.error is None
    face_ok = face_status["loaded"]
    ok = db_ok and face_ok

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if ok else "degraded",
        "app": {
            "name": settings.app.name,
            "version": settings.app.version,
            "phase": 2,
        },
        "database": {
            "connected": db_health.connected,
            "version": db_health.version,
            "target": settings.database.safe_repr(),  # ปิดบังรหัสผ่านไว้แล้ว
            "member_count": member_count,
            "error": db_health.error,
        },
        "face": face_status,
        "frame_source": source_status,
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/config", tags=["ระบบ"])
def get_config() -> dict[str, Any]:
    """ค่า config ที่ frontend ต้องใช้

    มี endpoint นี้เพื่อรักษากฎ "ค่า config ทุกตัวอยู่ที่ backend/app/config.py ที่เดียว"
    ถ้าไม่มี เราจะต้องไปตั้งค่าซ้ำในไฟล์ JavaScript แล้วสองที่จะไม่ตรงกันในที่สุด
    """
    stream = settings.stream
    return {
        "stream": {
            "source": stream.source,
            "target_width": stream.target_width,
            "send_fps": stream.send_fps,
            "jpeg_quality": stream.jpeg_quality,
            "mirror": stream.mirror,
        },
        "face": {
            "det_thresh": settings.face.det_thresh,
            "model_pack": settings.face.model_pack,
        },
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


# =============================================================================
# WebSocket: รับเฟรมจากเบราว์เซอร์แล้วตรวจจับใบหน้า
# =============================================================================


@app.websocket("/ws/detect")
async def websocket_detect(websocket: WebSocket) -> None:
    """รับเฟรมภาพจากเบราว์เซอร์ ตรวจจับใบหน้า แล้วส่งผลกลับทีละเฟรม

    รูปแบบข้อความเข้า : binary = [frame_id uint32 LE][JPEG bytes]
    รูปแบบข้อความออก : JSON
        {
          "frame_id": 123,
          "faces": [{"bbox": [x, y, w, h], "score": 0.94}],
          "source_size": [640, 480],     ขนาดของภาพที่ส่งเข้ามา
          "process_ms": 42.1             เวลาที่ใช้ตรวจจับเฉพาะส่วน AI
        }

    เรื่อง backpressure: ฝั่งเบราว์เซอร์เป็นคนคุม โดยจะไม่ส่งเฟรมใหม่จนกว่าจะได้
    ผลของเฟรมก่อนหน้ากลับไป ทำให้ที่นี่ไม่มีคิวสะสมและ latency ไม่ถ่างขึ้น
    ฝั่งนี้จึงประมวลผลทีละข้อความตามลำดับที่ได้รับได้เลย
    """
    await websocket.accept()

    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    logger.info("WebSocket /ws/detect เชื่อมต่อเข้ามาจาก %s", client)

    # ถ้าโมเดลโหลดไม่สำเร็จ ต้องบอกให้ชัดแล้วปิดการเชื่อมต่อ
    # ไม่ใช่รับเฟรมไปเรื่อย ๆ แล้วตอบผลว่างเปล่าจนผู้ใช้นึกว่ากล้องเสีย
    if not face_detector.is_loaded:
        reason = getattr(app.state, "face_model_error", None) or "โมเดลยังไม่ถูกโหลด"
        await websocket.send_json({"type": "error", "message": f"ระบบตรวจจับใบหน้าไม่พร้อม: {reason}"})
        await websocket.close(code=1011)
        logger.warning("ปิด WebSocket เพราะโมเดลไม่พร้อม: %s", reason)
        return

    frame_source = app.state.frame_source
    if not isinstance(frame_source, BrowserFrameSource):
        await websocket.send_json({
            "type": "error",
            "message": (
                f"ขณะนี้ระบบตั้งค่า FRAME_SOURCE={settings.stream.source} "
                "จึงไม่รับภาพจากเบราว์เซอร์ (ต้องตั้งเป็น browser)"
            ),
        })
        await websocket.close(code=1011)
        return

    try:
        while True:
            message = await websocket.receive_bytes()

            if len(message) <= FRAME_HEADER_SIZE:
                await websocket.send_json({
                    "type": "error",
                    "message": f"ข้อมูลสั้นเกินไป ({len(message)} ไบต์) ไม่มีส่วนภาพ",
                })
                continue

            (frame_id,) = struct.unpack(FRAME_HEADER_FORMAT, message[:FRAME_HEADER_SIZE])
            jpeg_bytes = message[FRAME_HEADER_SIZE:]

            try:
                frame = frame_source.submit(frame_id, jpeg_bytes)
            except FrameDecodeError as exc:
                logger.warning("เฟรม %d ถอดรหัสไม่ได้: %s", frame_id, exc)
                await websocket.send_json({
                    "type": "error",
                    "frame_id": frame_id,
                    "message": str(exc),
                })
                continue

            # การตรวจจับเป็นงาน CPU หนักและเป็น blocking
            # ต้องโยนไปทำใน thread แยก ไม่งั้นจะบล็อก event loop ทั้งเซิร์ฟเวอร์
            # (ผลคือ request อื่น ๆ เช่น /api/health จะค้างตามไปด้วย)
            started = asyncio.get_running_loop().time()
            faces = await asyncio.to_thread(face_detector.detect, frame.image)
            process_ms = (asyncio.get_running_loop().time() - started) * 1000.0

            width, height = frame.size

            await websocket.send_json({
                "type": "detections",
                "frame_id": frame_id,
                "faces": [face.as_dict() for face in faces],
                "source_size": [width, height],
                "process_ms": round(process_ms, 1),
            })

    except WebSocketDisconnect:
        logger.info("WebSocket /ws/detect ตัดการเชื่อมต่อ (%s)", client)
    except Exception as exc:  # noqa: BLE001 - ต้องไม่ให้ error หลุดไปทำให้ worker ตาย
        logger.exception("WebSocket /ws/detect เกิดข้อผิดพลาด: %s", exc)
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            # การเชื่อมต่อถูกปิดไปแล้ว ไม่ต้องทำอะไรต่อ
            pass
