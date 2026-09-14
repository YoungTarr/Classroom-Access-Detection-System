"""ตัวตรวจจับใบหน้า - InsightFace (SCRFD) รันบน onnxruntime

ข้อกำหนดสำคัญ 2 ข้อของไฟล์นี้:

1. **โหลดโมเดลครั้งเดียวตอนสตาร์ท** ไม่ใช่โหลดใหม่ทุกเฟรม
   การสร้าง onnxruntime session ใหม่กินเวลาเป็นวินาที ถ้าทำทุกเฟรมคือใช้งานไม่ได้เลย

2. **ห้ามดาวน์โหลดโมเดลตอน runtime**
   ปกติ insightface จะไปโหลดไฟล์จาก GitHub ให้อัตโนมัติถ้าหาไม่เจอ ซึ่งเป็นปัญหาเพราะ
     - สตาร์ทครั้งแรกค้างนานโดยไม่มีใครรู้ว่ากำลังโหลดอะไรอยู่
     - Pi ที่ไม่ได้ต่อเน็ตจะพังโดยไม่รู้สาเหตุ
     - ได้เวอร์ชันโมเดลไม่ตรงกันระหว่างเครื่อง dev กับเครื่องจริง
   ไฟล์นี้จึงตรวจสอบก่อนว่ามีไฟล์ .onnx อยู่จริงไหม ถ้าไม่มีให้ล้มเหลวทันที
   พร้อมบอกวิธีแก้ และส่ง "path ของโฟลเดอร์" ให้ insightface โดยตรง
   ซึ่งเป็นเส้นทางที่ตัวมันจะไม่แตะอินเทอร์เน็ตเลย
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)


class FaceModelError(RuntimeError):
    """โหลดโมเดลไม่สำเร็จ - ต้องหยุดและรายงาน ไม่ทำงานต่อแบบครึ่ง ๆ กลาง ๆ"""


@dataclass(frozen=True)
class DetectedFace:
    """ใบหน้าหนึ่งใบที่ตรวจเจอในเฟรม"""

    # กรอบสี่เหลี่ยมในระบบพิกัดของ "ภาพที่ส่งเข้ามาตรวจ"
    # เก็บเป็น x, y, w, h (มุมซ้ายบน + ความกว้าง/สูง) ตามที่ frontend ต้องใช้วาด
    x: int
    y: int
    w: int
    h: int

    # ความมั่นใจของตัวตรวจจับ 0.0-1.0
    score: float

    # จุดสังเกตบนใบหน้า 5 จุด (ตา 2 จมูก 1 มุมปาก 2)
    # เก็บไว้เพราะเฟส 4 ต้องใช้จัดหน้าให้ตรงก่อนสกัด embedding
    keypoints: np.ndarray | None = None

    def as_dict(self) -> dict:
        """แปลงเป็นรูปแบบที่ส่งกลับไปให้เบราว์เซอร์"""
        return {
            "bbox": [self.x, self.y, self.w, self.h],
            "score": round(float(self.score), 4),
        }


class FaceDetector:
    """ห่อ InsightFace FaceAnalysis ไว้ พร้อมคุมเรื่องการโหลดโมเดล"""

    def __init__(self) -> None:
        self._app = None  # insightface.app.FaceAnalysis
        self._loaded = False

        # onnxruntime รันหนึ่งงานต่อครั้งพอ ถ้าเปิดหลายแท็บพร้อมกันแล้วปล่อยให้
        # ยิงเข้ามาพร้อมกัน CPU จะแย่งกันจนช้าลงทั้งคู่ จึงล็อกให้เข้าคิวกัน
        self._lock = threading.Lock()

        # สถิติไว้แสดงในหน้า health
        self._total_detections = 0
        self._last_process_ms: float | None = None

    # ------------------------------------------------------------------
    # การโหลดโมเดล
    # ------------------------------------------------------------------
    def load(self) -> None:
        """โหลดโมเดลเข้าหน่วยความจำ เรียกครั้งเดียวตอนแอปสตาร์ท"""
        if self._loaded:
            return

        cfg = settings.face
        model_path = cfg.model_path

        self._verify_model_files(model_path)

        # import ตรงนี้ไม่ใช่บนหัวไฟล์ เพราะ insightface ใช้เวลา import นานพอสมควร
        # และจะได้ไม่ทำให้โมดูลอื่นที่ import ไฟล์นี้ต้องรอไปด้วย
        import onnxruntime
        from insightface.app import FaceAnalysis

        logger.info("กำลังโหลดโมเดลใบหน้า: %s", model_path)
        started = time.monotonic()

        sess_options = onnxruntime.SessionOptions()
        if cfg.num_threads > 0:
            # จำกัดจำนวน thread เพื่อไม่ให้กิน CPU จนงานอื่นไม่เหลือ (สำคัญบน Pi)
            sess_options.intra_op_num_threads = cfg.num_threads

        try:
            # ส่ง "path ของโฟลเดอร์" เข้าไปตรง ๆ แทนการส่งแค่ชื่อชุดโมเดล
            # insightface จะใช้โฟลเดอร์นี้ทันทีโดยไม่ตรวจสอบ/ดาวน์โหลดจากอินเทอร์เน็ต
            #
            # ชื่อ keyword ต้องเป็น sess_options (ไม่ใช่ session_options)
            # เพราะ insightface ส่ง kwargs ที่เหลือต่อเข้า onnxruntime.InferenceSession ตรง ๆ
            # โหลดเฉพาะโมดูล detection เท่านั้น
            #
            # เหตุผลสำคัญ: FaceAnalysis.get() จะรัน "ทุกโมดูลที่โหลดไว้" กับทุกใบหน้า
            # ถ้าโหลด recognition มาด้วย มันจะสกัด embedding ให้ทุกหน้าทุกเฟรม
            # ซึ่งวัดจริงแล้วทำให้ช้าจาก ~150 ms เป็น ~1000 ms ต่อเฟรม (ภาพ 6 คน)
            # ทั้งที่เฟส 2 ยังไม่ต้องใช้ embedding เลย
            #
            # เฟส 4 จะโหลดโมเดล recognition แยกไว้ที่ face/recognizer.py
            # แล้วเรียกเฉพาะตอนที่ต้องระบุตัวตนจริง ๆ ไม่ใช่ทุกเฟรม
            self._app = FaceAnalysis(
                name=str(model_path),
                allowed_modules=["detection"],
                providers=["CPUExecutionProvider"],
                sess_options=sess_options,
            )
            self._app.prepare(
                ctx_id=-1,  # -1 = ใช้ CPU (ไม่มี GPU ทั้งบน Pi และบนเครื่อง dev)
                det_thresh=cfg.det_thresh,
                det_size=(cfg.det_size, cfg.det_size),
            )
        except Exception as exc:  # noqa: BLE001 - ต้องห่อให้เป็นข้อความที่อ่านรู้เรื่อง
            raise FaceModelError(
                f"โหลดโมเดลจาก {model_path} ไม่สำเร็จ: {exc}"
            ) from exc

        elapsed = time.monotonic() - started
        self._loaded = True

        logger.info(
            "โหลดโมเดลสำเร็จใน %.2f วินาที (ชุด=%s, det_size=%d, det_thresh=%.2f, threads=%s)",
            elapsed,
            cfg.model_pack,
            cfg.det_size,
            cfg.det_thresh,
            cfg.num_threads or "อัตโนมัติ",
        )

    @staticmethod
    def _verify_model_files(model_path: Path) -> None:
        """ยืนยันว่ามีไฟล์โมเดลอยู่จริงก่อนจะเรียก insightface

        ขั้นตอนนี้คือหัวใจของกฎ "ห้ามดาวน์โหลดตอน runtime"
        ถ้าไม่ตรวจตรงนี้ insightface จะเงียบ ๆ ไปโหลดจากเน็ตให้เอง
        """
        if not model_path.exists():
            raise FaceModelError(
                f"ไม่พบโฟลเดอร์โมเดล: {model_path}\n"
                f"โมเดลต้องถูกโหลดฝังไว้ใน image ตั้งแต่ตอน build แล้ว\n"
                f"วิธีแก้: ตรวจว่าค่า FACE_MODEL_PACK ใน .env "
                f"ตรงกับ --build-arg FACE_MODEL_PACK ที่ใช้ตอน build image หรือไม่ "
                f"แล้วสั่ง docker compose build backend ใหม่"
            )

        onnx_files = sorted(model_path.glob("*.onnx"))
        if not onnx_files:
            raise FaceModelError(
                f"โฟลเดอร์ {model_path} มีอยู่ แต่ไม่มีไฟล์ .onnx อยู่ข้างใน\n"
                f"อาจเป็นเพราะแตกไฟล์ zip ไม่สมบูรณ์ "
                f"ลองสั่ง docker compose build --no-cache backend"
            )

        logger.info(
            "พบไฟล์โมเดล %d ไฟล์: %s",
            len(onnx_files),
            ", ".join(f.name for f in onnx_files),
        )

    # ------------------------------------------------------------------
    # การตรวจจับ
    # ------------------------------------------------------------------
    def detect(self, image: np.ndarray) -> list[DetectedFace]:
        """ตรวจจับใบหน้าทั้งหมดในภาพหนึ่งภาพ (ภาพเป็น BGR ตามมาตรฐาน OpenCV)

        เมธอดนี้ใช้เวลานาน (หลักสิบถึงร้อย ms) ผู้เรียกที่เป็น async
        ต้องเรียกผ่าน thread แยก ไม่งั้นจะบล็อก event loop ทั้งเซิร์ฟเวอร์
        """
        if not self._loaded or self._app is None:
            raise FaceModelError("ยังไม่ได้โหลดโมเดล - ต้องเรียก FaceDetector.load() ก่อน")

        started = time.monotonic()

        with self._lock:
            faces = self._app.get(image)

        self._last_process_ms = (time.monotonic() - started) * 1000.0

        height, width = image.shape[:2]
        results: list[DetectedFace] = []

        for face in faces:
            # insightface คืน bbox เป็น (x1, y1, x2, y2) แบบทศนิยม
            # เราแปลงเป็นจำนวนเต็ม (x, y, w, h) และตัดไม่ให้ล้นขอบภาพ
            x1, y1, x2, y2 = (float(v) for v in face.bbox)

            x = max(0, int(round(x1)))
            y = max(0, int(round(y1)))
            x2i = min(width, int(round(x2)))
            y2i = min(height, int(round(y2)))

            w = x2i - x
            h = y2i - y

            # กรอบที่ถูกตัดจนไม่เหลือพื้นที่ (หน้าอยู่นอกภาพเกือบหมด) ให้ข้ามไป
            if w <= 0 or h <= 0:
                continue

            results.append(
                DetectedFace(
                    x=x,
                    y=y,
                    w=w,
                    h=h,
                    score=float(getattr(face, "det_score", 0.0)),
                    keypoints=getattr(face, "kps", None),
                )
            )

        self._total_detections += len(results)
        return results

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def status(self) -> dict:
        """ข้อมูลสรุปสำหรับหน้า /api/health"""
        cfg = settings.face
        return {
            "loaded": self._loaded,
            "model_pack": cfg.model_pack,
            "model_path": str(cfg.model_path),
            "det_size": cfg.det_size,
            "det_thresh": cfg.det_thresh,
            "num_threads": cfg.num_threads or None,
            "total_detections": self._total_detections,
            "last_process_ms": (
                round(self._last_process_ms, 1) if self._last_process_ms else None
            ),
        }


# instance เดียวใช้ร่วมกันทั้งแอป โหลดโมเดลใน lifespan ของ FastAPI
face_detector = FaceDetector()
