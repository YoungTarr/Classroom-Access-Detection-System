# =============================================================================
# Backend image - FastAPI + InsightFace (เตรียมไว้สำหรับเฟสถัดไป)
#
# ใช้ python:3.11-slim-bookworm ซึ่งมี image ทั้ง amd64 (dev บน Windows)
# และ arm64 (Raspberry Pi 5) จึงใช้ Dockerfile ไฟล์เดียวกันได้ทั้งสองฝั่ง
# =============================================================================
FROM python:3.11-slim-bookworm

# PYTHONDONTWRITEBYTECODE : ไม่ต้องสร้างไฟล์ .pyc รกใน volume ที่ mount มาจาก host
# PYTHONUNBUFFERED        : ให้ log ออกทันที ไม่ค้างใน buffer (สำคัญมากตอนดู docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Asia/Bangkok

WORKDIR /app

# curl ใช้สำหรับ healthcheck / ทดสอบ endpoint จากในตัว container เอง
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# คัดลอก requirements ก่อนโค้ด เพื่อให้ Docker cache ชั้นติดตั้ง library ไว้
# แก้โค้ดแล้ว build ใหม่จะไม่ต้องลง library ซ้ำ
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# คัดลอกโค้ดเข้า image (ตอน dev จะถูก volume จาก host ทับอีกที)
COPY app ./app

EXPOSE 8000

# คำสั่งเริ่มต้นสำหรับ production (ตอน dev docker-compose.yml จะ override ให้ใส่ --reload)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
