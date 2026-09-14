# =============================================================================
# Backend image - FastAPI + InsightFace (onnxruntime)
#
# ใช้ python:3.11-slim-bookworm ซึ่งมี image ทั้ง amd64 (dev บน Windows)
# และ arm64 (Raspberry Pi 5) จึงใช้ Dockerfile ไฟล์เดียวกันได้ทั้งสองฝั่ง
# =============================================================================
FROM python:3.11-slim-bookworm

# ---------------------------------------------------------------------------
# เลือกชุดโมเดลตอน build
#   buffalo_l = ความแม่นสูง ใช้ตอน dev บนเครื่องแรง
#   buffalo_s = เล็กและเร็วกว่า ใช้บน Raspberry Pi 5
#
# สร้าง image สำหรับ Pi:  docker build --build-arg FACE_MODEL_PACK=buffalo_s .
# และต้องตั้ง FACE_MODEL_PACK ใน .env ให้ตรงกันด้วย ไม่งั้น backend จะฟ้องตอนสตาร์ท
# ---------------------------------------------------------------------------
ARG FACE_MODEL_PACK=buffalo_l

# PYTHONDONTWRITEBYTECODE : ไม่ต้องสร้างไฟล์ .pyc รกใน volume ที่ mount มาจาก host
# PYTHONUNBUFFERED        : ให้ log ออกทันที ไม่ค้างใน buffer (สำคัญตอนดู docker logs)
# FACE_MODELS_DIR         : ที่เก็บโมเดลในตัว image (โหลดมาแล้วตอน build)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Asia/Bangkok \
    FACE_MODELS_DIR=/models/models

WORKDIR /app

# curl/unzip  : ใช้ดาวน์โหลดและแตกไฟล์โมเดลตอน build
# libgl1      : opencv-python ต้องการ (insightface บังคับใช้ตัวที่ไม่ใช่ headless)
# libglib2.0-0: dependency ของ opencv เช่นกัน
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        tzdata \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# คัดลอก requirements ก่อนโค้ด เพื่อให้ Docker cache ชั้นติดตั้ง library ไว้
# แก้โค้ดแล้ว build ใหม่จะไม่ต้องลง library ซ้ำ (insightface ลงนานมาก)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# ดาวน์โหลดโมเดลตั้งแต่ตอน build image
#
# เหตุผล: ข้อกำหนดของโปรเจกต์คือ "ห้ามดาวน์โหลดโมเดลตอน runtime"
# ถ้าปล่อยให้ insightface ไปโหลดเองตอนสตาร์ท จะเกิดปัญหา
#   - สตาร์ทครั้งแรกช้ามากโดยไม่มีใครรู้ว่าค้างอยู่ตรงไหน
#   - ถ้าเน็ตล่มหรือ Pi ไม่ได้ต่อเน็ต ระบบจะพังโดยไม่มีสาเหตุที่ชัดเจน
#   - ได้เวอร์ชันโมเดลไม่ตรงกันระหว่างเครื่อง dev กับเครื่องจริง
#
# ปลายทางคือ /models/models/<ชื่อชุด>/*.onnx ตามรูปแบบที่ insightface ใช้
#
# หมายเหตุในสคริปต์ด้านล่าง (เขียนไว้ตรงนี้เพราะห้ามใส่ # คั่นกลางบรรทัดที่ต่อด้วย \):
#   - บาง zip ห่อไฟล์ไว้ในโฟลเดอร์ย่อยอีกชั้น จึงต้องย้ายขึ้นมาชั้นบนสุด
#     เพราะ insightface มองหาไฟล์ .onnx ที่ระดับบนสุดของโฟลเดอร์ชุดโมเดลเท่านั้น
#   - บรรทัด test ท้ายสุดคือการยืนยันว่าได้ไฟล์ .onnx มาจริง ถ้าไม่ได้ให้ build
#     ล้มเหลวตรงนี้เลย ดีกว่าปล่อย image ผ่านแล้วไปพังตอนรันจริง
# ---------------------------------------------------------------------------
RUN set -eux; \
    dest="/models/models/${FACE_MODEL_PACK}"; \
    mkdir -p "$dest"; \
    curl -fsSL -o /tmp/pack.zip \
      "https://github.com/deepinsight/insightface/releases/download/model-zoo/${FACE_MODEL_PACK}.zip"; \
    unzip -q /tmp/pack.zip -d "$dest"; \
    rm /tmp/pack.zip; \
    if [ -z "$(find "$dest" -maxdepth 1 -name '*.onnx' -print -quit)" ]; then \
        inner="$(find "$dest" -mindepth 1 -maxdepth 1 -type d | head -1)"; \
        if [ -n "$inner" ]; then \
            mv "$inner"/* "$dest"/; \
            rmdir "$inner"; \
        fi; \
    fi; \
    ls -lh "$dest"; \
    test -n "$(find "$dest" -maxdepth 1 -name '*.onnx' -print -quit)"

# คัดลอกโค้ดเข้า image (ตอน dev จะถูก volume จาก host ทับอีกที)
COPY app ./app

EXPOSE 8000

# คำสั่งเริ่มต้นสำหรับ production (ตอน dev docker-compose.yml จะ override ให้ใส่ --reload)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
