"""แปลงรูป HEIC/HEIF (รูปจาก iPhone) ใน data/import/ ให้เป็น JPEG ก่อนนำเข้าสมาชิก

OpenCV ที่ backend ใช้อ่านรูปเปิด HEIC ไม่ได้ จึงแปลงไว้ก่อนด้วย pillow-heif
รันใน container python ชั่วคราว (ดู scripts/import-members.ps1 / .sh) ไม่ต้องติดตั้งอะไรในเครื่อง
และไม่ต้องเพิ่มไลบรารีให้ image ของ backend

อ่านทั้งไฟล์ที่วางไว้ตรง ๆ และที่อยู่ใน .zip แล้วเขียนผลไปที่ data/import/converted/<ชื่อเดิม>.jpg
ไฟล์ที่เคยแปลงแล้วจะข้าม ตัวนำเข้าจะเลือกไฟล์ที่เปิดได้เองเมื่อมีทั้ง .heic และ .jpg ชื่อเดียวกัน
"""

import io
import sys
import zipfile
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

IMPORT_DIR = Path("/data/import")
OUT_DIR = IMPORT_DIR / "converted"
HEIC_SUFFIXES = (".heic", ".heif")


def convert(name: str, data: bytes) -> str:
    target = OUT_DIR / (Path(name).stem + ".jpg")
    if target.exists():
        return "skip"
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        OUT_DIR.mkdir(exist_ok=True)
        image.save(target, "JPEG", quality=95)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        print(f"  แปลง {name} ไม่สำเร็จ: {exc}")
        return "fail"


def main() -> int:
    counts = {"ok": 0, "skip": 0, "fail": 0}
    for path in sorted(IMPORT_DIR.rglob("*")):
        if path.is_dir() or OUT_DIR in path.parents:
            continue
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.filename.lower().endswith(HEIC_SUFFIXES):
                        counts[convert(info.filename, zf.read(info))] += 1
        elif path.suffix.lower() in HEIC_SUFFIXES:
            counts[convert(path.name, path.read_bytes())] += 1

    print(f"แปลง HEIC -> JPG: ใหม่ {counts['ok']} / เคยแปลงแล้ว {counts['skip']} / ไม่สำเร็จ {counts['fail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
