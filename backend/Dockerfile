# =============================================================================
# Backend image - FastAPI + InsightFace (onnxruntime) + FAISS
#
# ใช้ python:3.11-slim-bookworm ซึ่งมี image ทั้ง amd64 (dev บน Windows)
# และ arm64 (Raspberry Pi 5) จึงใช้ Dockerfile ไฟล์เดียวกันได้ทั้งสองฝั่ง
# =============================================================================
FROM python:3.11-slim-bookworm

# ---------------------------------------------------------------------------
# เลือกชุดโมเดลตอน build - แยกชุด "ตรวจจับ" กับ "จดจำ" ออกจากกัน
#
# ทำไมต้องแยก: วัดจริงบนเครื่อง dev แล้วพบว่า
#   ตรวจจับ  det_10g  (buffalo_l) ~ 90-190 ms ต่อเฟรม  -> เอาตัวแม่นไว้ก่อน
#   จดจำ     w600k_r50 (buffalo_l) ~ 167 ms ต่อ "หนึ่งหน้า" -> หนักเกินไปมาก
#            w600k_mbf (buffalo_s) ~  12 ms ต่อหนึ่งหน้า    -> เร็วกว่า 14 เท่า
# และทดสอบแล้วว่า mbf แยกคนต่างกันได้ดีพอ ๆ กับ r50
# (ความคล้ายระหว่างคนต่างกัน สูงสุด 0.17 เทียบกับ 0.22 ของ r50 ซึ่งต่ำกว่าเกณฑ์มาก)
#
# ถ้า Pi ยังช้าอยู่ ให้เปลี่ยนชุดตรวจจับเป็น buffalo_s ด้วย:
#   docker compose build --build-arg FACE_MODEL_PACK=buffalo_s backend
# ---------------------------------------------------------------------------
ARG FACE_MODEL_PACK=buffalo_l
ARG FACE_REC_PACK=buffalo_s

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
# หมายเหตุในสคริปต์ด้านล่าง (เขียนไว้ตรงนี้เพราะห้ามใส่ # คั่นกลางบรรทัดที่ต่อด้วย \):
#   - บาง zip ห่อไฟล์ไว้ในโฟลเดอร์ย่อยอีกชั้น จึงต้องย้ายขึ้นมาชั้นบนสุด
#     เพราะ insightface มองหาไฟล์ .onnx ที่ระดับบนสุดของโฟลเดอร์ชุดโมเดลเท่านั้น
#   - หลังแตกไฟล์แล้วจะลบ .onnx ที่ไม่ได้ใช้ทิ้ง (1k3d68 = 3D landmark 137MB,
#     genderage, 2d106det และ w600k_r50 ที่ช้าเกินไป) ลด image ได้หลายร้อย MB
#     ซึ่งสำคัญมากตอนย้ายขึ้น Raspberry Pi
#   - ถ้าตั้งสองชุดเป็นชื่อเดียวกัน โฟลเดอร์จะเป็นอันเดียวกัน จึงต้องเก็บทั้งสองแบบไว้
# ---------------------------------------------------------------------------
RUN set -eux; \
    fetch_pack() { \
        pack="$1"; \
        dest="/models/models/$pack"; \
        if [ -d "$dest" ]; then return 0; fi; \
        mkdir -p "$dest"; \
        curl -fsSL -o /tmp/pack.zip \
          "https://github.com/deepinsight/insightface/releases/download/model-zoo/${pack}.zip"; \
        unzip -q /tmp/pack.zip -d "$dest"; \
        rm /tmp/pack.zip; \
        if [ -z "$(find "$dest" -maxdepth 1 -name '*.onnx' -print -quit)" ]; then \
            inner="$(find "$dest" -mindepth 1 -maxdepth 1 -type d | head -1)"; \
            if [ -n "$inner" ]; then \
                mv "$inner"/* "$dest"/; \
                rmdir "$inner"; \
            fi; \
        fi; \
    }; \
    fetch_pack "${FACE_MODEL_PACK}"; \
    fetch_pack "${FACE_REC_PACK}"; \
    for f in /models/models/"${FACE_MODEL_PACK}"/*.onnx; do \
        base="$(basename "$f")"; \
        keep=0; \
        case "$base" in det_*) keep=1 ;; esac; \
        if [ "${FACE_MODEL_PACK}" = "${FACE_REC_PACK}" ]; then \
            case "$base" in w600k_*) keep=1 ;; esac; \
        fi; \
        if [ "$keep" = "0" ]; then rm -f "$f"; fi; \
    done; \
    for f in /models/models/"${FACE_REC_PACK}"/*.onnx; do \
        base="$(basename "$f")"; \
        keep=0; \
        case "$base" in w600k_*) keep=1 ;; esac; \
        if [ "${FACE_MODEL_PACK}" = "${FACE_REC_PACK}" ]; then \
            case "$base" in det_*) keep=1 ;; esac; \
        fi; \
        if [ "$keep" = "0" ]; then rm -f "$f"; fi; \
    done; \
    echo "ไฟล์โมเดลที่เหลืออยู่:"; \
    ls -lhR /models/models; \
    test -n "$(find "/models/models/${FACE_MODEL_PACK}" -maxdepth 1 -name 'det_*.onnx' -print -quit)"; \
    test -n "$(find "/models/models/${FACE_REC_PACK}" -maxdepth 1 -name 'w600k_*.onnx' -print -quit)"

# คัดลอกโค้ดเข้า image (ตอน dev จะถูก volume จาก host ทับอีกที)
COPY app ./app

EXPOSE 8000

# คำสั่งเริ่มต้นสำหรับ production (ตอน dev docker-compose.yml จะ override ให้ใส่ --reload)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
