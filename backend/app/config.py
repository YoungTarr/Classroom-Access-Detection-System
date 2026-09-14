"""ศูนย์รวมค่า config ทั้งระบบ

กฎของโปรเจกต์นี้: ค่าตั้งค่าทุกตัวต้องอยู่ในไฟล์นี้ที่เดียว และต้องอ่านมาจาก
environment variable เท่านั้น ห้าม hardcode กระจัดกระจายตามไฟล์อื่น
ทุกตัวที่เพิ่มในไฟล์นี้ต้องไปเพิ่มใน .env.example ด้วยเสมอ

หลักการ "ห้าม fallback เงียบ ๆ":
    ค่าที่ขาดไม่ได้ (เช่น รหัสผ่านฐานข้อมูล) ถ้าไม่มีให้โยน error ทันทีตอนสตาร์ท
    ไม่ใช่ปล่อยให้แอปขึ้นมาแล้วไปพังตอน request แรกโดยไม่รู้สาเหตุ
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# โหลดไฟล์ .env ให้อัตโนมัติ (มีประโยชน์ตอนรัน backend นอก Docker)
# ถ้ารันใน Docker ค่าจะมาจาก env_file/environment ของ compose อยู่แล้ว
# override=False = ไม่ทับค่าที่ระบบตั้งมาแล้ว ป้องกันไฟล์ .env ไป override ค่าของ container
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # pragma: no cover - ไม่ควรเกิด เพราะอยู่ใน requirements.txt
    pass


# =============================================================================
# ตัวช่วยอ่านค่าจาก environment
# =============================================================================


class ConfigError(RuntimeError):
    """ค่า config ไม่ถูกต้องหรือขาดหายไป - ต้องหยุดการทำงานทันที ไม่เดาค่าแทน"""


def _get_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    """อ่านค่าข้อความจาก environment

    required=True แล้วไม่มีค่า -> โยน ConfigError พร้อมบอกชื่อตัวแปรที่ขาด
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        if required:
            raise ConfigError(
                f"ไม่พบค่า environment ที่จำเป็น: {name} "
                f"(ตรวจสอบไฟล์ .env โดยเทียบกับ .env.example)"
            )
        return default if default is not None else ""
    return raw.strip()


def _get_int(name: str, default: int) -> int:
    """อ่านค่าตัวเลขจำนวนเต็ม ถ้าแปลงไม่ได้ให้ฟ้องชัดเจนว่าค่าอะไรผิด"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"ค่า {name} ต้องเป็นตัวเลขจำนวนเต็ม แต่ได้รับ: {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    """อ่านค่าตัวเลขทศนิยม ถ้าแปลงไม่ได้ให้ฟ้องชัดเจนว่าค่าอะไรผิด"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"ค่า {name} ต้องเป็นตัวเลข แต่ได้รับ: {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    """อ่านค่าจริง/เท็จ รองรับ true/false, 1/0, yes/no (ไม่สนตัวพิมพ์ใหญ่เล็ก)"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"ค่า {name} ต้องเป็น true หรือ false แต่ได้รับ: {raw!r}"
    )


def _get_list(name: str, default: list[str] | None = None) -> list[str]:
    """อ่านค่าที่คั่นด้วยเครื่องหมายจุลภาค เช่น CORS_ORIGINS=http://a,http://b"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


# =============================================================================
# กลุ่มค่า config
# =============================================================================


@dataclass(frozen=True)
class DatabaseSettings:
    """ค่าการเชื่อมต่อ PostgreSQL

    เตือน: ใน Docker ค่า host ต้องเป็นชื่อ service ("db") และ port ต้องเป็น 5432
    docker-compose.yml override ให้แล้ว ดูคำอธิบายในไฟล์นั้นประกอบ
    """

    host: str
    port: int
    name: str
    user: str
    password: str

    # ขนาด connection pool - Pi 5 ไม่ได้แรงมาก เปิดไว้ไม่ต้องเยอะ
    pool_min_size: int = 1
    pool_max_size: int = 5

    @property
    def conninfo(self) -> str:
        """สตริงเชื่อมต่อแบบ key=value สำหรับ psycopg

        ใช้รูปแบบ key=value แทน URL เพื่อเลี่ยงปัญหารหัสผ่านที่มีอักขระพิเศษ
        (เช่น @ หรือ / ) ซึ่งต้อง URL-encode ถ้าใช้รูปแบบ URL
        """
        return (
            f"host={self.host} port={self.port} dbname={self.name} "
            f"user={self.user} password={self.password}"
        )

    def safe_repr(self) -> str:
        """ข้อความสำหรับ log - ปิดบังรหัสผ่านเสมอ ห้าม log รหัสผ่านออกมาเด็ดขาด"""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


@dataclass(frozen=True)
class AppSettings:
    """ค่าทั่วไปของแอปพลิเคชัน"""

    name: str = "Classroom Access Detection System"
    version: str = "0.3.0"  # เฟส 3: ติดตามใบหน้าข้ามเฟรม + กรอบขยับลื่น
    log_level: str = "INFO"
    timezone: str = "Asia/Bangkok"

    # origin ที่อนุญาตให้เรียกข้ามโดเมน ปกติเป็นลิสต์ว่างเพราะ nginx proxy ให้แล้ว
    cors_origins: list[str] = field(default_factory=list)

    # โฟลเดอร์เก็บรูปใบหน้า (path ภายใน container)
    faces_dir: Path = Path("/data/faces")


@dataclass(frozen=True)
class FaceSettings:
    """ค่าของโมเดลตรวจจับ/จดจำใบหน้า (InsightFace บน onnxruntime)"""

    # ชื่อชุดโมเดล: buffalo_l (แม่นกว่า ใช้ตอน dev) / buffalo_s (เล็กเร็วกว่า ใช้บน Pi)
    # ต้องตรงกับ --build-arg FACE_MODEL_PACK ที่ใช้ตอน build image
    # เพราะโมเดลถูกโหลดฝังไว้ใน image ตั้งแต่ตอน build แล้ว
    model_pack: str

    # โฟลเดอร์แม่ที่เก็บชุดโมเดล โครงสร้างคือ <models_dir>/<model_pack>/*.onnx
    models_dir: Path

    # ขนาดภาพที่ป้อนให้ตัวตรวจจับ (SCRFD) ยิ่งใหญ่ยิ่งเจอหน้าเล็กได้ดีแต่ช้าลง
    # 640 คือค่ามาตรฐาน บน Pi อาจลดเหลือ 480 หรือ 320
    det_size: int

    # คะแนนขั้นต่ำที่จะนับว่าเป็นใบหน้า ต่ำไป = เจอขยะ สูงไป = หน้าเอียงแล้วหลุด
    det_thresh: float

    # จำนวน thread ที่ onnxruntime ใช้ต่อการประมวลผลหนึ่งครั้ง
    # 0 = ปล่อยให้ onnxruntime ตัดสินใจเอง (Pi 5 มี 4 core ลองตั้ง 3 ไว้เผื่อ core ให้งานอื่น)
    num_threads: int

    @property
    def model_path(self) -> Path:
        """path เต็มของโฟลเดอร์ชุดโมเดลที่จะโหลด"""
        return self.models_dir / self.model_pack


@dataclass(frozen=True)
class TrackingSettings:
    """ค่าของการติดตามใบหน้าข้ามเฟรม (tracking)

    จุดประสงค์ของ tracking คือทำให้ใบหน้าเดิม "มี id เดิม" ตลอดที่ยังอยู่ในภาพ
    ซึ่งจำเป็นมากสำหรับเฟสถัดไป:
        เฟส 4 - ใช้โหวตผลการจดจำย้อนหลังหลายเฟรม กันชื่อกะพริบสลับไปมา
        เฟส 5 - ใช้เก็บประวัติตำแหน่งเพื่อคำนวณทิศทางการเคลื่อนที่
    """

    # ค่า IoU (พื้นที่ทับซ้อน / พื้นที่รวม) ขั้นต่ำที่จะถือว่าเป็นใบหน้าเดียวกัน
    # สูงไป = ขยับเร็วนิดเดียวก็ถือเป็นคนใหม่, ต่ำไป = คนสองคนที่ยืนติดกันจะถูกจับสลับกัน
    iou_threshold: float

    # ถ้าจับคู่ด้วย IoU ไม่ได้ (เช่นขยับเร็วจนกรอบไม่ทับกันเลย)
    # ให้ลองจับคู่ด้วยระยะจุดกึ่งกลางแทน โดยยอมรับระยะไม่เกินสัดส่วนนี้ของความกว้างภาพ
    # ใช้เป็น "สัดส่วน" ไม่ใช่พิกเซลตายตัว เพื่อให้ทำงานเหมือนกันทุกความละเอียด
    max_center_distance_ratio: float

    # ใบหน้าหายไปกี่เฟรมติดกันจึงจะลบ track ทิ้ง
    # การค้างไว้สั้น ๆ คือหัวใจของการกันกรอบกะพริบ เพราะตัวตรวจจับมัก "พลาด"
    # เป็นครั้งคราวเมื่อหันหน้า เอามือบัง หรือแสงเปลี่ยน
    max_missing: int

    # จำนวนตำแหน่งย้อนหลังที่เก็บต่อหนึ่ง track (เฟส 5 ใช้คำนวณทิศทาง)
    history_size: int

    # ความนุ่มนวลของการขยับกรอบฝั่งหน้าเว็บ (0.0-1.0)
    # 0 = ไม่ขยับเลย, 1 = กระโดดไปตำแหน่งใหม่ทันที (เท่ากับไม่ได้ทำ smoothing)
    # ค่าน้อย = ลื่นแต่ตามช้า, ค่ามาก = ตามไว แต่กระตุกตามผลดิบมากขึ้น
    smoothing: float


@dataclass(frozen=True)
class StreamSettings:
    """ค่าที่เกี่ยวกับการรับภาพเข้ามาประมวลผล

    ค่าหลายตัวในนี้ frontend เป็นคนใช้ จึงถูกส่งออกไปทาง GET /api/config
    เพื่อให้ยังคงกฎ "config อยู่ที่เดียว" ไม่ต้องไปตั้งซ้ำในไฟล์ JavaScript
    """

    # แหล่งภาพ: browser = เว็บแคมผ่าน WebSocket (เฟส 2) / rtsp = กล้อง IP (เฟส 6)
    source: str

    # ความกว้างที่ย่อภาพก่อนส่งเข้า backend (สูงเกินไป = ช้าและกินแบนด์วิดท์)
    target_width: int

    # จำนวนเฟรมต่อวินาทีที่เบราว์เซอร์ "พยายาม" ส่ง (ของจริงขึ้นกับความเร็วในการตรวจจับ)
    send_fps: int

    # คุณภาพ JPEG ตอนเข้ารหัสก่อนส่ง (0.0-1.0)
    jpeg_quality: float

    # ภาพจากเว็บแคมควรกลับซ้าย-ขวาแบบกระจกเงาหรือไม่
    # true = ผู้ใช้เห็นตัวเองเหมือนส่องกระจก ซึ่งเป็นธรรมชาติกว่าสำหรับเว็บแคม
    # ค่านี้มีผลต่อการวาดกรอบด้วย (ต้องกลับพิกัดแกน X ตาม) และจะสำคัญมากในเฟส 5
    # ตอนคำนวณทิศทางซ้าย/ขวา เพราะ "ซ้ายบนจอ" กับ "ซ้ายในโลกจริง" จะสลับกัน
    mirror: bool


@dataclass(frozen=True)
class Settings:
    """รวมทุกกลุ่มไว้ในที่เดียว เรียกใช้ผ่านตัวแปร settings ด้านล่าง"""

    app: AppSettings
    database: DatabaseSettings
    face: FaceSettings
    stream: StreamSettings
    tracking: TrackingSettings


# แหล่งภาพที่ระบบรองรับ - ใส่ค่านอกเหนือจากนี้ต้องฟ้อง ไม่ใช่เงียบ ๆ แล้วใช้ค่า default
VALID_FRAME_SOURCES = ("browser", "rtsp")


def load_settings() -> Settings:
    """อ่านค่าทั้งหมดจาก environment ครั้งเดียวตอน import โมดูลนี้"""
    frame_source = _get_str("FRAME_SOURCE", "browser").lower()
    if frame_source not in VALID_FRAME_SOURCES:
        raise ConfigError(
            f"ค่า FRAME_SOURCE ไม่ถูกต้อง: {frame_source!r} "
            f"(รองรับเฉพาะ {' หรือ '.join(VALID_FRAME_SOURCES)})"
        )

    return Settings(
        app=AppSettings(
            log_level=_get_str("LOG_LEVEL", "INFO").upper(),
            timezone=_get_str("TZ", "Asia/Bangkok"),
            cors_origins=_get_list("CORS_ORIGINS"),
            faces_dir=Path(_get_str("FACES_DIR", "/data/faces")),
        ),
        database=DatabaseSettings(
            # DB_HOST/DB_PORT มีค่า default เพื่อให้รันใน Docker ได้ทันที
            # แต่ user/password/name ไม่มี default เด็ดขาด - ขาดเมื่อไรต้องฟ้อง
            host=_get_str("DB_HOST", "db"),
            port=_get_int("DB_PORT", 5432),
            name=_get_str("DB_NAME", required=True),
            user=_get_str("DB_USER", required=True),
            password=_get_str("DB_PASSWORD", required=True),
        ),
        face=FaceSettings(
            model_pack=_get_str("FACE_MODEL_PACK", "buffalo_l"),
            models_dir=Path(_get_str("FACE_MODELS_DIR", "/models/models")),
            det_size=_get_int("FACE_DET_SIZE", 640),
            det_thresh=_get_float("FACE_DET_THRESH", 0.5),
            num_threads=_get_int("FACE_NUM_THREADS", 0),
        ),
        stream=StreamSettings(
            source=frame_source,
            target_width=_get_int("STREAM_TARGET_WIDTH", 640),
            send_fps=_get_int("STREAM_SEND_FPS", 10),
            jpeg_quality=_get_float("STREAM_JPEG_QUALITY", 0.7),
            mirror=_get_bool("CAMERA_MIRROR", True),
        ),
        tracking=TrackingSettings(
            iou_threshold=_get_float("TRACK_IOU_THRESHOLD", 0.3),
            max_center_distance_ratio=_get_float("TRACK_MAX_CENTER_DISTANCE_RATIO", 0.15),
            max_missing=_get_int("TRACK_MAX_MISSING", 3),
            history_size=_get_int("TRACK_HISTORY_SIZE", 15),
            smoothing=_get_float("TRACK_SMOOTHING", 0.35),
        ),
    )


# instance เดียวใช้ร่วมกันทั้งระบบ
settings = load_settings()
