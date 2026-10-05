#!/usr/bin/env bash
# นำเข้าสมาชิกจาก Google Form (ไฟล์ zip ของโฟลเดอร์ CADS-export ใน data/import/)
#
#   ./scripts/import-members.sh           โหมดทดลอง แสดงแผน ไม่บันทึก
#   ./scripts/import-members.sh --apply   บันทึกจริง
#
# ตัวเลือกอื่น: --id-from-email, --allow-rename  (ดูรายละเอียดใน backend/app/tools/import_members.py)
set -euo pipefail
cd "$(dirname "$0")/.."
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \
  -v "./data/import:/data/import:ro" \
  -v "./data/faces:/data/faces-rw" \
  backend python -m app.tools.import_members "$@"
