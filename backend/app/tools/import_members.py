"""นำเข้าสมาชิก + รูปใบหน้าจากไฟล์ที่ export ออกมาจาก Google Form

ไฟล์ต้นทางมาจาก scripts/google_form_export.gs (โฟลเดอร์ CADS-export ใน Google Drive)
ดาวน์โหลดเป็น .zip แล้ววางไว้ที่ data/import/ (วางกี่ไฟล์ก็ได้ ระบบรวมให้เอง)
ข้างในมี
    responses.csv                              row, student_id, email, first_name, last_name
    <รหัส>_<front|left|right>_r<แถว>.<ext>     รูปของคำตอบแถวนั้น

============================================================================
วิธีรัน (ดู scripts/import-members.ps1 / .sh ที่ห่อคำสั่งนี้ไว้ให้แล้ว)
============================================================================

    python -m app.tools.import_members            โหมดทดลอง: แสดงแผน ไม่เขียนอะไรเลย
    python -m app.tools.import_members --apply    บันทึกจริง

============================================================================
กติกา
============================================================================

- หนึ่งรหัสนักศึกษา = สมาชิกหนึ่งคน ถ้าส่งซ้ำหลายรอบใช้คำตอบ "แถวล่าสุด"
- รหัสต้องเป็นตัวเลข 8 หลัก และต้องตรงกับเลขหน้า @ ของอีเมลสถาบัน
  ไม่ตรงกัน = ไม่นำเข้าแถวนั้น (กรอกผิดฝั่งไหนก็ได้ ต้องให้คนตัดสิน)
  ถ้ามั่นใจว่าอีเมลถูก ใช้ --id-from-email ให้ใช้รหัสจากอีเมลแทน
- ตัดคำนำหน้าชื่อ (นาย/นางสาว/นาง/น.ส.) ออก ให้ทุกคนแสดงชื่อแบบเดียวกัน
- รูปแปลงเป็น JPEG ย่อด้านยาวไม่เกิน 1600px แล้วเก็บที่ data/faces/<รหัส>/<มุม>.jpg
  และต้องตรวจเจอใบหน้า (ถ้าเจอหลายใบ หน้าใหญ่สุดต้องใหญ่กว่าใบอื่นชัดเจน) ไม่งั้นแจ้งปัญหา
  มุมนั้นจะไม่ถูกนำเข้า แต่มุมอื่นของคนเดียวกันยังนำเข้าได้
- รูปที่เหมือนของเดิมทุกไบต์จะข้าม ไม่เขียนทับ
- รหัสที่มีในฐานข้อมูลอยู่แล้วแต่ชื่อไม่ตรง จะไม่แก้ชื่อให้ (อาจกรอกรหัสคนอื่น)
  ถ้าตั้งใจแก้ชื่อจริง ใช้ --allow-rename
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.db.database import database
from app.face.detector import face_detector
from app.identify.members import PHOTO_ANGLES

IMPORT_DIR = Path("/data/import")
# backend service mount data/faces แบบอ่านอย่างเดียว ตัวนำเข้าจึงเขียนผ่านจุด mount แยกนี้
FACES_OUT_DIR = Path("/data/faces-rw")
RELOAD_URL = "http://backend:8000/api/faces/reload"

MAX_SIDE = 1600
JPEG_QUALITY = 92

# รูปที่ติดหน้าคนอื่นมาด้วย: รับได้ถ้าหน้าใหญ่สุดมีพื้นที่ใหญ่กว่าหน้าที่สองอย่างน้อยกี่เท่า
DOMINANT_FACE_RATIO = 4.0

# เรียงจากยาวไปสั้น ไม่งั้น "นาง" จะตัด "นางสาว" ไม่หมด
TITLE_PREFIXES = ("นางสาว", "น.ส.", "นาย", "นาง", "Miss", "Mrs.", "Mr.", "Ms.")
STUDENT_ID_RE = re.compile(r"^\d{8}$")
FILE_RE = re.compile(
    r"^(?P<sid>[^_/]+)_(?P<angle>front|left|right)_r(?P<row>\d+)(?:_\d+)?\.(?P<ext>[A-Za-z0-9]+)$"
)


@dataclass
class Response:
    row: int
    student_id: str
    file_sid: str  # รหัสตามที่กรอกในฟอร์ม (ใช้ตั้งชื่อไฟล์รูป) อาจต่างจาก student_id ถ้าแก้ตามอีเมล
    email: str
    first_name: str
    last_name: str
    title_removed: bool = False


@dataclass
class Plan:
    student_id: str
    first_name: str
    last_name: str
    row: int
    status: str = ""  # ใหม่ / อัปเดต / ไม่เปลี่ยน / ข้าม
    rename: bool = False
    photos: dict[str, bytes] = field(default_factory=dict)  # มุม -> JPEG ที่จะเขียน
    unchanged_photos: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# อ่านไฟล์ต้นทาง
# ---------------------------------------------------------------------------
def read_sources(
    import_dir: Path,
) -> tuple[list[dict[str, str]], dict[tuple[str, str, int], list[bytes]]]:
    """รวม responses.csv และรูปจากทุก zip (และโฟลเดอร์ที่แตกไว้แล้ว) ใน import_dir

    รูปหนึ่งมุมอาจมีหลายไฟล์ เช่น .heic ต้นฉบับ + .jpg ที่ heic_to_jpg.py แปลงไว้
    จึงเก็บไว้ทุกไฟล์ แล้วให้ to_jpeg เลือกไฟล์แรกที่เปิดได้
    """
    csv_rows: dict[int, dict[str, str]] = {}
    images: dict[tuple[str, str, int], list[bytes]] = {}

    def take(name: str, data_fn) -> None:
        base = Path(name).name
        if base == "responses.csv":
            text = data_fn().decode("utf-8-sig")
            for rec in csv.DictReader(io.StringIO(text)):
                csv_rows[int(rec["row"])] = rec
            return
        m = FILE_RE.match(base)
        if m:
            key = (m["sid"], m["angle"], int(m["row"]))
            images.setdefault(key, []).append(data_fn())

    for path in sorted(import_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if not info.is_dir():
                        take(info.filename, lambda i=info: zf.read(i))
        else:
            take(path.name, path.read_bytes)

    return [csv_rows[k] for k in sorted(csv_rows)], images


def strip_title(name: str) -> tuple[str, bool]:
    name = name.strip()
    for prefix in TITLE_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix) + 1:
            return name[len(prefix):].strip(), True
    return name, False


def to_jpeg(candidates: list[bytes]) -> tuple[bytes | None, np.ndarray | None, str | None]:
    """ถอดรหัสไฟล์แรกที่เปิดได้ (หมุนตาม EXIF) ย่อ แล้วเข้ารหัสเป็น JPEG คืน (jpeg, ภาพ, ปัญหา)"""
    image = None
    for raw in candidates:
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is not None:
            break
    if image is None:
        return None, None, "เปิดไฟล์รูปไม่ได้ (ถ้าเป็น HEIC ต้องรันผ่าน scripts/import-members ที่แปลงให้ก่อน)"
    height, width = image.shape[:2]
    scale = MAX_SIDE / max(height, width)
    if scale < 1.0:
        image = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return None, None, "เข้ารหัส JPEG ไม่สำเร็จ"
    return buffer.tobytes(), image, None


# ---------------------------------------------------------------------------
# วางแผน
# ---------------------------------------------------------------------------
def build_plans(args: argparse.Namespace) -> tuple[list[Plan], list[str]]:
    csv_rows, images = read_sources(args.import_dir)
    if not csv_rows:
        raise SystemExit(f"ไม่พบ responses.csv ใน {args.import_dir} - วางไฟล์ zip จาก CADS-export ไว้ที่ data/import/ ก่อน")

    skipped: list[str] = []
    latest: dict[str, Response] = {}
    older_rows: dict[str, list[int]] = {}

    for rec in csv_rows:
        row = int(rec["row"])
        sid = file_sid = rec["student_id"].strip()
        email = rec.get("email", "").strip()
        email_id = email.split("@")[0] if "@" in email else ""

        if email_id and email_id != sid:
            if args.id_from_email and STUDENT_ID_RE.match(email_id):
                sid = email_id
            else:
                skipped.append(
                    f"แถว {row}: รหัสที่กรอก {sid} ไม่ตรงกับอีเมล {email} "
                    f"(ถ้าอีเมลถูก รันใหม่พร้อม --id-from-email)"
                )
                continue
        if not STUDENT_ID_RE.match(sid):
            skipped.append(f"แถว {row}: รหัส '{sid}' ไม่ใช่ตัวเลข 8 หลัก")
            continue

        first, removed = strip_title(rec["first_name"])
        resp = Response(row, sid, file_sid, email, first, rec["last_name"].strip(), removed)
        if sid in latest:
            older_rows.setdefault(sid, []).append(latest[sid].row)
        latest[sid] = resp  # csv_rows เรียงตามแถวแล้ว ตัวท้ายสุด = ส่งล่าสุด

    existing = {m["student_id"]: m for m in database.fetch_members()}
    face_detector.load()

    plans: list[Plan] = []
    for sid, resp in sorted(latest.items()):
        plan = Plan(sid, resp.first_name, resp.last_name, resp.row)
        if resp.title_removed:
            plan.notes.append("ตัดคำนำหน้าชื่อออก")
        if sid in older_rows:
            plan.notes.append(f"ส่งซ้ำ ใช้แถวล่าสุด {resp.row} (ข้ามแถว {', '.join(map(str, older_rows[sid]))})")

        old = existing.get(sid)
        if old is not None and (old["first_name"], old["last_name"]) != (plan.first_name, plan.last_name):
            msg = f"ชื่อในระบบเดิมคือ '{old['first_name']} {old['last_name']}'"
            if args.allow_rename:
                plan.notes.append(msg + " -> แก้เป็นชื่อใหม่")
                plan.rename = True
            else:
                # อาจกรอกรหัสของคนอื่น ห้ามแตะทั้งชื่อและรูปของรหัสนี้ จนกว่าคนจะตรวจ
                plan.problems.append(msg + " ไม่ตรงกับฟอร์ม -> ข้ามทั้งคน (ใช้ --allow-rename ถ้าตั้งใจแก้ชื่อ)")
                plan.status = "ข้าม"
                plans.append(plan)
                continue

        for angle in PHOTO_ANGLES:
            raw = images.get((resp.file_sid, angle, resp.row))
            if raw is None:
                plan.problems.append(f"ไม่มีรูปมุม {angle}")
                continue
            jpeg, image, problem = to_jpeg(raw)
            if problem:
                plan.problems.append(f"รูป {angle}: {problem}")
                continue
            faces = sorted(face_detector.detect(image), key=lambda f: f.w * f.h, reverse=True)
            if not faces:
                plan.problems.append(f"รูป {angle}: ตรวจไม่เจอใบหน้า จึงไม่นำเข้ามุมนี้")
                continue
            if len(faces) > 1:
                # ตอนสร้างคลัง ระบบเลือกหน้าที่ใหญ่ที่สุด (face_identifier.py) จึงรับได้
                # ถ้าหน้าใหญ่สุดใหญ่กว่าหน้าอื่นมากจนไม่มีทางเลือกผิดคน
                # และตัวตรวจจับต้องมั่นใจในหน้าใหญ่สุดไม่น้อยกว่าใบอื่น
                # (กรอบใหญ่แต่คะแนนต่ำมักเป็นของที่ไม่ใช่หน้า เช่น มือ/ผมที่ถูกจับรวมเข้าไป)
                biggest, second = faces[0].w * faces[0].h, faces[1].w * faces[1].h
                if biggest < DOMINANT_FACE_RATIO * second or faces[0].score < faces[1].score:
                    plan.problems.append(
                        f"รูป {angle}: เจอ {len(faces)} ใบหน้า ไม่แน่ใจว่าใบไหนเป็นเจ้าของรูป จึงไม่นำเข้ามุมนี้"
                    )
                    continue
                plan.notes.append(f"รูป {angle}: เจอ {len(faces)} ใบหน้า ใช้ใบที่ใหญ่ที่สุด (ใหญ่กว่าใบอื่นชัดเจน)")
            target = FACES_OUT_DIR / sid / f"{angle}.jpg"
            if target.exists() and target.read_bytes() == jpeg:
                plan.unchanged_photos.append(angle)
            else:
                plan.photos[angle] = jpeg

        if old is None:
            plan.status = "ใหม่"
        elif plan.photos or plan.rename:
            plan.status = "อัปเดต"
        else:
            plan.status = "ไม่เปลี่ยน"
        plans.append(plan)

    return plans, skipped


# ---------------------------------------------------------------------------
# บันทึกจริง
# ---------------------------------------------------------------------------
UPSERT_SQL = """
    INSERT INTO members (student_id, first_name, last_name, photo_left, photo_front, photo_right)
    VALUES (%(student_id)s, %(first_name)s, %(last_name)s, %(left)s, %(front)s, %(right)s)
    ON CONFLICT (student_id) DO UPDATE SET
        first_name  = CASE WHEN %(rename)s THEN EXCLUDED.first_name ELSE members.first_name END,
        last_name   = CASE WHEN %(rename)s THEN EXCLUDED.last_name  ELSE members.last_name  END,
        photo_left  = COALESCE(EXCLUDED.photo_left,  members.photo_left),
        photo_front = COALESCE(EXCLUDED.photo_front, members.photo_front),
        photo_right = COALESCE(EXCLUDED.photo_right, members.photo_right)
"""


def apply(plans: list[Plan]) -> None:
    with database.pool.connection() as conn:
        for plan in plans:
            if plan.status in ("ไม่เปลี่ยน", "ข้าม"):
                continue
            folder = FACES_OUT_DIR / plan.student_id
            folder.mkdir(parents=True, exist_ok=True)
            for angle, jpeg in plan.photos.items():
                (folder / f"{angle}.jpg").write_bytes(jpeg)

            have = set(plan.photos) | set(plan.unchanged_photos)
            paths = {
                angle: (f"data/faces/{plan.student_id}/{angle}.jpg" if angle in have else None)
                for angle in PHOTO_ANGLES
            }
            conn.execute(UPSERT_SQL, {
                "student_id": plan.student_id,
                "first_name": plan.first_name,
                "last_name": plan.last_name,
                "rename": plan.rename,
                **paths,
            })


def reload_backend() -> str:
    try:
        with urllib.request.urlopen(RELOAD_URL, timeout=300) as res:
            data = json.loads(res.read().decode("utf-8"))
        return f"โหลดคลังใบหน้าใหม่แล้ว: {json.dumps(data.get('summary', data), ensure_ascii=False)[:300]}"
    except Exception as exc:  # noqa: BLE001
        return f"สั่งโหลดคลังใบหน้าใหม่ไม่สำเร็จ ({exc}) - กดปุ่ม 'โหลดรูปใบหน้าใหม่' บนหน้าเว็บแทน"


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="นำเข้าสมาชิกจาก Google Form (CADS-export)")
    parser.add_argument("--apply", action="store_true", help="บันทึกจริง (ไม่ใส่ = โหมดทดลอง)")
    parser.add_argument("--id-from-email", action="store_true",
                        help="รหัสที่กรอกไม่ตรงกับอีเมล ให้ใช้รหัสจากอีเมล")
    parser.add_argument("--allow-rename", action="store_true",
                        help="ยอมแก้ชื่อ-นามสกุลของสมาชิกที่มีอยู่แล้วให้ตรงกับฟอร์ม")
    parser.add_argument("--import-dir", type=Path, default=IMPORT_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args()

    database.connect()
    plans, skipped = build_plans(args)

    print("=" * 72)
    print("โหมดทดลอง (ยังไม่บันทึกอะไร)" if not args.apply else "บันทึกจริง")
    print("=" * 72)
    for plan in plans:
        mark = "!" if plan.problems else " "
        angles = ", ".join(plan.photos) or "-"
        print(f"{mark} [{plan.status:<9}] {plan.student_id} {plan.first_name} {plan.last_name}"
              f"  (แถว {plan.row}, รูปที่จะเขียน: {angles})")
        for note in plan.notes:
            print(f"      - {note}")
        for problem in plan.problems:
            print(f"      ! {problem}")

    counts = {s: sum(p.status == s for p in plans) for s in ("ใหม่", "อัปเดต", "ไม่เปลี่ยน", "ข้าม")}
    with_problems = sum(bool(p.problems) for p in plans)
    print("-" * 72)
    print(f"สรุป: ใหม่ {counts['ใหม่']} / อัปเดต {counts['อัปเดต']} / ไม่เปลี่ยน {counts['ไม่เปลี่ยน']} / ข้าม {counts['ข้าม']}"
          f" / มีปัญหา {with_problems} คน / ข้ามทั้งแถว {len(skipped)}")
    for line in skipped:
        print(f"  ข้าม {line}")

    if not args.apply:
        print("\nตรวจรายการด้านบนแล้ว ถ้าถูกต้องให้รันซ้ำพร้อม --apply เพื่อบันทึกจริง")
        return 0

    apply(plans)
    print("\nบันทึกลงฐานข้อมูลและ data/faces/ เรียบร้อย")
    print(reload_backend())
    return 0


if __name__ == "__main__":
    sys.exit(main())
