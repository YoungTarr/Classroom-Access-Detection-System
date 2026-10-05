# นำเข้าสมาชิกจาก Google Form (ไฟล์ zip ของโฟลเดอร์ CADS-export ใน data/import/)
#
#   .\scripts\import-members.ps1           โหมดทดลอง แสดงแผน ไม่บันทึก
#   .\scripts\import-members.ps1 --apply   บันทึกจริง
#
# ตัวเลือกอื่น: --id-from-email, --allow-rename  (ดูรายละเอียดใน backend/app/tools/import_members.py)
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose run --rm --no-deps `
  -v "./data/import:/data/import:ro" `
  -v "./data/faces:/data/faces-rw" `
  backend python -m app.tools.import_members @args
