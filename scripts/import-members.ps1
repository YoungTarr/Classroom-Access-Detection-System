# นำเข้าสมาชิกจาก Google Form (ไฟล์ zip ของโฟลเดอร์ CADS-export ใน data/import/)
#
#   .\scripts\import-members.ps1           โหมดทดลอง แสดงแผน ไม่บันทึก
#   .\scripts\import-members.ps1 --apply   บันทึกจริง
#
# ตัวเลือกอื่น: --id-from-email, --allow-rename  (ดูรายละเอียดใน backend/app/tools/import_members.py)
Set-Location (Split-Path -Parent $PSScriptRoot)

# 1) แปลงรูป HEIC (iPhone) เป็น JPG ก่อน เพราะ backend เปิด HEIC ไม่ได้ (ใช้ container python ชั่วคราว)
docker run --rm `
  -v "${PWD}/data/import:/data/import" `
  -v "${PWD}/scripts/heic_to_jpg.py:/heic_to_jpg.py:ro" `
  python:3.11-slim sh -c "pip install -q --disable-pip-version-check --root-user-action=ignore pillow pillow-heif && python /heic_to_jpg.py"

# 2) นำเข้า
docker compose run --rm --no-deps `
  -v "./data/import:/data/import:ro" `
  -v "./data/faces:/data/faces-rw" `
  backend python -m app.tools.import_members @args
