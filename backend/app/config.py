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
    version: str = "0.1.0"  # เฟส 1: ฐานข้อมูล + โครงสร้าง Docker
    log_level: str = "INFO"
    timezone: str = "Asia/Bangkok"

    # origin ที่อนุญาตให้เรียกข้ามโดเมน ปกติเป็นลิสต์ว่างเพราะ nginx proxy ให้แล้ว
    cors_origins: list[str] = field(default_factory=list)

    # โฟลเดอร์เก็บรูปใบหน้า (path ภายใน container)
    faces_dir: Path = Path("/data/faces")


@dataclass(frozen=True)
class Settings:
    """รวมทุกกลุ่มไว้ในที่เดียว เรียกใช้ผ่านตัวแปร settings ด้านล่าง"""

    app: AppSettings
    database: DatabaseSettings


def load_settings() -> Settings:
    """อ่านค่าทั้งหมดจาก environment ครั้งเดียวตอน import โมดูลนี้"""
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
    )


# instance เดียวใช้ร่วมกันทั้งระบบ
settings = load_settings()
