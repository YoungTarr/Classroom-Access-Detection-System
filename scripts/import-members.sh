#!/usr/bin/env bash
# นำเข้าสมาชิกจาก Google Form (ไฟล์ zip ของโฟลเดอร์ CADS-export ใน data/import/)
#
#   ./scripts/import-members.sh           โหมดทดลอง แสดงแผน ไม่บันทึก
#   ./scripts/import-members.sh --apply   บันทึกจริง
#
# ตัวเลือกอื่น: --id-from-email, --allow-rename  (ดูรายละเอียดใน backend/app/tools/import_members.py)
set -euo pipefail
cd "$(dirname "$0")/.."

# 1) แปลงรูป HEIC (iPhone) เป็น JPG ก่อน เพราะ backend เปิด HEIC ไม่ได้ (ใช้ container python ชั่วคราว)
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(pwd)/data/import:/data/import" \
  -v "$(pwd)/scripts/heic_to_jpg.py:/heic_to_jpg.py:ro" \
  python:3.11-slim sh -c "pip install -q --disable-pip-version-check --root-user-action=ignore pillow pillow-heif && python /heic_to_jpg.py"

# 2) นำเข้า
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \
  -v "./data/import:/data/import:ro" \
  -v "./data/faces:/data/faces-rw" \
  backend python -m app.tools.import_members "$@"
