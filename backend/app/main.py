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
import json
import logging
import struct
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.frame_source import (
    BrowserFrameSource,
    FrameDecodeError,
    FrameSource,
    create_frame_sources,
)
from app.core.detect_scheduler import DetectScheduler
from app.core.pipeline import create_pipeline
from app.core.stream_runner import CameraRunner
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
    # เป็น dict เพราะโหมด rtsp มีได้หลายกล้องพร้อมกัน
    app.state.frame_sources = create_frame_sources(settings.stream.source)
    for source in app.state.frame_sources.values():
        source.start()

    if settings.stream.source == "rtsp":
        for camera in settings.rtsp.cameras:
            logger.info(
                "กล้อง %s (%s) ทิศทาง %s ชนิด %s -> %s  [%s]",
                camera.id,
                camera.name,
                camera.direction,
                camera.source,
                camera.safe_url(),  # ปิดบังรหัสผ่านไว้แล้ว
                "เปิดใช้งาน" if camera.enabled else "ปิดอยู่",
            )

    # ---- ประตูคิวตรวจจับที่กล้องทุกตัวใช้ร่วมกัน (เฟส 8) ----
    # สร้างก่อนตัวขับสตรีม เพราะทุกตัวต้องได้ตัวเดียวกันนี้ไป
    app.state.detect_scheduler = DetectScheduler(settings.rates.detect_fps_total)

    # ---- ตัวขับสตรีมหนึ่งตัวต่อหนึ่งกล้อง (เฟส 7 + 8) ----
    # สร้างตอนสตาร์ท ไม่ใช่ตอนมีคนเปิดหน้าเว็บ เพราะระบบต้องรู้ว่าใครเดินผ่าน
    # แม้ตอนนั้นจะไม่มีใครเฝ้าดูอยู่เลยก็ตาม
    #
    # **จุดสำคัญที่สุดของเฟส 8**: ส่ง app.state.identifier ตัวเดิมให้ทุกกล้อง
    # ห้ามเรียก create_identifier() ซ้ำในลูปนี้เด็ดขาด เพราะข้างในมีทั้งโมเดล
    # จดจำใบหน้าและ FAISS index ซึ่งกินหลายร้อย MB ต่อชุด
    # (ส่วนโมเดลตรวจจับเป็น instance เดียวระดับโมดูลอยู่แล้ว - face/detector.py)
    #
    # สร้างให้ "ทุกกล้อง" ไม่ว่าภาพจะมาจาก RTSP หรือจากเว็บแคมที่เบราว์เซอร์ป้อนให้
    # เงื่อนไขเดียวคือแหล่งภาพนั้นผูกกับกล้องตัวหนึ่ง (source.camera ไม่เป็น None)
    # ซึ่งกันไม่ให้โหมดเว็บแคมเดี่ยว (FRAME_SOURCE=browser) ถูกสร้าง runner ไปด้วย
    app.state.camera_runners = {}
    for camera_id, source in app.state.frame_sources.items():
        if getattr(source, "camera", None) is None:
            continue

        runner = CameraRunner(
            source,
            identifier=app.state.identifier,
            scheduler=app.state.detect_scheduler,
        )
        await runner.start()
        app.state.camera_runners[camera_id] = runner

    if app.state.camera_runners:
        logger.info(
            "เปิดใช้งานกล้องทั้งหมด %d ตัว: %s "
            "(ตรวจจับกล้องละ %d fps เพดานรวมทั้งระบบ %d fps, โมเดลและคลังใบหน้าใช้ร่วมกันชุดเดียว)",
            len(app.state.camera_runners),
            ", ".join(app.state.camera_runners),
            settings.rates.detect_fps,
            settings.rates.detect_fps_total,
        )

    yield

    for runner in getattr(app.state, "camera_runners", {}).values():
        await runner.stop()
    for source in app.state.frame_sources.values():
        source.stop()
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

    sources = getattr(app.state, "frame_sources", {}) or {}
    source_status: dict[str, Any] = {
        "type": settings.stream.source,
        "alive": any(s.is_alive() for s in sources.values()),
    }

    # โหมดเว็บแคมเดี่ยว (ไม่มีกล้องในระบบ) รายงานแค่จำนวนเฟรมที่รับมา
    browser_source = sources.get("browser")
    if isinstance(browser_source, BrowserFrameSource) and browser_source.camera is None:
        source_status["received_frames"] = browser_source.received_count

    # โหมดกล้องหลายตัว: รายงานสถานะ "ทีละกล้อง" เพื่อให้รู้ว่าตัวไหนหลุด (เฟส 8)
    #
    # ต้องแยกกันจริง ๆ ไม่ใช่สรุปรวมว่า "กล้องพร้อม/ไม่พร้อม"
    # เพราะเกณฑ์ของเฟสนี้คือถอดปลั๊กตัวหนึ่งแล้วต้องดูออกว่าเป็นตัวไหน
    # ส่วนอีกตัวต้องยังรายงานว่าปกติอยู่
    #
    # รวมกล้องทุกชนิด (rtsp และ browser) ด้วยโค้ดชุดเดียว เพราะ status()
    # ของทั้งสองชนิดคืนรูปแบบเดียวกัน (ดู core/frame_source.py)
    camera_sources: list[FrameSource] = [
        s for s in sources.values() if getattr(s, "camera", None) is not None
    ]
    if camera_sources:
        runners = getattr(app.state, "camera_runners", {}) or {}
        cameras_status = []

        for source in camera_sources:
            entry = source.status()
            runner = runners.get(source.camera.id)
            if runner is not None:
                # รวมสถิติของตัวขับสตรีมเข้าไปด้วย จะได้เห็นทั้งฝั่งรับภาพและฝั่งประมวลผล
                stats = runner.status()
                entry["fps"] = stats["measured_fps"]
                entry["detect_ms"] = stats["detect_ms"]
                entry["identify_ms"] = stats["identify_ms"]
                entry["latency_ms"] = stats["latency_ms"]
                entry["viewers"] = stats["viewers"]
            cameras_status.append(entry)

        source_status["cameras"] = cameras_status
        source_status["camera_count"] = len(cameras_status)
        source_status["cameras_alive"] = sum(1 for c in cameras_status if c["alive"])

        scheduler = getattr(app.state, "detect_scheduler", None)
        if scheduler is not None:
            # ดูได้ว่ากล้องแต่ละตัวได้คิวตรวจไปกี่ครั้ง (ต้องใกล้เคียงกัน = แบ่งกันยุติธรรม)
            source_status["detect_scheduler"] = scheduler.status()

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
            "phase": 8,
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
        "direction": {
            # ส่งกรอบอ้างอิงไปให้หน้าเว็บแสดงด้วย เพื่อไม่ให้ผู้ใช้ตีความ "ซ้าย" ผิดด้าน
            "reference": settings.direction.reference,
            "min_shift_ratio": settings.direction.min_shift_ratio,
            "frame_gap": settings.direction.frame_gap,
        },
    }


@app.get("/api/cameras", tags=["ระบบ"])
def list_cameras() -> dict[str, Any]:
    """รายการกล้อง IP พร้อมสถานะการเชื่อมต่อของแต่ละตัว

    หน้าเว็บใช้ endpoint นี้ "สร้างจอ" ให้ครบทุกกล้องตอนเปิดหน้า
    (ตั้งแต่เฟส 8 ไม่มี dropdown ให้เลือกแล้ว แต่แสดงทุกตัวพร้อมกัน)
    ลำดับที่คืนออกไปเรียงตาม CAMERA_IDS ใน .env เสมอ เพื่อให้ตำแหน่งจอไม่สลับที่
    ทุกครั้งที่รีเฟรชหน้าเว็บ

    URL ที่ส่งออกไปถูกปิดบังรหัสผ่านไว้แล้วเสมอ (safe_url)
    """
    sources = getattr(app.state, "frame_sources", {}) or {}
    runners = getattr(app.state, "camera_runners", {}) or {}

    cameras = []
    for camera in settings.rtsp.cameras:
        entry: dict[str, Any] = {
            "id": camera.id,
            "name": camera.name,
            "direction": camera.direction,
            "enabled": camera.enabled,
            "url": camera.safe_url(),
        }

        entry["source"] = camera.source

        source = sources.get(camera.id)
        if source is not None and getattr(source, "camera", None) is not None:
            entry.update(source.status())

        runner = runners.get(camera.id)
        if runner is not None:
            entry["fps"] = runner.status()["measured_fps"]

        cameras.append(entry)

    return {
        "source": settings.stream.source,
        "total": len(cameras),
        "enabled": sum(1 for c in cameras if c["enabled"]),
        "cameras": cameras,
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

    # endpoint นี้ให้บริการเฉพาะ "โหมดเว็บแคมเดี่ยว" เท่านั้น (เฟส 2-5)
    # ส่วนเว็บแคมที่ทำหน้าที่เป็นกล้องตัวหนึ่งในระบบกล้องหลายตัว ใช้ /ws/feed แทน
    frame_source = getattr(app.state, "frame_sources", {}).get("browser")
    if not isinstance(frame_source, BrowserFrameSource) or frame_source.camera is not None:
        await websocket.send_json({
            "type": "error",
            "message": (
                f"ขณะนี้ระบบตั้งค่า FRAME_SOURCE={settings.stream.source} "
                "จึงไม่รับภาพทาง /ws/detect\n"
                "ถ้าต้องการใช้เว็บแคมเป็นกล้องตัวหนึ่งในระบบกล้องหลายตัว "
                "ให้ตั้ง CAMERA_<ID>_SOURCE=browser แล้วส่งภาพไปที่ /ws/feed?camera=<id> แทน"
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


# =============================================================================
# WebSocket: เบราว์เซอร์ป้อนภาพเว็บแคมเข้ามาเป็น "กล้องตัวหนึ่ง" (เฟส 8)
# =============================================================================
#
# ใช้กับกล้องที่ตั้ง CAMERA_<ID>_SOURCE=browser เท่านั้น
#
# ต่างจาก /ws/detect ตรงที่ endpoint นี้ **ไม่ส่งผลตรวจจับกลับไป**
# หน้าที่มีแค่รับภาพแล้วหย่อนลงแหล่งภาพของกล้องตัวนั้น
# ส่วนภาพกับผลตรวจจะกลับไปหาหน้าเว็บทาง /ws/stream?camera=<id> เหมือนกล้อง IP ทุกตัว
#
#     เบราว์เซอร์ --(/ws/feed)--> BrowserFrameSource --> CameraRunner --> AI
#          ^                                                              |
#          '-------------------(/ws/stream)-----------------------------'
#
# ทำไมถึงวนกลับแบบนี้แทนที่จะให้เบราว์เซอร์วาดกรอบเองจากภาพในเครื่อง:
#
#   1. ภาพที่ผู้ใช้เห็น คือภาพเฟรมเดียวกับที่ AI ตรวจจริง ๆ เสมอ
#      ไม่มีทางที่กรอบจะไปวาดทับภาพคนละเฟรมกัน
#   2. หน้าเว็บมองกล้องสองตัวเหมือนกันเป๊ะ ใช้โค้ดแสดงผลชุดเดียว
#      ไม่ต้องมี if แยกชนิดกล้องกระจายไปทั่ว
#   3. พอกล้อง Tapo ตัวที่สองมาถึง เปลี่ยนแค่ .env แล้วหน้าเว็บไม่ต้องแก้เลย
#
# ต้นทุนคือภาพเดินทางขึ้นแล้วลงสองเที่ยว ซึ่งยอมรับได้เพราะเป็นการทดแทนชั่วคราว
# และเครื่องที่เปิดหน้าเว็บกับ backend มักอยู่ในวง LAN เดียวกัน
# =============================================================================


@app.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket) -> None:
    """รับภาพเว็บแคมจากเบราว์เซอร์ เข้าเป็นเฟรมของกล้องที่ระบุ

    รูปแบบข้อความเข้า : binary = [frame_id uint32 LE][JPEG bytes] (เหมือน /ws/detect)
    รูปแบบข้อความออก : JSON {"type": "ack", "frame_id": n}

    ต้องตอบ ack กลับไปทุกเฟรม เพื่อให้เบราว์เซอร์ใช้คุมจังหวะ (backpressure)
    คือส่งเฟรมถัดไปเมื่อเฟรมก่อนหน้าถึงปลายทางแล้วเท่านั้น
    ถ้าไม่มี ack เบราว์เซอร์จะยิงตามนาฬิกาของตัวเองโดยไม่รู้ว่าปลายทางรับทันไหม
    แล้วเฟรมจะไปกองอยู่ในบัฟเฟอร์ของ WebSocket จนภาพช้ากว่าความจริงเรื่อย ๆ
    """
    await websocket.accept()

    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    sources = getattr(app.state, "frame_sources", {}) or {}
    requested = websocket.query_params.get("camera")

    # ---- เลือกกล้องปลายทาง ----
    if not requested:
        await websocket.send_json({
            "type": "error",
            "message": "ต้องระบุกล้องปลายทาง เช่น /ws/feed?camera=door_out",
        })
        await websocket.close(code=1008)
        return

    source = sources.get(requested)

    # ต้องเป็นกล้องที่ตั้งไว้ว่ารับภาพจากเบราว์เซอร์เท่านั้น
    # ถ้าปล่อยให้ป้อนภาพเข้ากล้อง IP ได้ ภาพจากเว็บแคมจะไปทับภาพกล้องจริง
    # ซึ่งเป็นช่องโหว่ที่อันตรายมากสำหรับระบบบันทึกคนเข้า-ออก
    if not isinstance(source, BrowserFrameSource) or source.camera is None:
        known = [
            cid for cid, s in sources.items()
            if isinstance(s, BrowserFrameSource) and s.camera is not None
        ]
        await websocket.send_json({
            "type": "error",
            "message": (
                f"กล้อง {requested!r} ไม่ได้ตั้งค่าให้รับภาพจากเบราว์เซอร์\n"
                f"กล้องที่รับได้ตอนนี้: {', '.join(known) or 'ไม่มีเลย'}\n"
                f"(ตั้ง CAMERA_{requested.upper()}_SOURCE=browser ในไฟล์ .env)"
            ),
        })
        await websocket.close(code=1011)
        return

    if not face_detector.is_loaded:
        reason = getattr(app.state, "face_model_error", None) or "โมเดลยังไม่ถูกโหลด"
        await websocket.send_json({
            "type": "error",
            "message": f"ระบบตรวจจับใบหน้าไม่พร้อม: {reason}",
        })
        await websocket.close(code=1011)
        return

    source.attach_feeder()
    logger.info(
        "WebSocket /ws/feed เริ่มป้อนภาพให้กล้อง %s จาก %s", source.camera.id, client
    )

    # บอกเบราว์เซอร์ว่าพร้อมรับแล้ว จะได้เริ่มส่งเฟรมแรกได้เลย
    await websocket.send_json({
        "type": "ready",
        "camera": source.camera.id,
        "camera_name": source.camera.name,
        "direction": source.camera.direction,
    })

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

            try:
                # แค่หย่อนเฟรมลงแหล่งภาพ ไม่ประมวลผลตรงนี้
                # CameraRunner ของกล้องตัวนี้จะมาหยิบไปตรวจตามจังหวะของมันเอง
                # (ซึ่งเข้าคิวร่วมกับกล้อง IP อยู่แล้ว)
                source.submit(frame_id, message[FRAME_HEADER_SIZE:])
            except FrameDecodeError as exc:
                logger.warning("เฟรม %d ของกล้อง %s ถอดรหัสไม่ได้: %s",
                               frame_id, source.camera.id, exc)
                await websocket.send_json({
                    "type": "error",
                    "frame_id": frame_id,
                    "message": str(exc),
                })
                continue

            await websocket.send_json({"type": "ack", "frame_id": frame_id})

    except WebSocketDisconnect:
        logger.info("WebSocket /ws/feed หยุดป้อนภาพกล้อง %s (%s)", source.camera.id, client)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WebSocket /ws/feed เกิดข้อผิดพลาด: %s", exc)
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass
    finally:
        # ต้องลดตัวนับเสมอ ไม่งั้นหน้า health จะรายงานว่ายังมีคนป้อนภาพอยู่ตลอดไป
        # ทั้งที่ปิดแท็บไปแล้ว ทำให้แยกไม่ออกว่ากล้องหลุดเพราะอะไร
        source.detach_feeder()


# =============================================================================
# WebSocket: backend push ภาพจากกล้อง IP ไปให้หน้าเว็บ (เฟส 6 + 7)
# =============================================================================
#
# ต่างจาก /ws/detect ตรงทิศทางของภาพ:
#     /ws/detect  เบราว์เซอร์ -> backend  (ภาพมาจากเว็บแคมของผู้ใช้)
#     /ws/stream  backend -> เบราว์เซอร์  (ภาพมาจากกล้อง IP ที่ backend ต่ออยู่)
#
# ตั้งแต่เฟส 7 เป็นต้นมา endpoint นี้ "ไม่ได้ประมวลผลอะไรเองเลย"
# หน้าที่ทั้งหมดคือไปสมัครรับภาพจาก CameraRunner ของกล้องตัวนั้น
# แล้วส่งต่อให้เบราว์เซอร์
#
# เหตุผล: CameraRunner ตัวเดียวทำงานให้ผู้ชมทุกคน
# ถ้าให้แต่ละการเชื่อมต่อประมวลผลเอง ผู้ดูแลสองคนเปิดดูพร้อมกัน
# จะกลายเป็นรัน AI ซ้ำสองชุดกับภาพเดียวกัน ซึ่งเปลืองเครื่องโดยไม่จำเป็น
# (รายละเอียดโครงสร้างอยู่ใน core/stream_runner.py)
# =============================================================================


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket) -> None:
    """ส่งภาพสดจากกล้อง IP พร้อมผลตรวจจับไปให้หน้าเว็บ

    เลือกกล้องด้วย query string เช่น /ws/stream?camera=door_in
    ถ้าไม่ระบุจะใช้กล้องตัวแรกที่เปิดใช้งานอยู่

    ============================================================================
    เฟส 8: หนึ่งช่อง WebSocket ต่อหนึ่งกล้อง
    ============================================================================

    หน้าเว็บที่แสดงสองจอจะเปิด endpoint นี้สองครั้ง (camera=door_in และ door_out)
    เลือกแบบนี้แทนการยัดสองกล้องลงช่องเดียว เพราะ:

        - กล้องตัวหนึ่งหลุด ช่องของอีกตัวไม่ต้องรู้เรื่องเลย
          ไม่มีทางที่ปัญหาของกล้องหนึ่งจะไปหยุดภาพของอีกตัว
        - คิวของผู้ชม (backpressure) แยกกันตามธรรมชาติ
          ผู้ชมที่รับกล้องหนึ่งไม่ทัน จะไม่ทำให้ภาพของกล้องอีกตัวกระตุกตาม

    แต่กฎเดิมยังอยู่ครบไม่เปลี่ยน: **ภาพกับผลตรวจของเฟรมนั้นไปด้วยกันในข้อความเดียว**
    และทุกข้อความระบุ camera ไว้ชัดเจน หน้าเว็บจึงตรวจได้ว่าไม่ได้วาดผิดจอ
    """
    await websocket.accept()

    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    runners = getattr(app.state, "camera_runners", {}) or {}

    # ---- ตรวจความพร้อมก่อนเริ่มส่ง ----
    if settings.stream.source != "rtsp":
        await websocket.send_json({
            "type": "error",
            "message": (
                f"ขณะนี้ตั้งค่า FRAME_SOURCE={settings.stream.source} "
                "ระบบจึงรับภาพจากเบราว์เซอร์ ไม่ได้ต่อกล้อง IP "
                "(ตั้งเป็น rtsp ในไฟล์ .env เพื่อใช้กล้อง IP)"
            ),
        })
        await websocket.close(code=1011)
        return

    if not face_detector.is_loaded:
        reason = getattr(app.state, "face_model_error", None) or "โมเดลยังไม่ถูกโหลด"
        await websocket.send_json({"type": "error", "message": f"ระบบตรวจจับใบหน้าไม่พร้อม: {reason}"})
        await websocket.close(code=1011)
        return

    # ---- เลือกกล้อง ----
    requested = websocket.query_params.get("camera")

    if requested:
        runner = runners.get(requested)
        if runner is None:
            await websocket.send_json({
                "type": "error",
                "message": (
                    f"ไม่พบกล้องชื่อ {requested!r} "
                    f"(กล้องที่เปิดใช้งานอยู่: {', '.join(runners) or 'ไม่มีเลย'})"
                ),
            })
            await websocket.close(code=1011)
            return
    else:
        runner = next(iter(runners.values()), None)
        if runner is None:
            await websocket.send_json({
                "type": "error",
                "message": "ไม่มีกล้องที่เปิดใช้งานอยู่เลย (ตรวจ CAMERA_IDS ในไฟล์ .env)",
            })
            await websocket.close(code=1011)
            return

    camera_id = runner.camera_id
    source = runner.source
    logger.info("WebSocket /ws/stream เชื่อมต่อจาก %s ดูกล้อง %s", client, camera_id)

    queue = runner.subscribe()
    last_alive = True

    try:
        while True:
            # ---- กล้องหลุด: แจ้งหน้าเว็บทันที ไม่ปล่อยให้ภาพค้างเงียบ ๆ ----
            alive = source.is_alive()

            if not alive:
                if last_alive:
                    logger.warning("กล้อง %s ไม่ส่งภาพแล้ว แจ้งหน้าเว็บ", camera_id)
                    last_alive = False
                await websocket.send_json({
                    "type": "camera_status",
                    "camera": camera_id,
                    "camera_name": runner.camera.name,
                    "direction": runner.camera.direction,
                    "alive": False,
                    "status": source.status(),
                })
                await asyncio.sleep(1.0)
                continue

            if not last_alive:
                logger.info("กล้อง %s กลับมาส่งภาพแล้ว", camera_id)
                await websocket.send_json({
                    "type": "camera_status",
                    "camera": camera_id,
                    "camera_name": runner.camera.name,
                    "direction": runner.camera.direction,
                    "alive": True,
                    "status": source.status(),
                })
            last_alive = True

            # ---- รอเฟรมถัดไปจากตัวขับสตรีม ----
            # ตั้ง timeout ไว้เพื่อให้วนกลับไปเช็คสถานะกล้องได้เรื่อย ๆ
            # แม้ตอนที่ไม่มีเฟรมเข้ามาเลย (เช่นกล้องเพิ่งหลุด)
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            await websocket.send_bytes(message)

    except WebSocketDisconnect:
        logger.info("WebSocket /ws/stream ตัดการเชื่อมต่อ (%s)", client)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WebSocket /ws/stream เกิดข้อผิดพลาด: %s", exc)
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass
    finally:
        # ต้องยกเลิกการสมัครรับเสมอ ไม่งั้นคิวจะค้างอยู่ใน runner
        # แล้ว runner จะเข้าใจผิดว่ายังมีคนดูอยู่ ทำให้เข้ารหัสภาพต่อไปเรื่อย ๆ
        runner.unsubscribe(queue)
