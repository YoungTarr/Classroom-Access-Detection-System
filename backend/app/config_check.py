"""ตรวจความสมเหตุสมผลของ config กล้องตอนสตาร์ท (เฟส 10)

============================================================================
ทำไมต้องมีไฟล์นี้ แยกจาก config.py
============================================================================

config.py ทำหน้าที่ "อ่านค่า" และฟ้องเฉพาะค่าที่ใช้ต่อไม่ได้เลย (ConfigError = หยุดระบบ)
แต่มีความผิดพลาดอีกกลุ่มที่ระบบ "ยังรันได้" จึงไม่เคยมีอะไรฟ้อง เช่น

    ตอนทดสอบชี้กล้องทั้งสองตัวไปที่ IP เดียวกัน (กล้องตัวเดียวปล่อยสองสตรีม)
    พอได้กล้องจริงมาแล้วลืมแก้ CAMERA_2_HOST ระบบก็รันต่อเงียบ ๆ
    หน้าเว็บขึ้น "ปกติทุกกล้องที่ติดตั้ง 2/2" ทั้งที่จริงเป็นกล้องตัวเดียวกัน

ไฟล์นี้จับความผิดพลาดกลุ่มนั้นแล้ว "เตือน" เท่านั้น ไม่หยุดระบบ
เพราะบางกรณีตั้งใจทำจริง (เช่นทดสอบด้วยกล้องตัวเดียว) การหยุดระบบจะขวางงานทดสอบ
คำเตือนถูกส่งออกไปสามทาง: log ตอนสตาร์ท / GET /api/health / แถบเหลืองบนหน้าเว็บ

กฎของข้อความเตือน: ต้องบอก "ชื่อตัวแปรใน .env" ที่ต้องแก้เสมอ
และห้ามมีรหัสผ่านอยู่ในข้อความเด็ดขาด (ข้อความนี้ออกไปถึงหน้าเว็บ)
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import asdict, dataclass, field

from app.config import CameraSettings, Settings

# ชื่อ host ที่ยอมรับ (กรณีไม่ได้ใส่เป็นตัวเลข IP) เช่น tapo-door.local
# แต่ละช่วงคั่นด้วยจุด ห้ามช่วงว่าง จึงจับ "จุดเกินท้าย" หรือ "จุดซ้อน" ได้ด้วย
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*$")

# คำแนะนำท้ายทุกคำเตือน - แก้ .env แล้ว restart เฉย ๆ ไม่อ่านค่าใหม่
APPLY_HINT = "แก้ไฟล์ .env แล้วสั่ง docker compose up -d backend (ใช้ restart ไม่ได้ เพราะไม่อ่าน .env ใหม่)"


@dataclass(frozen=True)
class ConfigWarning:
    """คำเตือนหนึ่งข้อ"""

    # รหัสสั้น ๆ ไว้ให้โปรแกรมแยกประเภทได้ (หน้าเว็บ/สคริปต์ตรวจรับ)
    code: str

    # ข้อความภาษาไทยที่อ่านแล้วรู้ทันทีว่าต้องแก้อะไร
    message: str

    # กล้องที่เกี่ยวข้อง (ว่างได้ถ้าเป็นเรื่องของทั้งระบบ)
    cameras: list[str] = field(default_factory=list)

    # ชื่อตัวแปรใน .env ที่ควรไปดู
    env_vars: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _raw(name: str) -> str | None:
    """ค่าดิบจาก environment ก่อนถูก strip

    config.py ตัดช่องว่างหัว-ท้ายทิ้งให้อัตโนมัติ จึงต้องอ่านค่าดิบเองที่นี่
    ถึงจะรู้ว่าในไฟล์ .env มีช่องว่างปนมาหรือเปล่า
    """
    return os.environ.get(name)


def _label(camera: CameraSettings) -> str:
    """ชื่อกล้องสำหรับใส่ในข้อความ เช่น กล้อง 2 (ประตูทางออก, OUT)"""
    return f"กล้อง {camera.id} ({camera.name}, {camera.direction})"


def _host_problem(host: str) -> str | None:
    """ตรวจรูปแบบของ HOST คืนคำอธิบายปัญหาเป็นภาษาไทย หรือ None ถ้าดูปกติ"""
    if re.search(r"\s", host):
        return "มีช่องว่างปนอยู่กลางค่า"
    if "://" in host or "@" in host or "/" in host:
        return "ใส่แค่ IP หรือชื่อเครื่องก็พอ ไม่ต้องมี rtsp://, บัญชีผู้ใช้ หรือ path"
    if ":" in host:
        return "ไม่ต้องใส่พอร์ตต่อท้าย HOST ให้ใส่ใน ..._PORT แทน"

    # ประกอบด้วยตัวเลขกับจุดล้วน = ตั้งใจใส่เป็น IPv4 ต้องถูกรูปแบบเป๊ะ
    if all(ch.isdigit() or ch == "." for ch in host):
        if host.endswith("."):
            return "รูปแบบ IP ผิด: มีจุดเกินท้าย"
        if host.startswith("."):
            return "รูปแบบ IP ผิด: มีจุดเกินข้างหน้า"
        if ".." in host:
            return "รูปแบบ IP ผิด: มีจุดซ้อนกัน"
        parts = host.split(".")
        if len(parts) != 4:
            return f"รูปแบบ IP ผิด: ต้องมี 4 ส่วนคั่นด้วยจุด แต่มี {len(parts)} ส่วน"
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return "รูปแบบ IP ผิด: แต่ละส่วนต้องอยู่ระหว่าง 0-255 และไม่ขึ้นต้นด้วย 0"
        return None

    if host.endswith("."):
        return "มีจุดเกินท้าย"
    if not _HOSTNAME_RE.match(host):
        return "มีอักขระที่ใช้ในชื่อเครื่องไม่ได้"
    return None


def _check_single_camera(camera: CameraSettings) -> list[ConfigWarning]:
    """ตรวจค่าของกล้องตัวเดียว"""
    warnings: list[ConfigWarning] = []
    prefix = camera.env_prefix
    label = _label(camera)

    # ---- ช่องว่างปนมากับค่า (มักเกิดจากคัดลอกมาวาง) ----
    # config.py ตัดหัว-ท้ายให้แล้ว ระบบจึงยังใช้ได้ แต่ถ้าเป็นรหัสผ่านที่คัดลอกมาผิด
    # ผู้ใช้จะงงว่าทำไมต่อไม่ได้ จึงบอกไว้ให้เห็น (ไม่แสดงค่าจริง กันรหัสผ่านหลุด)
    for suffix in ("HOST", "USER", "PASSWORD", "PORT", "PATH"):
        name = f"{prefix}{suffix}"
        raw = _raw(name)
        if raw is None or raw.strip() == "":
            continue
        if raw != raw.strip():
            warnings.append(ConfigWarning(
                code="whitespace",
                message=(
                    f"{label}: ค่า {name} มีช่องว่างอยู่หัวหรือท้าย "
                    f"(ระบบตัดทิ้งให้แล้ว แต่ควรลบออกจาก .env ให้เรียบร้อย "
                    f"และตรวจว่าคัดลอกค่ามาครบถูกต้อง)"
                ),
                cameras=[camera.id],
                env_vars=[name],
            ))
        elif suffix in ("USER", "PORT", "PATH") and re.search(r"\s", raw):
            # รหัสผ่านอาจมีช่องว่างกลางได้จริง จึงไม่ตรวจข้อนี้กับ PASSWORD
            warnings.append(ConfigWarning(
                code="whitespace",
                message=f"{label}: ค่า {name} มีช่องว่างปนอยู่กลางค่า",
                cameras=[camera.id],
                env_vars=[name],
            ))

    if not camera.installed:
        # HOST ว่าง = ยังไม่ได้ติดตั้ง (เฟส 9) ไม่ใช่ความผิดพลาด
        # แต่ถ้าตั้งบัญชีเฉพาะของกล้องตัวนี้ไว้แล้ว น่าจะติดตั้งแล้วแต่ลืมใส่ IP
        own = [
            f"{prefix}{s}" for s in ("USER", "PASSWORD")
            if (_raw(f"{prefix}{s}") or "").strip()
        ]
        if own:
            warnings.append(ConfigWarning(
                code="missing_host",
                message=(
                    f"{label}: ตั้ง {', '.join(own)} ไว้แล้ว แต่ {prefix}HOST ยังว่าง "
                    f"ระบบจึงถือว่ายังไม่ได้ติดตั้งกล้องตัวนี้ "
                    f"ถ้าติดตั้งแล้วให้ใส่ IP ของกล้องใน {prefix}HOST"
                ),
                cameras=[camera.id],
                env_vars=[f"{prefix}HOST"],
            ))
        return warnings

    # ---- รูปแบบของ HOST ----
    problem = _host_problem(camera.host)
    if problem:
        warnings.append(ConfigWarning(
            code="bad_host",
            message=f"{label}: {prefix}HOST={camera.host!r} {problem}",
            cameras=[camera.id],
            env_vars=[f"{prefix}HOST"],
        ))

    # ---- บัญชีผู้ใช้ไม่ครบ ----
    # ดูจาก "ค่าที่ใช้จริง" หลังรวมค่ากลางแล้ว ถ้า CAMERA_DEFAULT_USER มีค่า
    # การไม่ตั้ง CAMERA_2_USER ไม่ใช่ปัญหา เพราะระบบใช้ค่ากลางแทนให้
    missing = []
    if not camera.username:
        missing.append(("USER", "CAMERA_DEFAULT_USER"))
    if not camera.password:
        missing.append(("PASSWORD", "CAMERA_DEFAULT_PASSWORD"))
    if missing:
        names = [f"{prefix}{s}" for s, _ in missing]
        defaults = [d for _, d in missing]
        warnings.append(ConfigWarning(
            code="missing_credentials",
            message=(
                f"{label}: config ไม่ครบ ขาด {' และ '.join(names)} "
                f"(และค่ากลาง {' / '.join(defaults)} ก็ว่าง) "
                f"กล้องจะปฏิเสธการเชื่อมต่อ — ใส่บัญชี Camera Account ที่ตั้งในแอป Tapo"
            ),
            cameras=[camera.id],
            env_vars=names,
        ))

    # ---- พอร์ต ----
    if not 1 <= camera.port <= 65535:
        warnings.append(ConfigWarning(
            code="bad_port",
            message=f"{label}: พอร์ต {camera.port} อยู่นอกช่วง 1-65535 (กล้อง Tapo ใช้ 554)",
            cameras=[camera.id],
            env_vars=[f"{prefix}PORT", "CAMERA_DEFAULT_PORT"],
        ))

    return warnings


def _check_duplicate_hosts(cameras: list[CameraSettings]) -> list[ConfigWarning]:
    """กล้องตั้งแต่ 2 ตัวขึ้นไปชี้ไป host เดียวกัน - ต้นเหตุของปัญหาที่ทำให้เกิดไฟล์นี้"""
    groups: dict[str, list[CameraSettings]] = {}
    for camera in cameras:
        # ไม่สนตัวพิมพ์เล็กใหญ่และจุดท้าย เพื่อจับกรณีที่ต่างกันแค่หน้าตา
        key = camera.host.lower().rstrip(".")
        groups.setdefault(key, []).append(camera)

    warnings = []
    for host, group in groups.items():
        if len(group) < 2:
            continue
        names = ", ".join(_label(c) for c in group)
        paths = {c.path for c in group}
        same_stream = (
            " และใช้ path เดียวกันด้วย = ภาพจากสตรีมเดียวกันเป๊ะ"
            if len(paths) == 1 else ""
        )
        warnings.append(ConfigWarning(
            code="duplicate_host",
            message=(
                f"กล้องหลายตัวชี้ไปที่ IP เดียวกัน ({host}): {names}{same_stream}\n"
                f"ภาพทุกจอจึงมาจากกล้องตัวเดียว ไม่ใช่กล้องแยกกัน — "
                f"อาจลืมแก้ค่าทดสอบหลังได้กล้องจริงมา "
                f"ให้ใส่ IP ของกล้องแต่ละตัวใน "
                f"{' / '.join(c.env_prefix + 'HOST' for c in group)} ให้ไม่ซ้ำกัน"
            ),
            cameras=[c.id for c in group],
            env_vars=[c.env_prefix + "HOST" for c in group],
        ))
    return warnings


def _check_duplicate_directions(cameras: list[CameraSettings]) -> list[ConfigWarning]:
    """กล้องหลายตัวตั้ง direction เหมือนกัน - ระบบจะไม่มีกล้องจับอีกทิศเลย"""
    groups: dict[str, list[CameraSettings]] = {}
    for camera in cameras:
        groups.setdefault(camera.direction, []).append(camera)

    warnings = []
    for direction, group in groups.items():
        if len(group) < 2:
            continue
        others = [d for d in ("IN", "OUT") if d != direction]
        missing_note = (
            f" และไม่มีกล้องตัวไหนเป็น {others[0]} เลย"
            if others and others[0] not in groups else ""
        )
        warnings.append(ConfigWarning(
            code="duplicate_direction",
            message=(
                f"กล้องหลายตัวตั้งทิศทางเป็น {direction} เหมือนกัน: "
                f"{', '.join(_label(c) for c in group)}{missing_note}\n"
                f"ตรวจค่า {' / '.join(c.env_prefix + 'DIRECTION' for c in group)} "
                f"(ปกติกล้องขาเข้าเป็น IN ขาออกเป็น OUT — ถ้าตั้งใจมีหลายประตูข้ามข้อนี้ได้)"
            ),
            cameras=[c.id for c in group],
            env_vars=[c.env_prefix + "DIRECTION" for c in group],
        ))
    return warnings


def check_camera_config(settings: Settings) -> list[ConfigWarning]:
    """ตรวจ config กล้องทั้งหมด คืนรายการคำเตือน (ว่าง = ไม่พบปัญหา)

    ตรวจเฉพาะโหมด rtsp เพราะโหมดเว็บแคมเดี่ยวไม่ได้ใช้ค่ากล้องเลย
    เตือนในโหมดนั้นจะกลายเป็นเสียงรบกวนที่ผู้ใช้ทำอะไรไม่ได้

    ฟังก์ชันนี้ห้ามโยน exception ออกไป - คำเตือนต้องไม่ทำให้ระบบหยุด
    """
    if settings.stream.source != "rtsp":
        return []

    enabled = [c for c in settings.rtsp.cameras if c.enabled]
    installed = [c for c in enabled if c.installed]

    warnings: list[ConfigWarning] = []
    for camera in enabled:
        warnings.extend(_check_single_camera(camera))

    # host ซ้ำ นับเฉพาะกล้องที่ติดตั้งแล้ว (HOST ว่างสองตัวไม่ใช่ "IP ซ้ำ")
    warnings.extend(_check_duplicate_hosts(installed))

    # ทิศทางซ้ำ นับทุกตัวที่เปิดใช้ แม้ยังไม่ได้ติดตั้ง
    # เพราะเป็นค่าที่ควรตั้งให้ถูกไว้ก่อนตั้งแต่ตอนยังไม่มีกล้อง
    warnings.extend(_check_duplicate_directions(enabled))

    return warnings
