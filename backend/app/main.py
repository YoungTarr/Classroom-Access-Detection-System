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
from app.core.pipeline import create_pipeline
from app.core.tracker import tracks_to_dicts
from app.db.database import database
from app.face.detector import FaceModelError, face_detector
from app.face.recognizer import face_recognizer
from app.identify import IdentifierCreationError, create_identifier

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
        await asyncio.to_thread(face_recognizer.load)
    except FaceModelError as exc:
        app.state.face_model_error = str(exc)
        logger.error("โหลดโมเดลใบหน้าไม่สำเร็จ:\n%s", exc)

    # ---- สร้างตัวระบุตัวตนและคลังเวกเตอร์ใบหน้า ----
    # ใช้ตัวเดียวร่วมกันทุกการเชื่อมต่อ เพราะคลังเวกเตอร์เป็นข้อมูลอ่านอย่างเดียว
    # (ส่วนที่เป็นสถานะคือผลโหวต ซึ่งเก็บอยู่ใน track ของแต่ละ pipeline)
    app.state.identifier = None
    app.state.identifier_error = None

    if app.state.face_model_error is None:
        try:
            identifier = create_identifier()
            # สร้างคลังตั้งแต่สตาร์ท จะได้เห็นปัญหาเรื่องไฟล์รูปทันทีใน log
            # ไม่ใช่ไปเจอตอนมีคนเดินผ่านกล้องแล้วขึ้น Unknown โดยไม่รู้สาเหตุ
            await asyncio.to_thread(identifier.reload)
            app.state.identifier = identifier
        except IdentifierCreationError as exc:
            app.state.identifier_error = str(exc)
            logger.error("สร้างตัวระบุตัวตนไม่สำเร็จ: %s", exc)
        except Exception as exc:  # noqa: BLE001
            app.state.identifier_error = f"สร้างคลังใบหน้าไม่สำเร็จ: {exc}"
            logger.exception("สร้างคลังใบหน้าไม่สำเร็จ")
    else:
        app.state.identifier_error = "ข้ามการสร้างคลังใบหน้า เพราะโหลดโมเดลไม่สำเร็จ"

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
    face_status["recognition_model"] = face_recognizer.model_name
    face_status["recognition_loaded"] = face_recognizer.is_loaded

    identifier = getattr(app.state, "identifier", None)
    if identifier is not None:
        identify_status = identifier.status()
        identify_status["error"] = None
    else:
        identify_status = {
            "mode": settings.identify.mode,
            "ready": False,
            "error": getattr(app.state, "identifier_error", None) or "ยังไม่ได้สร้าง",
        }

    frame_source = getattr(app.state, "frame_source", None)
    source_status: dict[str, Any] = {
        "type": settings.stream.source,
        "alive": frame_source.is_alive() if frame_source else False,
    }
    if isinstance(frame_source, BrowserFrameSource):
        source_status["received_frames"] = frame_source.received_count

    db_ok = db_health.connected and db_health.error is None
    face_ok = face_status["loaded"] and face_status["recognition_loaded"]

    # หมายเหตุ: คลังใบหน้าว่าง (ยังไม่มีรูป) ไม่ถือว่าระบบพัง
    # ระบบยังตรวจจับและติดตามได้ปกติ แค่จะขึ้น Unknown กับทุกคน
    # จึงนับว่า degraded เฉพาะเมื่อ "สร้างตัวระบุตัวตนไม่ได้เลย" เท่านั้น
    identify_ok = identifier is not None
    ok = db_ok and face_ok and identify_ok

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if ok else "degraded",
        "app": {
            "name": settings.app.name,
            "version": settings.app.version,
            "phase": 4,
        },
        "database": {
            "connected": db_health.connected,
            "version": db_health.version,
            "target": settings.database.safe_repr(),  # ปิดบังรหัสผ่านไว้แล้ว
            "member_count": member_count,
            "error": db_health.error,
        },
        "face": face_status,
        "identify": identify_status,
        "frame_source": source_status,
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/faces/reload", tags=["สมาชิก"])
async def reload_faces(response: Response) -> dict[str, Any]:
    """สร้างคลังเวกเตอร์ใบหน้าใหม่ โดยไม่ต้องรีสตาร์ท container

    ใช้เมื่อ
        - เพิ่มรูปใหม่ลงใน data/faces/
        - เพิ่ม/แก้/ลบสมาชิกในฐานข้อมูล
        - แก้ไฟล์รูปที่เคยมีปัญหาแล้วอยากให้ลองใหม่

    ตอบกลับพร้อมรายงานเต็มว่าได้เวกเตอร์กี่ตัว ใครลงทะเบียนไม่สำเร็จ
    และไฟล์ไหนมีปัญหาอะไร เพื่อให้แก้ได้ตรงจุดโดยไม่ต้องไปไล่อ่าน log
    """
    identifier = getattr(app.state, "identifier", None)

    if identifier is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "error",
            "message": (
                getattr(app.state, "identifier_error", None)
                or "ยังไม่ได้สร้างตัวระบุตัวตน"
            ),
        }

    try:
        # to_thread เพราะการอ่านไฟล์และสกัด embedding เป็นงาน blocking ที่ใช้เวลานาน
        report = await asyncio.to_thread(identifier.reload)
    except Exception as exc:  # noqa: BLE001 - endpoint นี้ต้องรายงานสาเหตุเต็ม ๆ
        logger.exception("สร้างคลังใบหน้าใหม่ไม่สำเร็จ")
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"status": "error", "message": f"สร้างคลังใบหน้าใหม่ไม่สำเร็จ: {exc}"}

    return {
        "status": "ok",
        "message": (
            f"สร้างคลังใหม่สำเร็จ: สมาชิก {report.member_count} คน "
            f"ลงทะเบียนได้ {report.enrolled_count} คน "
            f"รวม {report.vector_count} เวกเตอร์"
        ),
        "report": report.as_dict(),
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
        "tracking": {
            # หน้าเว็บใช้ค่านี้ทำ interpolate ให้กรอบขยับลื่น
            "smoothing": settings.tracking.smoothing,
            "max_missing": settings.tracking.max_missing,
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
          "tracks": [                    ผลหลังผ่านการติดตามข้ามเฟรมแล้ว
            {
              "track_id": 3,             คงที่ตลอดที่คนคนนี้ยังอยู่ในภาพ
              "bbox": [x, y, w, h],
              "score": 0.94,
              "visible": true,           false = กรอบที่ค้างไว้เพราะเพิ่งคลาดไป
              "missing": 0,              จำนวนเฟรมติดกันที่หาไม่เจอ
              "hits": 27                 จำนวนเฟรมที่เจอสะสม
            }
          ],
          "detected_count": 1,           จำนวนใบหน้าที่ตรวจเจอจริงในเฟรมนี้
          "source_size": [640, 480],     ขนาดของภาพที่ส่งเข้ามา
          "process_ms": 42.1,            เวลารวมที่ใช้ประมวลผล
          "detect_ms": 41.0,
          "track_ms": 1.1
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

    # สร้าง pipeline ใหม่ต่อหนึ่งการเชื่อมต่อ
    # เพราะตัวติดตามจำ track ไว้ข้างใน ถ้าใช้ร่วมกันสองแท็บจะจับคู่ใบหน้ากันมั่ว
    # ส่วนตัวระบุตัวตนใช้ร่วมกันได้ เพราะคลังเวกเตอร์เป็นข้อมูลอ่านอย่างเดียว
    pipeline = create_pipeline(identifier=getattr(app.state, "identifier", None))

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

            # การประมวลผลเป็นงาน CPU หนักและเป็น blocking
            # ต้องโยนไปทำใน thread แยก ไม่งั้นจะบล็อก event loop ทั้งเซิร์ฟเวอร์
            # (ผลคือ request อื่น ๆ เช่น /api/health จะค้างตามไปด้วย)
            started = asyncio.get_running_loop().time()
            result = await asyncio.to_thread(pipeline.process, frame)
            process_ms = (asyncio.get_running_loop().time() - started) * 1000.0

            await websocket.send_json({
                "type": "detections",
                "frame_id": result.frame_id,
                "tracks": tracks_to_dicts(result.tracks),
                "detected_count": result.detected_count,
                "source_size": list(result.source_size),
                "process_ms": round(process_ms, 1),
                "detect_ms": round(result.detect_ms, 1),
                "track_ms": round(result.track_ms, 2),
                "identify_ms": round(result.identify_ms, 1),
                "identified_count": result.identified_count,
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
