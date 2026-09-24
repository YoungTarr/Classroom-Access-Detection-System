"""ตัวขับสตรีมของกล้องหนึ่งตัว - แยกอัตราการทำงานสามระดับ (เฟส 7)

============================================================================
โครงสร้าง
============================================================================

    RTSPFrameSource (thread)      อ่านจากกล้องที่ CAPTURE_FPS
            |                     เก็บแค่เฟรมล่าสุด
            v
    _detect_loop (async task)     ตรวจจับ+จดจำที่ DETECT_FPS (ต่ำ เช่น 5)
            |                     งานหนักทำใน thread แยก
            v
    _stream_loop (async task)     เข้ารหัส JPEG + ส่งที่ STREAM_FPS (สูง เช่น 15)
            |                     งานเข้ารหัสก็ทำใน thread แยกเช่นกัน
            v
    ผู้ชมทุกคนที่เปิดดูกล้องตัวนี้

============================================================================
สองเรื่องสำคัญที่โครงสร้างนี้แก้
============================================================================

1. **ภาพลื่นขึ้นโดยไม่ต้องเร่ง AI**
   เดิมทุกอย่างเดินจังหวะเดียวกัน หน้าเว็บจึงได้ภาพเท่าความเร็วของ AI (~8 fps)
   ตอนนี้ภาพวิ่งที่ STREAM_FPS ส่วน AI เดินช้ากว่าได้ตามสบาย
   เฟรมที่ยังไม่มีผลตรวจใหม่จะถูกทำเครื่องหมาย is_fresh=false
   แล้วหน้าเว็บจะ interpolate กรอบต่อไปเอง (กลไกจากเฟส 3)

2. **AI ทำงานชุดเดียวต่อกล้อง ไม่ว่าจะมีคนเปิดดูกี่คน**
   เดิมสร้าง pipeline ใหม่ทุกครั้งที่มีคนเปิดหน้าเว็บ
   ผู้ดูแลสองคนเปิดพร้อมกัน = รัน AI ซ้ำสองชุดกับภาพเดียวกัน
   ตอนนี้ตัวขับตัวเดียวทำงานให้ทุกคน ผู้ชมแค่มา "สมัครรับ" ภาพที่ส่งออกไป

============================================================================
เฟส 8: มีตัวขับหนึ่งตัวต่อหนึ่งกล้อง เดินขนานกันไป
============================================================================

    CameraRunner(1 ขาเข้า)  ---.
                                >--- DetectScheduler (ประตูบานเดียว) ---> โมเดล AI
    CameraRunner(2 ขาออก)   ---'                                          (ชุดเดียว)

ทุกอย่างที่เป็น "สถานะของกล้อง" แยกกันคนละชุด (thread อ่านกล้อง, tracker, สถิติ)
ส่วนทุกอย่างที่เป็น "โมเดล" ใช้ร่วมกันชุดเดียว (ดูรายละเอียดในหัวคลาส CameraRunner)
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.core.detect_scheduler import DetectScheduler
from app.core.frame_source import FrameSource
from app.core.pipeline import Pipeline
from app.core.rate_meter import RateMeter
from app.core.tracker import tracks_to_dicts
from app.identify.base import Identifier

logger = logging.getLogger(__name__)

# หัวข้อความ: ความยาวของส่วน JSON เป็น uint32 little-endian
STREAM_HEADER_FORMAT = "<I"
STREAM_HEADER_SIZE = struct.calcsize(STREAM_HEADER_FORMAT)


def encode_stream_message(meta: dict[str, Any], jpeg: bytes) -> bytes:
    """ประกอบข้อความ binary ก้อนเดียวจากข้อมูลกำกับและภาพ

        [ 4 ไบต์ ][ ..... JSON ..... ][ ..... JPEG ..... ]
         ความยาว JSON

    ที่รวมเป็นก้อนเดียวแทนการส่งสองข้อความ เพราะภาพกับผลตรวจต้องคู่กันเสมอ
    ถ้าแยกส่งแล้วมีอะไรพลาดกลางทาง หน้าเว็บจะเอาผลของเฟรมหนึ่ง
    ไปวาดทับภาพของอีกเฟรมหนึ่งโดยไม่มีใครรู้
    """
    payload = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    return struct.pack(STREAM_HEADER_FORMAT, len(payload)) + payload + jpeg


@dataclass
class DetectionSnapshot:
    """ผลตรวจจับหนึ่งชุด ใช้ร่วมกันโดยผู้ชมทุกคน

    แปลงเป็น dict ไว้ตั้งแต่ต้นทาง เพราะจะถูกส่งซ้ำหลายเฟรมและหลายคน
    ถ้าแปลงใหม่ทุกครั้งจะเสียแรงเปล่า
    """

    frame_id: int
    tracks: list[dict] = field(default_factory=list)
    detected_count: int = 0
    source_size: tuple[int, int] = (0, 0)
    detect_ms: float = 0.0
    track_ms: float = 0.0
    identify_ms: float = 0.0
    created_at: float = 0.0


class CameraRunner:
    """ตัวขับสตรีมของกล้องหนึ่งตัว

    ============================================================================
    อะไรเป็นของกล้องตัวนี้ อะไรใช้ร่วมกับกล้องตัวอื่น (เฟส 8)
    ============================================================================

    ของกล้องตัวนี้คนเดียว (สร้างใหม่ทุกครั้งที่เพิ่มกล้อง):
        source      thread อ่านกล้อง + watchdog ของตัวเอง
        pipeline    เพราะตัวติดตาม (tracker) จำ track ไว้ข้างใน
                    ถ้าใช้ร่วมกัน ใบหน้าจากคนละกล้องจะถูกจับคู่กันมั่ว
                    คนที่เดินผ่านกล้องขาเข้าจะกลายเป็น track เดียวกับคนที่กล้องขาออก
        มาตรวัด     สถิติต้องแยกกัน ไม่งั้นดูไม่ออกว่ากล้องตัวไหนมีปัญหา
        ผู้ชม       คนที่เปิดดูกล้องตัวนี้

    ใช้ร่วมกันทุกกล้อง (**ห้ามสร้างใหม่ต่อกล้องเด็ดขาด**):
        identifier  ข้างในมีทั้งโมเดลจดจำใบหน้าและ FAISS index
                    ถ้าสร้างแยกต่อกล้องจะกิน RAM เพิ่มหลายร้อย MB โดยเปล่าประโยชน์
                    ซึ่ง Pi 5 ไม่มีให้เสีย เพราะต้องแบ่งให้ PostgreSQL
                    และการถอดรหัสวิดีโอสองสตรีมด้วย
                    ที่แชร์ได้เพราะคลังเวกเตอร์เป็นข้อมูล "อ่านอย่างเดียว"
                    ส่วนที่เป็นสถานะคือผลโหวต ซึ่งเก็บอยู่ใน track ของแต่ละ pipeline
        detector    โมเดลตรวจจับ เป็น instance เดียวระดับโมดูล (face/detector.py)
                    Pipeline รับมาใช้ต่อ ไม่ได้สร้างใหม่
        scheduler   ประตูคิวตรวจจับ ทำให้สองกล้องผลัดกันใช้ CPU แทนที่จะแย่งกัน
    """

    def __init__(
        self,
        source: FrameSource,
        identifier: Identifier | None,
        scheduler: DetectScheduler,
    ) -> None:
        # รับ FrameSource แบบกว้าง ๆ ไม่เจาะจงว่าเป็น RTSP โดยตั้งใจ
        # RTSP กับตัวทดแทนอื่นใช้ interface เดียวกัน โค้ดในคลาสนี้ใช้แค่
        # source.read() กับ source.camera จึงไม่ต้องรู้ว่าเป็นชนิดไหน
        self.source = source
        self.camera = source.camera
        self.camera_id = source.camera.id

        # ประตูคิวที่ใช้ร่วมกับกล้องตัวอื่น (ดูคำอธิบายใน core/detect_scheduler.py)
        self.scheduler = scheduler

        # pipeline ตัวเดียวต่อกล้อง (ตัวติดตามจำ track ไว้ข้างใน จึงห้ามใช้ร่วมข้ามกล้อง)
        # แต่ตัวตรวจจับและตัวระบุตัวตนที่อยู่ข้างใน pipeline เป็นตัวที่แชร์กันทั้งระบบ
        self.pipeline = Pipeline(identifier=identifier)

        self._detect_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._stopping = False

        # ผลตรวจล่าสุดที่ผู้ชมทุกคนใช้ร่วมกัน
        self._latest_detection: DetectionSnapshot | None = None

        # คิวของผู้ชมแต่ละคน ขนาด 1 เพราะถ้าใครรับช้าให้ทิ้งเฟรมเก่าไปเลย
        # ไม่ปล่อยให้คิวยาวจนภาพของคนนั้นช้ากว่าความจริง (หลักการเดียวกับ frame_source)
        self._subscribers: set[asyncio.Queue] = set()

        # ตัวนับเฟรมที่ส่งออก ใช้เป็น frame_id ของสตรีม
        self._stream_frame_id = 0

        # เฟรมสตรีมที่ผ่านไปแล้วนับตั้งแต่ได้ผลตรวจชุดล่าสุด
        self._frames_since_detection = 0

        # มาตรวัดอัตราจริงของทั้งสามขั้น (ของกล้องตัวนี้เท่านั้น)
        self.capture_meter = RateMeter()
        self.detect_meter = RateMeter()
        self.stream_meter = RateMeter()

        self._last_capture_frame_id = -1

        # เวลาที่ใช้แต่ละขั้นในรอบล่าสุด เก็บไว้รายงานทาง /api/health
        # (ในข้อความ WebSocket มีอยู่แล้ว แต่ health ต้องดูได้โดยไม่ต้องเปิดหน้าเว็บ)
        self._last_detect_ms = 0.0
        self._last_identify_ms = 0.0
        self._last_latency_ms = 0.0

    # ------------------------------------------------------------------
    # วงจรชีวิต
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._detect_task is not None:
            return
        self._stopping = False
        self._detect_task = asyncio.create_task(
            self._detect_loop(), name=f"detect-{self.camera_id}"
        )
        self._stream_task = asyncio.create_task(
            self._stream_loop(), name=f"stream-{self.camera_id}"
        )
        logger.info(
            "เริ่มตัวขับสตรีมกล้อง %s (capture=%d detect=%d/กล้อง stream=%d fps, "
            "เพดานตรวจจับรวมทั้งระบบ %d fps)",
            self.camera_id,
            settings.rates.capture_fps,
            settings.rates.detect_fps,
            settings.rates.stream_fps,
            self.scheduler.total_fps,
        )

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._detect_task, self._stream_task):
            if task is not None:
                task.cancel()
        for task in (self._detect_task, self._stream_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._detect_task = None
        self._stream_task = None
        logger.info("หยุดตัวขับสตรีมกล้อง %s", self.camera_id)

    # ------------------------------------------------------------------
    # การสมัครรับภาพ
    # ------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        """ขอคิวรับภาพหนึ่งอัน สำหรับผู้ชมหนึ่งคน"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        logger.info(
            "มีผู้ชมเข้าดูกล้อง %s (รวม %d คน)", self.camera_id, len(self._subscribers)
        )
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        logger.info(
            "ผู้ชมออกจากกล้อง %s (เหลือ %d คน)", self.camera_id, len(self._subscribers)
        )

    @property
    def viewer_count(self) -> int:
        return len(self._subscribers)

    # ------------------------------------------------------------------
    # ลูปตรวจจับ (ช้า)
    # ------------------------------------------------------------------
    async def _detect_loop(self) -> None:
        """ตรวจจับ+จดจำใบหน้าที่อัตรา DETECT_FPS (ต่อกล้อง)

        ทำงานแยกจากการส่งภาพโดยสิ้นเชิง ถ้าลูปนี้ช้าลง ภาพที่หน้าเว็บ
        ก็ยังลื่นเท่าเดิม แค่กรอบจะอัปเดตห่างขึ้นเท่านั้น

        ตั้งแต่เฟส 8 ก่อนจะตรวจต้องขอคิวจาก DetectScheduler ที่แชร์กับกล้องตัวอื่น
        เพื่อให้สองกล้องผลัดกันใช้ CPU ทีละตัว ไม่ใช่แย่งกันจนช้าลงทั้งคู่
        """
        interval = 1.0 / max(1, settings.rates.detect_fps)
        last_frame_id = -1

        while not self._stopping:
            started = time.monotonic()

            try:
                frame = self.source.read()

                # ตรวจเฉพาะเฟรมใหม่ ถ้ายังเป็นเฟรมเดิมก็ไม่ต้องเสียแรงตรวจซ้ำ
                #
                # **ขอคิวหลังจากเห็นว่ามีของทำจริงแล้วเท่านั้น**
                # ถ้าขอคิวไว้ก่อนแล้วค่อยมาพบว่าไม่มีเฟรมใหม่ กล้องที่ถูกถอดปลั๊ก
                # จะคอยจองคิวเปล่า ๆ แล้วถ่วงกล้องที่ยังทำงานอยู่ (ผิดข้อ 3 ของเฟสนี้)
                if frame is not None and frame.frame_id != last_frame_id:
                    last_frame_id = frame.frame_id

                    async with self.scheduler.slot(self.camera_id):
                        # งานหนัก - ต้องอยู่ใน thread แยก ไม่งั้นบล็อก event loop
                        # ทำให้การส่งภาพของกล้องทุกตัวและ request อื่น ๆ ค้างตามไปด้วย
                        result = await asyncio.to_thread(self.pipeline.process, frame)

                    self._latest_detection = DetectionSnapshot(
                        frame_id=result.frame_id,
                        tracks=tracks_to_dicts(result.tracks),
                        detected_count=result.detected_count,
                        source_size=result.source_size,
                        detect_ms=result.detect_ms,
                        track_ms=result.track_ms,
                        identify_ms=result.identify_ms,
                        created_at=time.monotonic(),
                    )
                    self._frames_since_detection = 0
                    self.detect_meter.tick()

                    self._last_detect_ms = result.detect_ms
                    self._last_identify_ms = result.identify_ms
                    self._last_latency_ms = (time.monotonic() - frame.received_at) * 1000.0

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - ลูปนี้ต้องไม่ตายกลางคัน
                # ต้องกลืน error ไว้ในกล้องตัวนี้ ห้ามให้ลอยออกไป
                # ไม่งั้น task ของกล้องตัวนี้จะตาย แล้วกล้องตัวนี้จะเงียบไปเฉย ๆ
                # (ส่วนกล้องตัวอื่นไม่กระทบ เพราะเป็น task แยกกันคนละตัว)
                logger.exception("ลูปตรวจจับของกล้อง %s ผิดพลาด: %s", self.camera_id, exc)

            # หน่วงให้ครบรอบ โดยหักเวลาที่ใช้ไปแล้วออก
            # (รวมเวลาที่เสียไปกับการรอคิวด้วย จึงไม่ช้าซ้ำซ้อนเมื่อมีกล้องสองตัว)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, interval - elapsed))

    # ------------------------------------------------------------------
    # ลูปส่งภาพ (เร็ว)
    # ------------------------------------------------------------------
    async def _stream_loop(self) -> None:
        """เข้ารหัสภาพเป็น JPEG แล้วส่งให้ผู้ชมทุกคน ที่อัตรา STREAM_FPS"""
        import cv2

        interval = 1.0 / max(1, settings.rates.stream_fps)
        quality = int(settings.stream.jpeg_quality * 100)
        last_frame_id = -1

        # ตัวคุมอัตราแบบ token bucket ด้วยเหตุผลเดียวกับใน frame_source
        # (เฟรมมาเป็นช่อ ถ้าวัดระยะห่างตรง ๆ จะทิ้งเฟรมทั้งที่อัตราเฉลี่ยยังไม่เกิน)
        stream_fps = max(1, settings.rates.stream_fps)
        burst_size = 2.0
        tokens = burst_size
        last_token_at = time.monotonic()

        # ตรวจดูเฟรมใหม่ถี่กว่าอัตราที่จะส่งจริง แล้วส่งทันทีที่มีของใหม่
        #
        # ถ้าหลับครบรอบเต็มแล้วค่อยตื่นมาดู จังหวะจะชนกับจังหวะที่เฟรมมาถึงพอดี
        # (aliasing) ทำให้พลาดเฟรมแล้วต้องรออีกรอบ ได้จริงเหลือครึ่งเดียว
        poll_interval = interval / 4.0

        def encode(image) -> bytes | None:
            ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            return buffer.tobytes() if ok else None

        while not self._stopping:
            try:
                # ไม่มีคนดูก็ไม่ต้องเข้ารหัสภาพให้เปลืองเครื่อง
                # (ลูปตรวจจับยังเดินอยู่ เพราะระบบต้องรู้ว่ามีใครเดินผ่านแม้ไม่มีคนเฝ้าดู)
                if self._subscribers:
                    frame = self.source.read()
                    now = time.monotonic()

                    tokens = min(burst_size, tokens + (now - last_token_at) * stream_fps)
                    last_token_at = now

                    is_new = frame is not None and frame.frame_id != last_frame_id

                    if is_new and tokens >= 1.0:
                        last_frame_id = frame.frame_id
                        tokens -= 1.0
                        self.capture_meter.tick()

                        # งานเข้ารหัสก็หนักพอควร แยก thread เช่นกัน
                        # และต้องไม่ไปบล็อกลูปตรวจจับที่เดินคู่ขนานอยู่
                        jpeg = await asyncio.to_thread(encode, frame.image)

                        if jpeg is not None:
                            self._publish(frame, jpeg)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("ลูปส่งภาพของกล้อง %s ผิดพลาด: %s", self.camera_id, exc)

            await asyncio.sleep(poll_interval)

    def _publish(self, frame, jpeg: bytes) -> None:
        """ประกอบข้อความแล้วหย่อนลงคิวของผู้ชมทุกคน"""
        self._stream_frame_id += 1
        self._frames_since_detection += 1

        detection = self._latest_detection

        # is_fresh = ผลตรวจชุดนี้เพิ่งได้มาในรอบนี้หรือเปล่า
        # หน้าเว็บจะรับผลใหม่เฉพาะตอน is_fresh=true
        # ระหว่างนั้นจะ interpolate กรอบต่อไปเองโดยไม่กระตุก
        is_fresh = detection is not None and self._frames_since_detection <= 1

        # ผลเก่าเกินไปแล้ว บอกหน้าเว็บให้ค่อย ๆ จางกรอบลง
        # เพื่อสื่อว่า "กำลังเดาตำแหน่งอยู่" ไม่ใช่ผลสด
        is_stale = (
            detection is None
            or self._frames_since_detection > settings.rates.stale_after_frames
        )

        meta: dict[str, Any] = {
            "type": "frame",
            # ทุกข้อความต้องบอกให้ชัดว่าเป็นของกล้องตัวไหน (ข้อบังคับของเฟส 8)
            # ถึงจะใช้ WebSocket แยกช่องต่อกล้องแล้วก็ยังต้องมี เพราะหน้าเว็บ
            # ต้องยืนยันได้ว่าไม่ได้เอาภาพของกล้องหนึ่งไปวาดในจออีกตัว
            "camera": self.camera_id,
            "camera_name": self.camera.name,
            "direction": self.camera.direction,
            "frame_id": self._stream_frame_id,
            "is_fresh": is_fresh,
            "is_stale": is_stale,
            "frames_since_detection": self._frames_since_detection,
            "frame_age_ms": round((time.monotonic() - frame.received_at) * 1000, 1),
            # อัตราที่ "วัดได้จริง" ของทั้งสามขั้น ไม่ใช่ค่าที่ตั้งไว้
            "fps": {
                "capture": round(self.capture_meter.fps, 1),
                "detect": round(self.detect_meter.fps, 1),
                "stream": round(self.stream_meter.fps, 1),
            },
            "viewers": len(self._subscribers),
        }

        if detection is not None:
            meta.update({
                "detections_frame_id": detection.frame_id,
                "tracks": detection.tracks,
                "detected_count": detection.detected_count,
                "source_size": list(detection.source_size),
                "detect_ms": round(detection.detect_ms, 1),
                "track_ms": round(detection.track_ms, 2),
                "identify_ms": round(detection.identify_ms, 1),
                "detection_age_ms": round((time.monotonic() - detection.created_at) * 1000, 1),
            })
        else:
            # ยังไม่เคยตรวจได้เลย - ส่งภาพไปก่อน หน้าเว็บจะได้ไม่ต้องรอ
            meta.update({
                "detections_frame_id": None,
                "tracks": [],
                "detected_count": 0,
                "source_size": list(frame.size),
            })

        message = encode_stream_message(meta, jpeg)
        self.stream_meter.tick()

        for queue in list(self._subscribers):
            # คิวเต็มแปลว่าผู้ชมคนนั้นรับไม่ทัน ให้ทิ้งเฟรมเก่าแล้วใส่ตัวใหม่แทน
            # ผู้ชมที่เน็ตช้าจะได้ภาพกระโดดบ้าง แต่จะไม่ค่อย ๆ ช้ากว่าความจริงเรื่อย ๆ
            # และที่สำคัญคือไม่ไปถ่วงผู้ชมคนอื่น
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """สถิติของกล้องตัวนี้ตัวเดียว ไม่มีค่าที่ปนกับกล้องตัวอื่นเลย"""
        return {
            "camera": self.camera_id,
            "name": self.camera.name,
            "direction": self.camera.direction,
            "viewers": len(self._subscribers),
            "configured_fps": {
                "capture": settings.rates.capture_fps,
                # detect เป็นค่า "ต่อกล้อง" ส่วนเพดานรวมอยู่ในสถานะของ scheduler
                "detect": settings.rates.detect_fps,
                "stream": settings.rates.stream_fps,
            },
            "measured_fps": {
                "capture": round(self.capture_meter.fps, 1),
                "detect": round(self.detect_meter.fps, 1),
                "stream": round(self.stream_meter.fps, 1),
            },
            # เวลาที่ใช้ในรอบตรวจล่าสุดของกล้องตัวนี้
            "detect_ms": round(self._last_detect_ms, 1),
            "identify_ms": round(self._last_identify_ms, 1),
            "latency_ms": round(self._last_latency_ms, 1),
            "processed_frames": self.pipeline.processed_frames,
        }
