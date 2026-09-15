"""สกัดลายเซ็นใบหน้า (embedding) ด้วย ArcFace บน onnxruntime

embedding คือเวกเตอร์ 512 มิติที่แทน "หน้าตา" ของคนคนหนึ่ง
ใบหน้าของคนเดียวกันจะได้เวกเตอร์ที่ชี้ไปทางเดียวกัน แม้ถ่ายคนละวัน คนละเสื้อ
ส่วนคนละคนจะชี้ไปคนละทาง เราจึงเทียบว่า "เป็นคนเดียวกันไหม" ด้วยมุมระหว่างเวกเตอร์

ทำไมต้อง normalize ให้ความยาวเป็น 1:
    เมื่อเวกเตอร์ยาวเท่ากันหมด การคูณ dot product จะเท่ากับ cosine similarity พอดี
    ทำให้ใช้ FAISS แบบ IndexFlatIP (inner product) ค้นหาได้ตรง ๆ
    และได้คะแนนที่ตีความง่ายคือ 1.0 = เหมือนกันสนิท, 0.0 = ไม่เกี่ยวกันเลย

ทำไมแยกชุดโมเดลกับตัวตรวจจับ (ดูคำอธิบายใน Dockerfile ประกอบ):
    วัดบนเครื่อง dev แล้ว w600k_r50 ใช้ ~167 ms ต่อหนึ่งใบหน้า ซึ่งหนักเกินไป
    ส่วน w600k_mbf ใช้ ~12 ms เร็วกว่า 14 เท่า และแยกคนได้ดีพอ ๆ กัน
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from app.config import settings
from app.face.detector import FaceModelError

logger = logging.getLogger(__name__)

# ขนาดภาพที่ ArcFace ต้องการ (จัดหน้าให้ตรงแล้วครอปเป็นสี่เหลี่ยมจัตุรัส)
ARCFACE_INPUT_SIZE = 112


class FaceRecognizer:
    """ห่อโมเดล ArcFace ไว้ พร้อมคุมเรื่องการโหลดและการ normalize"""

    def __init__(self) -> None:
        self._model = None
        self._loaded = False
        self._model_file: Path | None = None

        # onnxruntime รันทีละงานพอ ถ้าปล่อยให้แย่งกันหลาย thread จะช้าลงทั้งคู่
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # การโหลดโมเดล
    # ------------------------------------------------------------------
    def load(self) -> None:
        """โหลดโมเดลเข้าหน่วยความจำ เรียกครั้งเดียวตอนแอปสตาร์ท"""
        if self._loaded:
            return

        cfg = settings.face
        model_file = self._find_model_file(cfg.rec_model_path)

        import onnxruntime
        from insightface.model_zoo import model_zoo

        logger.info("กำลังโหลดโมเดลจดจำใบหน้า: %s", model_file)
        started = time.monotonic()

        sess_options = onnxruntime.SessionOptions()
        if cfg.num_threads > 0:
            sess_options.intra_op_num_threads = cfg.num_threads

        try:
            model = model_zoo.get_model(
                str(model_file),
                providers=["CPUExecutionProvider"],
                sess_options=sess_options,
            )
            if model is None:
                raise FaceModelError(
                    f"insightface ไม่รู้จักโมเดลในไฟล์ {model_file} "
                    "(อาจไม่ใช่โมเดลสำหรับจดจำใบหน้า)"
                )
            model.prepare(ctx_id=-1)  # -1 = ใช้ CPU
        except FaceModelError:
            raise
        except Exception as exc:  # noqa: BLE001 - ห่อให้เป็นข้อความที่อ่านรู้เรื่อง
            raise FaceModelError(
                f"โหลดโมเดลจดจำใบหน้าจาก {model_file} ไม่สำเร็จ: {exc}"
            ) from exc

        taskname = getattr(model, "taskname", "?")
        if taskname != "recognition":
            raise FaceModelError(
                f"ไฟล์ {model_file} ไม่ใช่โมเดลสำหรับจดจำใบหน้า "
                f"(insightface บอกว่าเป็นงานประเภท {taskname!r})"
            )

        self._model = model
        self._model_file = model_file
        self._loaded = True

        logger.info(
            "โหลดโมเดลจดจำใบหน้าสำเร็จใน %.2f วินาที (%s, เวกเตอร์ %d มิติ)",
            time.monotonic() - started,
            model_file.name,
            self.dimension,
        )

    @staticmethod
    def _find_model_file(pack_dir: Path) -> Path:
        """หาไฟล์โมเดลจดจำใบหน้าในโฟลเดอร์ชุดโมเดล

        ชื่อไฟล์ของ insightface ขึ้นต้นด้วย w600k_ เสมอ (w600k_r50, w600k_mbf)
        ถ้าหาไม่เจอให้ฟ้องทันทีพร้อมบอกวิธีแก้ ไม่ปล่อยให้ insightface
        แอบไปดาวน์โหลดจากอินเทอร์เน็ตเอง (ผิดกฎของโปรเจกต์)
        """
        if not pack_dir.exists():
            raise FaceModelError(
                f"ไม่พบโฟลเดอร์ชุดโมเดลจดจำใบหน้า: {pack_dir}\n"
                f"วิธีแก้: ตรวจว่าค่า FACE_REC_PACK ใน .env ตรงกับ "
                f"--build-arg FACE_REC_PACK ที่ใช้ตอน build image หรือไม่ "
                f"แล้วสั่ง docker compose build backend ใหม่"
            )

        candidates = sorted(pack_dir.glob("w600k_*.onnx"))

        if not candidates:
            available = sorted(f.name for f in pack_dir.glob("*.onnx"))
            raise FaceModelError(
                f"ไม่พบไฟล์โมเดลจดจำใบหน้า (w600k_*.onnx) ในโฟลเดอร์ {pack_dir}\n"
                f"ไฟล์ที่มีอยู่: {available or 'ไม่มีไฟล์ .onnx เลย'}\n"
                f"ลองสั่ง docker compose build --no-cache backend"
            )

        if len(candidates) > 1:
            # มีมากกว่าหนึ่งตัวแปลว่าตั้งค่าผิด ไม่เดาให้ว่าจะใช้ตัวไหน
            raise FaceModelError(
                f"พบโมเดลจดจำใบหน้ามากกว่าหนึ่งไฟล์ในโฟลเดอร์ {pack_dir}: "
                f"{[c.name for c in candidates]} - ไม่สามารถเดาได้ว่าต้องใช้ตัวไหน"
            )

        return candidates[0]

    # ------------------------------------------------------------------
    # การสกัด embedding
    # ------------------------------------------------------------------
    def embed(self, image: np.ndarray, keypoints: np.ndarray) -> np.ndarray:
        """สกัด embedding ที่ normalize แล้ว จากใบหน้าหนึ่งใบ

        image     : ภาพเต็มเฟรม (BGR)
        keypoints : จุดสังเกตบนใบหน้า 5 จุดจากตัวตรวจจับ

        ทำไมต้องใช้จุดสังเกตแทนการครอปตามกรอบตรง ๆ:
            ArcFace ต้องการใบหน้าที่ "จัดตรง" แล้ว คือตาสองข้างอยู่ระดับเดียวกัน
            และอยู่ตำแหน่งเดิมทุกครั้ง ถ้าครอปตามกรอบเฉย ๆ แล้วหน้าเอียง
            เวกเตอร์ที่ได้จะเพี้ยนไปมาก จนคนเดียวกันกลายเป็นคนละคน
            norm_crop() ใช้จุดสังเกตหมุนและปรับขนาดภาพให้ตรงแบบมาตรฐานก่อน
        """
        if not self._loaded or self._model is None:
            raise FaceModelError(
                "ยังไม่ได้โหลดโมเดลจดจำใบหน้า - ต้องเรียก FaceRecognizer.load() ก่อน"
            )

        if keypoints is None:
            raise ValueError("ไม่มีจุดสังเกตบนใบหน้า จึงจัดหน้าให้ตรงก่อนสกัดไม่ได้")

        from insightface.utils import face_align

        aligned = face_align.norm_crop(
            image,
            landmark=np.asarray(keypoints, dtype=np.float32),
            image_size=ARCFACE_INPUT_SIZE,
        )

        with self._lock:
            raw = self._model.get_feat(aligned)

        return self.normalize(raw)

    @staticmethod
    def normalize(vector: np.ndarray) -> np.ndarray:
        """ปรับความยาวเวกเตอร์ให้เป็น 1 เพื่อให้ dot product = cosine similarity"""
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))

        if norm == 0.0:
            # แทบเป็นไปไม่ได้ แต่ถ้าเกิดขึ้นแล้วหารต่อจะได้ NaN ทั้งเวกเตอร์
            # ซึ่งจะทำให้ FAISS คืนผลประหลาดโดยไม่มีใครรู้สาเหตุ
            raise ValueError("ได้เวกเตอร์ศูนย์จากโมเดล จึง normalize ไม่ได้")

        return vec / norm

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def dimension(self) -> int:
        """จำนวนมิติของเวกเตอร์ที่โมเดลนี้ให้ (ปกติ 512)"""
        if self._model is None:
            return 0
        shape = getattr(self._model, "output_shape", None)
        if shape and len(shape) >= 2:
            return int(shape[-1])
        return 512

    @property
    def model_name(self) -> str:
        return self._model_file.name if self._model_file else "-"


# instance เดียวใช้ร่วมกันทั้งแอป โหลดโมเดลใน lifespan ของ FastAPI
face_recognizer = FaceRecognizer()
