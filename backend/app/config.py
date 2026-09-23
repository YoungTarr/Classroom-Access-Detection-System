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
    version: str = "0.8.0"  # เฟส 8: กล้องสองตัวพร้อมกัน (ขาเข้า / ขาออก)
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

    # ชุดโมเดลที่ใช้ "จดจำ" ใบหน้า แยกจากชุดที่ใช้ "ตรวจจับ" โดยตั้งใจ
    # วัดจริงแล้ว w600k_r50 (buffalo_l) ใช้ ~167 ms ต่อหนึ่งใบหน้า ซึ่งหนักเกินไป
    # ส่วน w600k_mbf (buffalo_s) ใช้ ~12 ms เร็วกว่า 14 เท่า และแยกคนได้ดีพอ ๆ กัน
    rec_pack: str

    @property
    def model_path(self) -> Path:
        """path เต็มของโฟลเดอร์ชุดโมเดลตรวจจับที่จะโหลด"""
        return self.models_dir / self.model_pack

    @property
    def rec_model_path(self) -> Path:
        """path เต็มของโฟลเดอร์ชุดโมเดลจดจำใบหน้า"""
        return self.models_dir / self.rec_pack


@dataclass(frozen=True)
class RateSettings:
    """อัตราการทำงานของแต่ละขั้น แยกอิสระจากกัน (เฟส 7)

    ============================================================================
    ทำไมต้องแยกสามค่า
    ============================================================================

    ก่อนหน้านี้ทุกอย่างเดินด้วยจังหวะเดียวกัน คือ "อ่านเฟรม -> ตรวจ -> ส่ง"
    ผลคืออัตราที่หน้าเว็บได้ภาพ ถูกล็อกไว้เท่ากับความเร็วของ AI เสมอ
    AI ใช้ ~100 ms ต่อเฟรม หน้าเว็บจึงได้ภาพแค่ ~8 fps ซึ่งตาคนเห็นว่ากระตุก

    แต่ความจริงแล้วสองอย่างนี้ไม่จำเป็นต้องเท่ากันเลย:
        - ตำแหน่งใบหน้าเปลี่ยนช้า ตรวจ 5 ครั้งต่อวินาทีก็เพียงพอ
        - ส่วนภาพต้องลื่น ควรส่ง 15 ครั้งต่อวินาทีขึ้นไป

    พอแยกจากกันแล้ว ภาพจะลื่นขึ้นมากโดยที่เครื่องทำงานน้อยลงด้วยซ้ำ
    ระหว่างเฟรมที่ยังไม่มีผลตรวจใหม่ หน้าเว็บจะ interpolate กรอบต่อไปเอง
    (กลไกที่ทำไว้ตั้งแต่เฟส 3)
    """

    # อัตราสูงสุดที่ดึงเฟรมจากกล้องมาใช้งาน
    # กล้องส่งมา ~15 fps ถ้าตั้งต่ำกว่านั้นจะข้ามเฟรมส่วนเกินทิ้งตั้งแต่ต้นทาง
    # ช่วยลดภาระ CPU เพราะไม่ต้องแปลงสีภาพที่ยังไงก็ไม่ได้ใช้
    capture_fps: int

    # อัตราที่ส่งภาพเข้าโมเดล AI - ตัวนี้แพงที่สุด ตั้งต่ำไว้
    #
    # ตั้งแต่เฟส 8 ค่านี้หมายถึง **ต่อกล้องหนึ่งตัว**
    # สองกล้องที่ตั้ง DETECT_FPS=5 จึงหมายถึงอยากได้กล้องละ 5 ครั้งต่อวินาที
    detect_fps: int

    # เพดาน "รวมทุกกล้อง" ของการตรวจจับ (เฟส 8)
    #
    # ทำไมต้องมีแยกจาก detect_fps: เพราะ detect_fps เป็นความต้องการต่อกล้อง
    # แต่ CPU ที่มีเป็นของทั้งเครื่องร่วมกัน สองกล้องที่ขอกล้องละ 5 = ขอรวม 10
    # ซึ่ง Pi 5 อาจทำไม่ไหวเมื่อต้องถอดรหัสวิดีโอสองสตรีมไปพร้อมกันด้วย
    # ค่านี้คือปุ่มเดียวที่ใช้บอกว่า "ทั้งระบบตรวจได้ไม่เกินกี่ครั้งต่อวินาที"
    #
    # ไม่ได้ตั้งไว้ (หรือตั้ง 0) = คิดให้เองเป็น detect_fps x จำนวนกล้องที่เปิดใช้
    # ซึ่งเท่ากับไม่ได้จำกัดอะไรเพิ่ม และทำให้ระบบกล้องตัวเดียวทำงานเหมือนเดิมทุกประการ
    detect_fps_total: int

    # อัตราที่ส่งภาพไปหน้าเว็บ - ตั้งสูงได้เพราะแค่เข้ารหัส JPEG ไม่ได้คิดอะไรหนัก
    stream_fps: int

    # ผลตรวจเก่าเกินกี่เฟรมของสตรีม จึงเริ่มทำให้กรอบจางลง
    # เพื่อบอกผู้ใช้ว่า "กำลังเดาตำแหน่งอยู่" ไม่ใช่ผลสด
    stale_after_frames: int


@dataclass(frozen=True)
class CameraSettings:
    """กล้อง IP หนึ่งตัว (เฟส 6)

    ออกแบบให้เป็น "รายการ" ตั้งแต่ต้น แม้ตอนนี้จะมีกล้องตัวเดียว
    เพราะระบบจริงจะมีสองตัวที่ประตู (ตัวหนึ่งจับคนเข้า อีกตัวจับคนออก)
    การเพิ่มกล้องตัวที่สองจึงต้องแก้แค่ไฟล์ .env ไม่ต้องแตะโค้ดเลย
    """

    # ชื่อสั้น ๆ ที่ใช้อ้างอิงกล้องตัวนี้ (ใช้เป็นส่วนหนึ่งของชื่อตัวแปร env ด้วย)
    id: str

    # ชื่อที่แสดงบนหน้าเว็บ
    name: str

    # ==========================================================================
    # ภาพของกล้องตัวนี้มาจากไหน (เฟส 8)
    # ==========================================================================
    #
    #   "rtsp"    = backend ไปดึงจากกล้อง IP เอง (ใช้ host/port/username/password)
    #   "browser" = เบราว์เซอร์ที่เปิดหน้าเว็บส่งภาพเว็บแคมขึ้นมาให้
    #
    # ที่ต้องมี "browser" ด้วย เพราะช่วงนี้มีกล้อง Tapo แค่ตัวเดียว
    # จึงใช้เว็บแคมของโน้ตบุ๊กเป็นกล้องขาออกไปก่อน เพื่อทดสอบระบบสองกล้องได้จริง
    #
    # ข้อดีของการทำเป็น "แหล่งภาพต่อกล้อง" แทนการแยกโหมดทั้งระบบ:
    # ทุกอย่างที่เหลือมองกล้องสองตัวนี้เหมือนกันหมด (สถิติ, /api/health,
    # คิวตรวจจับ, การแชร์โมเดล, ทิศทาง IN/OUT)
    # พอกล้อง Tapo ตัวที่สองมาถึง เปลี่ยน SOURCE เป็น rtsp แล้วใส่ HOST
    # เท่านี้ก็ใช้งานได้ ไม่ต้องแก้โค้ดแม้แต่บรรทัดเดียว
    #
    # ข้อจำกัดที่ต้องรู้: กล้องแบบ browser จะมีภาพเฉพาะตอนมีคนเปิดหน้าเว็บอยู่
    # ไม่มีใครเปิดอยู่ = ไม่มีภาพ และหน้า health จะรายงานว่ากล้องตัวนั้นหลุด
    # ซึ่งถูกต้องตามความจริง ไม่ใช่การซ่อนปัญหา
    source: str

    # ---- ใช้เฉพาะกล้องแบบ rtsp (กล้องแบบ browser ไม่ต้องมีค่าพวกนี้) ----
    host: str
    port: int
    username: str
    password: str

    # path ของสตรีมบนกล้อง เช่น /stream1 (ความละเอียดสูง) หรือ /stream2 (ต่ำกว่า เร็วกว่า)
    path: str

    # กล้องตัวนี้ติดตั้งไว้เพื่อจับคนเข้าหรือคนออก ("IN" / "OUT")
    # ยังไม่ได้ใช้ตัดสินใจอะไรในเฟสนี้ แต่เก็บไว้ให้พร้อมสำหรับการบันทึกเข้า-ออก
    direction: str

    # ปิดกล้องตัวนี้ชั่วคราวได้โดยไม่ต้องลบ config ทิ้ง
    enabled: bool

    @property
    def is_rtsp(self) -> bool:
        """กล้องตัวนี้เป็นกล้อง IP ที่ backend ไปดึงภาพเองหรือไม่"""
        return self.source == "rtsp"

    @property
    def rtsp_url(self) -> str:
        """ประกอบ RTSP URL จากชิ้นส่วนใน config

        **ต้อง URL-encode ชื่อผู้ใช้และรหัสผ่านเสมอ**
        เพราะรหัสผ่านที่มีอักขระพิเศษจะทำให้ URL ผิดรูปทันที เช่น
            รหัสผ่าน  p@ss/word
            ถ้าไม่ encode -> rtsp://user:p@ss/word@192.168.1.102/stream2
            ตัว @ ตัวแรกจะถูกตีความว่าเป็นตัวคั่น host ทำให้ต่อกล้องไม่ได้
            และ error ที่ได้จะกำกวมมาก (ไม่มีอะไรบอกว่าเป็นเพราะรหัสผ่าน)
        หลัง encode จะกลายเป็น p%40ss%2Fword ซึ่งปลอดภัย
        """
        from urllib.parse import quote

        # safe="" หมายถึงให้ encode ทุกอักขระพิเศษ ไม่เว้นตัวไหนไว้เลย
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        path = self.path if self.path.startswith("/") else f"/{self.path}"

        return f"rtsp://{user}:{password}@{self.host}:{self.port}{path}"

    def safe_url(self) -> str:
        """URL สำหรับใส่ใน log - ปิดบังชื่อผู้ใช้และรหัสผ่านเสมอ

        ห้าม log RTSP URL เต็ม ๆ เด็ดขาด เพราะ log มักถูกส่งต่อ แปะในแชต
        หรือเก็บไว้ในไฟล์ที่คนอื่นอ่านได้ รหัสผ่านกล้องจะหลุดไปโดยไม่ตั้งใจ
        """
        if not self.is_rtsp:
            # กล้องแบบ browser ไม่มี URL ให้ปิดบัง บอกที่มาของภาพไปตรง ๆ แทน
            return "เว็บแคมของเบราว์เซอร์ (ไม่มี URL)"

        path = self.path if self.path.startswith("/") else f"/{self.path}"
        return f"rtsp://***:***@{self.host}:{self.port}{path}"


@dataclass(frozen=True)
class RTSPSettings:
    """ค่าการเชื่อมต่อกล้อง IP ที่ใช้ร่วมกันทุกตัว"""

    cameras: list[CameraSettings]

    # ตัวเลือกที่ส่งให้ FFmpeg ผ่าน environment OPENCV_FFMPEG_CAPTURE_OPTIONS
    #
    # rtsp_transport;tcp สำคัญมาก: กล้อง Tapo ต่อผ่าน Wi-Fi 2.4GHz ซึ่งมีแพ็กเก็ตหาย
    # ถ้าใช้ UDP (ค่าเริ่มต้นของ FFmpeg) ภาพจะแตกเป็นบล็อก ๆ และ decode พัง
    # TCP ช้ากว่านิดหน่อยแต่ภาพครบ ซึ่งจำเป็นสำหรับการตรวจจับใบหน้า
    #
    # stimeout เป็นหน่วยไมโครวินาที บอกให้ FFmpeg ยอมแพ้เองเมื่อรอข้อมูลนานเกินไป
    # ถ้าไม่ตั้ง cap.read() อาจค้างไม่มีวันคืนค่า เมื่อกล้องถูกถอดปลั๊ก
    ffmpeg_options: str

    # ไม่ได้เฟรมใหม่เกินกี่วินาทีถือว่าสตรีมตายแล้ว ต้องต่อใหม่
    watchdog_timeout: float

    # การรอก่อนลองต่อใหม่ เพิ่มเป็นเท่าตัวทุกครั้งที่ล้มเหลว (1, 2, 4, 8, ...)
    reconnect_initial_delay: float
    reconnect_max_delay: float


@dataclass(frozen=True)
class DirectionSettings:
    """ค่าของการคำนวณทิศทางการเคลื่อนที่ (เฟส 5)

    ============================================================================
    เรื่องที่ต้องเข้าใจให้ชัดก่อนแก้ไฟล์นี้: "ซ้าย" ของใคร
    ============================================================================

    ภาพจากเว็บแคมที่แสดงบนหน้าเว็บถูกพลิกกระจก (CSS transform: scaleX(-1))
    เพื่อให้ผู้ใช้เห็นตัวเองเหมือนส่องกระจก ซึ่งเป็นธรรมชาติกว่า
    แต่ภาพที่ "ส่งมาให้ backend ประมวลผล" นั้น **ไม่ได้ถูกพลิก** (ดู camera.js)

    ผลคือคำว่า "ซ้าย" มีสองความหมายที่ตรงข้ามกัน:

        world  = ซ้าย/ขวาตามที่กล้องมองเห็นจริง (ภาพดิบ ไม่พลิก)
                 คนเดินไปทางขวาของภาพ = เดินไปทางขวาของห้องจริง

        screen = ซ้าย/ขวาตามที่ผู้ใช้เห็นบนจอ (หลังพลิกกระจกแล้ว)
                 คนโบกมือขวา จะเห็นอยู่ฝั่งซ้ายของจอ

    ============================================================================
    ทำไมเรื่องนี้สำคัญมาก
    ============================================================================

    เฟส 6 จะมีกล้องสองตัวที่ประตู และทิศทางนี้จะถูกใช้ตัดสินว่า
    คนคนนั้น "เข้าห้อง" หรือ "ออกจากห้อง"

    ถ้าใช้กรอบอ้างอิงผิด ระบบจะบันทึกเข้า-ออกกลับด้านทั้งหมด
    ซึ่งเป็นบั๊กที่หาเจอยากมาก เพราะระบบทำงานได้ปกติทุกอย่าง แค่ผลกลับด้าน

    ============================================================================
    ทำไมค่าตั้งต้นเป็น "screen"
    ============================================================================

    เพราะมันให้ผลที่ถูกต้องทั้งสองสถานการณ์:

        ตอนทดสอบด้วยเว็บแคม (จอพลิกกระจก)
            ป้ายทิศทางจะตรงกับที่ผู้ใช้เห็นบนจอ ถ้าใช้ "world" ป้ายจะบอก
            "ไปทางขวา" ในขณะที่กรอบวิ่งไปทางซ้ายบนจอ ซึ่งดูเหมือนระบบพัง

        ตอนใช้กล้อง IP จริง (เฟส 6 ไม่มีการพลิกกระจก)
            เมื่อ mirror=false ทั้งสองกรอบอ้างอิงให้ผล "เหมือนกันทุกประการ"
            การเลือก screen จึงไม่ได้ทำให้เสียความถูกต้องของ IN/OUT เลย

    สรุป: เลือก screen แล้วได้ทั้งความเข้าใจง่ายตอน dev และความถูกต้องตอนใช้จริง
    ถ้าอยากได้ทิศทางตามภาพดิบที่กล้องเห็นเสมอ ให้เปลี่ยนเป็น world
    """

    # กรอบอ้างอิงที่ใช้รายงานทิศทาง: "world" (ตามจริง) หรือ "screen" (ตามที่เห็นบนจอ)
    reference: str

    # เทียบตำแหน่งปัจจุบันกับเมื่อกี่เฟรมก่อน
    # น้อยไป = ไวแต่สั่นตามการขยับเล็กน้อย, มากไป = นิ่งแต่ตอบสนองช้า
    frame_gap: int

    # ระยะขั้นต่ำที่ถือว่า "เคลื่อนที่" คิดเป็นสัดส่วนของความกว้างภาพ
    # ใช้สัดส่วนไม่ใช่พิกเซลตายตัว เพื่อให้ได้ผลเหมือนกันทุกความละเอียด
    # (ขยับ 20 px บนภาพกว้าง 320 กับ 1280 มีความหมายต่างกันมาก)
    min_shift_ratio: float

    # ตัวคูณลดเกณฑ์เมื่อ "กำลังเคลื่อนที่อยู่แล้ว" (hysteresis)
    # ป้องกันอาการกะพริบสลับ เคลื่อนที่ <-> อยู่กับที่ ตอนเดินช้า ๆ
    # ใกล้เกณฑ์พอดี ค่า 0.6 = พอเริ่มเดินแล้วจะเลิกนับว่าเดินก็ต่อเมื่อช้าลงกว่าเดิมมาก
    hysteresis: float

    # จำนวนผลย้อนหลังที่เก็บไว้โหวต (กันป้ายทิศทางกะพริบ หลักการเดียวกับการโหวตชื่อ)
    vote_window: int


@dataclass(frozen=True)
class IdentifySettings:
    """ค่าของการระบุตัวตน (เฟส 4)"""

    # วิธีที่ใช้ระบุตัวตน ตอนนี้มีแค่ "face"
    # อนาคตอาจเพิ่ม rfid / qr โดยไม่ต้องแก้ pipeline
    mode: str

    # คะแนน cosine similarity ขั้นต่ำที่จะถือว่า "ใช่คนนี้"
    # ทดสอบแล้วพบว่าคนต่างกันได้คะแนนสูงสุดราว 0.17 จึงเว้นระยะห่างไว้มากพอ
    # สูงไป = คนในระบบก็จำไม่ได้ (ขึ้น Unknown), ต่ำไป = จำสลับคน
    similarity_threshold: float

    # จำนวนใบหน้าสูงสุดที่จะส่งเข้าโมเดลจดจำต่อหนึ่งเฟรม
    # เป็นเพดานกันกรณีคนเยอะผิดปกติจนเฟรมหนึ่งใช้เวลานานเกินไป
    # ใบหน้าที่เกินเพดานจะถูกจัดคิวไปทำในเฟรมถัดไป (ไม่ได้ถูกทิ้ง)
    max_faces_per_frame: int

    # จำนวนผลย้อนหลังที่เก็บไว้โหวตต่อหนึ่ง track
    # นี่คือส่วนที่กันชื่อกะพริบ: ไม่เชื่อผลเฟรมเดียว แต่ดูว่าหลายเฟรมที่ผ่านมาบอกว่าใคร
    vote_window: int

    # ต้องได้เสียงข้างมากอย่างน้อยกี่เสียงจึงจะกล้าแสดงชื่อ
    # ตั้งต่ำไป = ชื่อขึ้นเร็วแต่ผิดง่าย, สูงไป = กว่าจะขึ้นชื่อต้องรอนาน
    min_votes: int

    # track ที่ได้ผลชัดเจนแล้ว จะตรวจซ้ำทุกกี่เฟรม
    # ไม่ตรวจทุกเฟรมเพื่อประหยัดแรงเครื่อง แต่ยังตรวจเป็นระยะเผื่อจับคนผิดตั้งแต่แรก
    recheck_interval: int


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

    # โหมดการทำงานของทั้งระบบ
    #
    #   "browser" = โหมดเว็บแคมเดี่ยว (เฟส 2-5) ไม่ใช้ระบบกล้องหลายตัวเลย
    #               เบราว์เซอร์ส่งภาพมาทาง /ws/detect แล้วรับผลกลับไปวาดเอง
    #               มีไว้สำหรับพัฒนา/ทดสอบบนเครื่องที่ไม่มีกล้อง IP
    #
    #   "rtsp"    = โหมดกล้องหลายตัวตาม CAMERA_IDS (เฟส 6 เป็นต้นไป)
    #               ชื่อยังเป็น rtsp ตามเดิมเพราะกล้องส่วนใหญ่เป็นกล้อง IP
    #               แต่ตั้งแต่เฟส 8 กล้องแต่ละตัวเลือกแหล่งภาพของตัวเองได้
    #               (CAMERA_<ID>_SOURCE = rtsp หรือ browser)
    #               จึงใช้กล้อง IP กับเว็บแคมพร้อมกันในโหมดนี้ได้
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
    identify: IdentifySettings
    direction: DirectionSettings
    rtsp: RTSPSettings
    rates: RateSettings


# แหล่งภาพที่ระบบรองรับ - ใส่ค่านอกเหนือจากนี้ต้องฟ้อง ไม่ใช่เงียบ ๆ แล้วใช้ค่า default
VALID_FRAME_SOURCES = ("browser", "rtsp")

# วิธีระบุตัวตนที่ระบบรองรับ (อนาคตอาจเพิ่ม rfid / qr)
VALID_IDENTIFIERS = ("face",)

# กรอบอ้างอิงของทิศทาง - อ่านคำอธิบายเต็มใน DirectionSettings ก่อนเปลี่ยนค่า
VALID_DIRECTION_REFERENCES = ("world", "screen")

# ทิศทางที่กล้องแต่ละตัวรับผิดชอบ
VALID_CAMERA_DIRECTIONS = ("IN", "OUT")

# แหล่งภาพของกล้องแต่ละตัว (เฟส 8 - อ่านคำอธิบายเต็มใน CameraSettings.source)
VALID_CAMERA_SOURCES = ("rtsp", "browser")


def _camera_env_prefix(camera_id: str) -> str:
    """แปลงชื่อกล้องเป็นคำนำหน้าของตัวแปร environment

    door_in -> CAMERA_DOOR_IN_   จึงได้ตัวแปรเช่น CAMERA_DOOR_IN_HOST
    """
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in camera_id.upper())
    return f"CAMERA_{cleaned}_"


def _load_cameras(require_credentials: bool) -> list[CameraSettings]:
    """อ่านรายการกล้องทั้งหมดจาก environment

    รูปแบบใน .env ออกแบบให้เพิ่มกล้องได้โดยไม่ต้องแก้โค้ด:

        CAMERA_IDS=door_in,door_out

        CAMERA_DOOR_IN_NAME=ประตูทางเข้า
        CAMERA_DOOR_IN_HOST=192.168.1.102
        CAMERA_DOOR_IN_DIRECTION=IN
        ...

    ค่าที่กล้องทุกตัวมักใช้เหมือนกัน (บัญชีผู้ใช้ พอร์ต path) ตั้งเป็นค่ากลางได้ที่
    CAMERA_DEFAULT_USER / CAMERA_DEFAULT_PASSWORD / CAMERA_DEFAULT_PORT / CAMERA_DEFAULT_PATH
    แล้วกล้องตัวไหนที่ต่างจากค่ากลางค่อยกำหนดทับเฉพาะตัวนั้น

    กล้องที่ใช้เว็บแคมของเครื่องที่เปิดหน้าเว็บ (เฟส 8) ตั้งแค่:

        CAMERA_DOOR_OUT_SOURCE=browser
        CAMERA_DOOR_OUT_DIRECTION=OUT

    ไม่ต้องมี HOST หรือบัญชีผู้ใช้ เพราะไม่ได้ต่อผ่านเครือข่าย
    """
    camera_ids = _get_list("CAMERA_IDS")
    if not camera_ids:
        return []

    # ค่ากลางที่ใช้ร่วมกันทุกตัว (กล้อง Tapo หลายตัวมักใช้บัญชีเดียวกัน)
    default_user = _get_str("CAMERA_DEFAULT_USER", "")
    default_password = _get_str("CAMERA_DEFAULT_PASSWORD", "")
    default_port = _get_int("CAMERA_DEFAULT_PORT", 554)
    default_path = _get_str("CAMERA_DEFAULT_PATH", "/stream2")

    # ชื่อกล้องซ้ำกันจะกลายเป็นกุญแจซ้ำใน dict ของแหล่งภาพ ตัวหลังทับตัวหน้าเงียบ ๆ
    # อาการที่เห็นคือ "ตั้งกล้องสองตัวแล้วขึ้นตัวเดียว" ซึ่งหาสาเหตุยากมาก
    duplicates = {cid for cid in camera_ids if camera_ids.count(cid) > 1}
    if duplicates:
        raise ConfigError(
            f"CAMERA_IDS มีชื่อกล้องซ้ำกัน: {', '.join(sorted(duplicates))}\n"
            "กล้องแต่ละตัวต้องมีชื่อไม่ซ้ำ เพราะชื่อถูกใช้แยกภาพและสถิติของแต่ละตัว"
        )

    cameras: list[CameraSettings] = []

    for camera_id in camera_ids:
        prefix = _camera_env_prefix(camera_id)

        enabled = _get_bool(f"{prefix}ENABLED", True)

        # ---- แหล่งภาพของกล้องตัวนี้ (rtsp = กล้อง IP / browser = เว็บแคม) ----
        source = _get_str(f"{prefix}SOURCE", "rtsp").lower()
        if source not in VALID_CAMERA_SOURCES:
            raise ConfigError(
                f"ค่า {prefix}SOURCE ไม่ถูกต้อง: {source!r} "
                f"(รองรับเฉพาะ {' หรือ '.join(VALID_CAMERA_SOURCES)})\n"
                f"  rtsp    = backend ไปดึงภาพจากกล้อง IP เอง\n"
                f"  browser = เบราว์เซอร์ที่เปิดหน้าเว็บส่งภาพเว็บแคมขึ้นมาให้"
            )

        is_rtsp = source == "rtsp"

        host = _get_str(f"{prefix}HOST", "")
        username = _get_str(f"{prefix}USER", default_user)
        password = _get_str(f"{prefix}PASSWORD", default_password)

        # ค่าที่เกี่ยวกับเครือข่ายบังคับเฉพาะกล้องแบบ rtsp เท่านั้น
        # กล้องแบบ browser ไม่ได้ต่อผ่านเครือข่าย จึงไม่มีอะไรให้ตั้ง
        if is_rtsp and not host:
            raise ConfigError(
                f"กล้อง {camera_id!r} ไม่ได้กำหนด {prefix}HOST ในไฟล์ .env\n"
                f"(ชื่อกล้องมาจาก CAMERA_IDS - ถ้าไม่ได้ใช้กล้องตัวนี้แล้วให้เอาออกจาก CAMERA_IDS\n"
                f" ถ้าตั้งใจจะใช้เว็บแคมของเครื่องแทน ให้ตั้ง {prefix}SOURCE=browser)"
            )

        # บัญชีกล้องขาดไม่ได้ ถ้าไม่มีจะต่อไม่ได้แน่นอน จึงฟ้องตั้งแต่ตอนสตาร์ท
        # ดีกว่าปล่อยให้ไปเจอ error กำกวมจาก FFmpeg ตอน runtime
        #
        # แต่ตรวจเฉพาะตอนที่จะใช้กล้องจริง (FRAME_SOURCE=rtsp และกล้องเปิดอยู่)
        # ไม่งั้นคนที่ใช้แค่เว็บแคมจะสตาร์ทระบบไม่ได้ ทั้งที่ยังไม่มีกล้อง IP
        if require_credentials and enabled and is_rtsp and (not username or not password):
            raise ConfigError(
                f"กล้อง {camera_id!r} ยังไม่ได้ตั้งบัญชีผู้ใช้\n"
                f"กำหนด {prefix}USER และ {prefix}PASSWORD "
                f"หรือใช้ค่ากลาง CAMERA_DEFAULT_USER / CAMERA_DEFAULT_PASSWORD\n"
                f"(สำหรับกล้อง TP-Link Tapo คือบัญชีที่ตั้งไว้ในแอปที่เมนู "
                f"Advanced Settings > Camera Account)"
            )

        direction = _get_str(f"{prefix}DIRECTION", "IN").upper()
        if direction not in VALID_CAMERA_DIRECTIONS:
            raise ConfigError(
                f"ค่า {prefix}DIRECTION ไม่ถูกต้อง: {direction!r} "
                f"(รองรับเฉพาะ {' หรือ '.join(VALID_CAMERA_DIRECTIONS)})"
            )

        cameras.append(
            CameraSettings(
                id=camera_id,
                name=_get_str(f"{prefix}NAME", camera_id),
                source=source,
                host=host,
                port=_get_int(f"{prefix}PORT", default_port),
                username=username,
                password=password,
                path=_get_str(f"{prefix}PATH", default_path),
                direction=direction,
                enabled=enabled,
            )
        )

    return cameras


def load_settings() -> Settings:
    """อ่านค่าทั้งหมดจาก environment ครั้งเดียวตอน import โมดูลนี้"""
    frame_source = _get_str("FRAME_SOURCE", "browser").lower()
    if frame_source not in VALID_FRAME_SOURCES:
        raise ConfigError(
            f"ค่า FRAME_SOURCE ไม่ถูกต้อง: {frame_source!r} "
            f"(รองรับเฉพาะ {' หรือ '.join(VALID_FRAME_SOURCES)})"
        )

    identifier_mode = _get_str("IDENTIFIER", "face").lower()
    if identifier_mode not in VALID_IDENTIFIERS:
        raise ConfigError(
            f"ค่า IDENTIFIER ไม่ถูกต้อง: {identifier_mode!r} "
            f"(รองรับเฉพาะ {' หรือ '.join(VALID_IDENTIFIERS)})"
        )

    cameras = _load_cameras(require_credentials=(frame_source == "rtsp"))
    enabled_cameras = [c for c in cameras if c.enabled]

    if frame_source == "rtsp" and not enabled_cameras:
        raise ConfigError(
            "ตั้ง FRAME_SOURCE=rtsp ไว้ แต่ไม่มีกล้องที่เปิดใช้งานเลย\n"
            "ตรวจค่า CAMERA_IDS ในไฟล์ .env และค่า ..._ENABLED ของกล้องแต่ละตัว"
        )

    # ---- งบการตรวจจับ: ต่อกล้อง กับ รวมทั้งระบบ (เฟส 8) ----
    # ไม่ได้ตั้งเพดานรวมไว้ = คิดให้เองจาก "ต่อกล้อง x จำนวนกล้องที่เปิดใช้"
    # ผลคือระบบกล้องตัวเดียวได้พฤติกรรมเดิมเป๊ะ ส่วนสองกล้องก็ยังได้เต็มที่ทั้งคู่
    # ถ้าเครื่องทำไม่ไหวค่อยลด DETECT_FPS_TOTAL ลงทีหลัง โดยไม่ต้องแตะ DETECT_FPS
    detect_fps = _get_int("DETECT_FPS", 5)
    detect_fps_total = _get_int("DETECT_FPS_TOTAL", 0)
    if detect_fps_total <= 0:
        detect_fps_total = detect_fps * max(1, len(enabled_cameras))

    # การพลิกกระจกเป็นธรรมเนียมของ "โหมดเว็บแคมเดี่ยว" เท่านั้น
    # (ให้ผู้ใช้เห็นตัวเองเหมือนส่องกระจก ซึ่งเป็นธรรมชาติกว่าตอนทดสอบ)
    #
    # กล้อง IP ที่ติดอยู่ที่ประตูไม่มีเหตุผลที่จะพลิก และถ้าปล่อยให้ค่านี้เป็น true
    # ในโหมดกล้องหลายตัวจะเกิดปัญหาร้ายแรงคือ backend คำนวณทิศทางแบบพลิกด้าน
    # แต่หน้าเว็บแสดงภาพไม่พลิก ทำให้ป้ายทิศทางสวนทางกับภาพที่เห็น
    # จึงบังคับให้เป็น false เสมอในโหมดกล้องหลายตัว ไม่ว่าจะตั้งค่าอะไรไว้ก็ตาม
    #
    # รวมถึงกล้องที่ตั้ง SOURCE=browser ในโหมดนี้ด้วย: ภาพเว็บแคมถูกส่งขึ้นมาแบบไม่พลิก
    # แล้ว backend ส่งกลับไปแสดงเป็นภาพเดียวกัน จึงไม่มีการพลิกที่จุดไหนเลย
    # ผลพลอยได้คือ "ซ้ายบนจอ" กับ "ซ้ายที่กล้องเห็น" ตรงกันพอดี
    # ทิศทาง IN/OUT ในเฟสถัดไปจึงคำนวณจากกล้องทั้งสองแบบได้เหมือนกัน
    mirror = _get_bool("CAMERA_MIRROR", True) and frame_source == "browser"

    direction_reference = _get_str("DIRECTION_REFERENCE", "screen").lower()
    if direction_reference not in VALID_DIRECTION_REFERENCES:
        raise ConfigError(
            f"ค่า DIRECTION_REFERENCE ไม่ถูกต้อง: {direction_reference!r} "
            f"(รองรับเฉพาะ {' หรือ '.join(VALID_DIRECTION_REFERENCES)})"
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
            rec_pack=_get_str("FACE_REC_PACK", "buffalo_s"),
        ),
        identify=IdentifySettings(
            mode=identifier_mode,
            similarity_threshold=_get_float("SIMILARITY_THRESHOLD", 0.35),
            max_faces_per_frame=_get_int("IDENTIFY_MAX_FACES_PER_FRAME", 4),
            vote_window=_get_int("IDENTITY_VOTE_WINDOW", 7),
            min_votes=_get_int("IDENTITY_MIN_VOTES", 3),
            recheck_interval=_get_int("IDENTITY_RECHECK_INTERVAL", 15),
        ),
        direction=DirectionSettings(
            reference=direction_reference,
            frame_gap=_get_int("DIRECTION_FRAME_GAP", 8),
            min_shift_ratio=_get_float("DIRECTION_MIN_SHIFT_RATIO", 0.025),
            hysteresis=_get_float("DIRECTION_HYSTERESIS", 0.6),
            vote_window=_get_int("DIRECTION_VOTE_WINDOW", 5),
        ),
        rtsp=RTSPSettings(
            cameras=cameras,
            ffmpeg_options=_get_str(
                "RTSP_FFMPEG_OPTIONS",
                "rtsp_transport;tcp|stimeout;5000000",
            ),
            watchdog_timeout=_get_float("RTSP_WATCHDOG_TIMEOUT", 5.0),
            reconnect_initial_delay=_get_float("RTSP_RECONNECT_INITIAL_DELAY", 1.0),
            reconnect_max_delay=_get_float("RTSP_RECONNECT_MAX_DELAY", 30.0),
        ),
        rates=RateSettings(
            capture_fps=_get_int("CAPTURE_FPS", 15),
            detect_fps=detect_fps,
            detect_fps_total=detect_fps_total,
            stream_fps=_get_int("STREAM_FPS", 15),
            stale_after_frames=_get_int("STALE_AFTER_FRAMES", 6),
        ),
        stream=StreamSettings(
            source=frame_source,
            target_width=_get_int("STREAM_TARGET_WIDTH", 640),
            send_fps=_get_int("STREAM_SEND_FPS", 10),
            jpeg_quality=_get_float("STREAM_JPEG_QUALITY", 0.7),
            mirror=mirror,
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
