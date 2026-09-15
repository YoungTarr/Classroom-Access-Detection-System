# ระบบตรวจจับคนเข้าใช้ห้องของหลักสูตรวิศวกรรมคอมพิวเตอร์

**Classroom Access Detection System (CADS)**

ระบบตรวจจับและจดจำใบหน้าของผู้ที่เดินเข้า-ออกห้องเรียน ผ่านกล้อง IP
โดยใช้ InsightFace + FAISS ประมวลผลบนเครื่องปลายทางเอง (on-premise ไม่ส่งข้อมูลออกนอกองค์กร)

---

## สถาปัตยกรรม

```
                      เครื่องผู้ดูแล (เบราว์เซอร์)
                                 |
                                 |  http://<ip>:3000
                                 v
    +----------------------------------------------------------+
    |  frontend : nginx  (static + reverse proxy)               |
    |             /api/ , /ws/  ->  backend:8000                |
    +----------------------------------------------------------+
                                 |
                                 v
    +----------------------------------------------------------+
    |  backend  : FastAPI + InsightFace (onnxruntime) + FAISS   |
    +----------------------------------------------------------+
                                 |
                                 v
    +----------------------------------------------------------+
    |  db       : PostgreSQL 16     |  adminer : จัดการ DB       |
    +----------------------------------------------------------+
```

ทุก service รันเป็น Docker container แยกกัน ผ่าน `docker compose`

### เครื่องปลายทาง

| รายการ | ค่า |
|---|---|
| ฮาร์ดแวร์ | Raspberry Pi 5 (RAM 8GB) |
| ระบบปฏิบัติการ | Ubuntu 24.04 LTS (ARM64) |
| โหมดการใช้งาน | headless — ไม่มีจอ ดูผ่านเบราว์เซอร์จากเครื่องอื่นในวง LAN |
| กล้อง | TP-Link Tapo C200 (RTSP, Wi-Fi 2.4GHz) |

> ระหว่างพัฒนาใช้ Windows + Docker Desktop ก่อน
> ไลบรารีทุกตัวจึงถูกเลือกให้มี wheel สำหรับ **linux/arm64 (aarch64)** ด้วย
> เพื่อให้ย้ายขึ้น Pi ได้โดยไม่ต้อง build จาก source

---

## เทคโนโลยีที่ใช้

| ส่วน | เทคโนโลยี |
|---|---|
| Backend | Python 3.11 + FastAPI + uvicorn |
| Frontend | HTML / CSS / JavaScript ธรรมดา (ไม่มี build step) เสิร์ฟด้วย nginx |
| ฐานข้อมูล | PostgreSQL 16 |
| ตรวจจับ/จดจำใบหน้า | InsightFace (SCRFD + ArcFace) บน onnxruntime |
| ค้นหาเวกเตอร์ | FAISS |
| การติดตั้ง | Docker + Docker Compose |

---

## เริ่มต้นใช้งาน

### 1. เตรียมไฟล์ `.env`

```bash
cp .env.example .env
```

Windows (PowerShell):

```powershell
Copy-Item .env.example .env
```

จากนั้นแก้ `DB_PASSWORD` ให้เป็นรหัสผ่านของคุณเอง

> ถ้าลืมสร้าง `.env` แล้วสั่ง `docker compose up` ระบบจะหยุดทันทีพร้อมข้อความ
> บอกว่าขาดตัวแปรอะไร (ตั้งใจให้ฟ้องตั้งแต่ต้น ไม่ปล่อยให้พังทีหลัง)

### 2. สั่งรัน

```bash
docker compose up -d --build
```

### 3. เปิดใช้งาน

| หน้า | ที่อยู่ | คำอธิบาย |
|---|---|---|
| หน้าเว็บหลัก | http://localhost:3000 | สถานะระบบ + รายชื่อสมาชิก |
| Adminer | http://localhost:8080 | จัดการฐานข้อมูลผ่านเว็บ |
| API | http://localhost:8000/docs | เอกสาร API อัตโนมัติของ FastAPI |

**ข้อมูลเข้า Adminer**

| ช่อง | ค่า |
|---|---|
| System | PostgreSQL |
| Server | `db` |
| Username / Password / Database | ตามที่ตั้งไว้ใน `.env` |

> เข้าหน้าเว็บด้วย `http://localhost:3000` เท่านั้น (อย่าใช้ `127.0.0.1` หรือ IP อื่น)
> เพราะตั้งแต่เฟส 2 จะต้องขอสิทธิ์ใช้เว็บแคม ซึ่งเบราว์เซอร์อนุญาตเฉพาะ
> `localhost` หรือเว็บที่เป็น HTTPS เท่านั้น

---

## คำสั่งที่ใช้บ่อย

ดูสถานะ container ทั้งหมด:

```bash
docker compose ps
```

ดู log ของ backend แบบต่อเนื่อง:

```bash
docker compose logs -f backend
```

รีสตาร์ทเฉพาะ backend:

```bash
docker compose restart backend
```

หยุดทั้งระบบ (ข้อมูลใน DB ยังอยู่):

```bash
docker compose down
```

ล้างทุกอย่างรวมถึงข้อมูลใน DB (ใช้เมื่อแก้ `db/schema.sql` แล้วอยากให้รันใหม่):

```bash
docker compose down -v
```

---

## โครงสร้างโปรเจกต์

```
backend/
  Dockerfile, requirements.txt, .dockerignore
  app/
    main.py                  FastAPI app, routes, WebSocket, startup
    config.py                ค่า config ทุกตัวรวมไว้ที่เดียว อ่านจาก environment
    db/database.py           ต่อ PostgreSQL, query members
    face/detector.py         โหลด InsightFace (detection + recognition)     [เฟส 2]
    face/recognizer.py       สกัด embedding + normalize                     [เฟส 4]
    vector_index/faiss_index.py                                             [เฟส 4]
    identify/base.py         interface Identifier + IdentityResult          [เฟส 4]
    identify/__init__.py     create_identifier() จุดสลับ implementation      [เฟส 4]
    identify/face_identifier.py                                             [เฟส 4]
    identify/members.py      MemberDirectory ดึงชื่อจากตาราง members         [เฟส 4]
    core/frame_source.py     FrameSource / BrowserFrameSource / RTSP        [เฟส 2, 6]
    core/pipeline.py         detect -> identify -> track -> direction       [เฟส 3]
    core/tracker.py          tracking + โหวตผล + คำนวณทิศทาง                 [เฟส 3, 5]
frontend/
  Dockerfile, nginx.conf
  index.html, css/style.css, js/app.js, js/camera.js
db/
  schema.sql               โครงสร้างตาราง (รันอัตโนมัติตอนสร้าง DB ครั้งแรก)
  seed.sql                 ข้อมูลตัวอย่าง
data/faces/                รูปใบหน้าจริง (ไม่ขึ้น git — เป็นข้อมูลส่วนบุคคล)
docker-compose.yml, .env.example, .gitignore, README.md
```

### ที่เก็บรูปใบหน้า

ฐานข้อมูลเก็บเพียง **path** ของรูป ไฟล์จริงวางไว้ที่:

```
data/faces/<รหัสนักศึกษา>/left.jpg
data/faces/<รหัสนักศึกษา>/front.jpg
data/faces/<รหัสนักศึกษา>/right.jpg
```

เก็บ 3 มุมต่อคนเพื่อให้จดจำได้แม้หันหน้าไม่ตรงกล้อง
โฟลเดอร์นี้ถูก mount เข้า container ของ backend ที่ `/data/faces` แบบอ่านอย่างเดียว

**หลังวางรูปเสร็จ** กดปุ่ม *"โหลดรูปใบหน้าใหม่"* บนหน้าเว็บ (หรือเรียก `GET /api/faces/reload`)
ระบบจะสร้างคลังเวกเตอร์ใหม่ทันทีโดยไม่ต้องรีสตาร์ท container
และรายงานกลับมาว่าได้กี่เวกเตอร์ ไฟล์ไหนมีปัญหาอะไรบ้าง

### ข้อแนะนำในการถ่ายรูปลงทะเบียน

| ควร | ไม่ควร |
|---|---|
| แสงสม่ำเสมอ หน้าชัด | ย้อนแสง / หน้ามืด |
| มีคนเดียวในรูป | มีหลายคน (ระบบจะเลือกหน้าที่ใหญ่ที่สุดแล้วเตือน) |
| หันซ้าย-ตรง-ขวา ราว 30-45° | หันข้างจนเห็นตาข้างเดียว |
| ใบหน้ากินพื้นที่รูปพอสมควร | ถ่ายไกลจนหน้าเล็กมาก |
| ถอดแว่นกันแดด / หมวก | ใส่ของบังใบหน้า |

---

## แผนการพัฒนาเป็นเฟส

| เฟส | เนื้อหา | สถานะ |
|---|---|---|
| 1 | ฐานข้อมูล + โครงสร้าง Docker + หน้าแสดงรายชื่อสมาชิก | ✅ เสร็จแล้ว |
| 2 | ตรวจจับใบหน้าจากเว็บแคมผ่าน WebSocket | ✅ เสร็จแล้ว |
| 3 | กรอบขยับตามลื่น + tracking ข้ามเฟรม | ✅ เสร็จแล้ว |
| 4 | จดจำว่าเป็นใคร (FAISS + ชื่อจากฐานข้อมูล) | ✅ เสร็จแล้ว |
| 5 | ทิศทางการเคลื่อนที่ (ซ้าย / ขวา / อยู่กับที่) | ✅ เสร็จแล้ว |
| 6 | รับภาพจากกล้อง IP จริง (RTSP) | ⬜ |
| 7 | แยกอัตรา fps ให้ภาพลื่นขึ้น | ⬜ |

---

## ข้อตกลงในการเขียนโค้ดของโปรเจกต์นี้

1. **คอมเมนต์เป็นภาษาไทยทุกไฟล์** — ระบบนี้ต้องส่งต่อให้รุ่นน้องดูแลได้
2. **ค่า config อยู่ที่เดียว** คือ `backend/app/config.py` อ่านจาก environment เท่านั้น
   ห้าม hardcode กระจัดกระจาย และทุกค่าที่เพิ่มต้องไปเพิ่มใน `.env.example` ด้วย
3. **ห้าม fallback เงียบ ๆ** — อะไรพังต้องรายงานสาเหตุให้เห็นชัดเจน
   ไม่ใช่เดาค่าแทนแล้วทำงานต่อจนไปพังที่อื่นโดยหาต้นตอไม่เจอ
4. **ชื่อ-นามสกุลต้องมาจากฐานข้อมูลเสมอ** ห้าม hardcode ในโค้ด
5. **ไลบรารีต้องมี wheel สำหรับ ARM64** ถ้าตัวไหนไม่มี ให้หยุดแล้วรายงาน อย่าเดา

---

## หมายเหตุสำคัญเรื่องเว็บแคม (เฟส 2)

**ต้องเปิดหน้าเว็บด้วย `http://localhost:3000` เท่านั้น**

เบราว์เซอร์อนุญาตให้ใช้กล้อง (`getUserMedia`) เฉพาะหน้าเว็บที่เป็น *secure context*
ซึ่งมีแค่ 2 กรณี คือ `http://localhost` หรือเว็บที่เป็น **HTTPS**

| เปิดด้วย | ใช้กล้องได้ไหม |
|---|---|
| `http://localhost:3000` | ✅ ได้ |
| `http://127.0.0.1:3000` | ❌ ไม่ได้ (ถือเป็นคนละอย่างกับ localhost) |
| `http://192.168.1.x:3000` | ❌ ไม่ได้ |

> ข้อจำกัดนี้มีผลเฉพาะ **เว็บแคม** เท่านั้น
> เมื่อขึ้นเฟส 6 ที่รับภาพจากกล้อง IP โดยตรง จะเปิดดูจากเครื่องไหนในวง LAN ก็ได้
> เพราะภาพถูกดึงที่ฝั่ง backend ไม่ได้ใช้กล้องของเครื่องผู้ใช้

### ถ้าเปิดกล้องไม่ได้

| ข้อความที่ขึ้น | สาเหตุและวิธีแก้ |
|---|---|
| ไม่อนุญาตให้ใช้กล้อง | กดไอคอนกล้องบนแถบที่อยู่เว็บ เลือกอนุญาต แล้วโหลดหน้าใหม่ |
| มีโปรแกรมอื่นใช้อยู่ | ปิด Zoom / Teams / Discord / OBS หรือแท็บอื่นที่เปิดกล้องค้าง (เจอบ่อยมากบน Windows) |
| ไม่พบกล้อง | ตรวจว่าเสียบกล้องแล้ว และไม่ได้ถูกปิดไว้ใน Device Manager |

---

## กล้อง IP (เฟส 6)

ตั้ง `FRAME_SOURCE=rtsp` ในไฟล์ `.env` เพื่อสลับจากเว็บแคมไปใช้กล้อง IP
หน้าเว็บจะเปลี่ยนโหมดให้อัตโนมัติ ไม่ต้องแก้อะไรเพิ่ม

> โหมดนี้ **ไม่ใช้กล้องของเครื่องผู้ใช้เลย** backend เป็นคนต่อกล้องเอง
> จึงเปิดดูจากเครื่องไหนในวง LAN ก็ได้ ไม่ติดข้อจำกัด `localhost`/HTTPS แบบเว็บแคม

### เพิ่มกล้องตัวที่สอง — แก้แค่ `.env` ไม่ต้องแตะโค้ด

```bash
CAMERA_IDS=door_in,door_out

CAMERA_DOOR_OUT_NAME=ประตูทางออก
CAMERA_DOOR_OUT_HOST=192.168.1.103
CAMERA_DOOR_OUT_DIRECTION=OUT
CAMERA_DOOR_OUT_ENABLED=true
```

บัญชีผู้ใช้/พอร์ต/path ที่ใช้เหมือนกันทุกตัว ตั้งครั้งเดียวที่ `CAMERA_DEFAULT_*`
กล้องตัวไหนต่างจากค่ากลาง ค่อยกำหนดทับเฉพาะตัวนั้น (เช่น `CAMERA_DOOR_OUT_USER=...`)

### บัญชีกล้อง Tapo

ต้องใช้ **Camera Account** ไม่ใช่บัญชี TP-Link ID ที่ล็อกอินแอป

> แอป Tapo → เลือกอุปกรณ์ → ⚙️ Settings → Advanced Settings → **Camera Account**

### เรื่องที่จัดการไว้แล้วในโค้ด

| ปัญหา | วิธีจัดการ |
|---|---|
| ภาพแตกเป็นบล็อกบน Wi-Fi 2.4GHz | บังคับใช้ TCP ผ่าน `OPENCV_FFMPEG_CAPTURE_OPTIONS` **ก่อน**สร้าง `VideoCapture` |
| ภาพช้ากว่าจริงและถ่างขึ้นเรื่อย ๆ | thread อ่านทิ้งตลอดเวลา เก็บแค่เฟรมล่าสุด (`CAP_PROP_BUFFERSIZE` ใช้ไม่ได้กับ FFMPEG) |
| RTSP ค้างแบบไม่มี error | watchdog วัดจาก "เวลาตั้งแต่ได้เฟรมดีล่าสุด" ไม่ได้เช็คแค่ `read()` คืน `False` |
| กล้องถูกถอดปลั๊ก | ต่อใหม่อัตโนมัติแบบ exponential backoff (1, 2, 4, 8 … เพดาน 30 วินาที) |
| รหัสผ่านหลุดใน log | `safe_url()` ปิดบังเป็น `rtsp://***:***@ip/path` เสมอ |
| รหัสผ่านมีอักขระพิเศษ | URL-encode อัตโนมัติ (`@` → `%40`) |

---

## หมายเหตุสำคัญเรื่องการเชื่อมต่อฐานข้อมูล

ไฟล์ `.env` กำหนด `DB_HOST=localhost` ซึ่ง **ใช้ได้เฉพาะเมื่อเชื่อมต่อจากเครื่อง host**
เช่นเปิดด้วย DBeaver หรือ psql บน Windows

แต่เมื่อ backend รันอยู่ใน container ค่านี้จะผิด เพราะ `localhost` ของ backend
หมายถึง "ตัว container backend เอง" ไม่ใช่เครื่อง host

`docker-compose.yml` จึง override ค่าให้อัตโนมัติเป็น:

| ตัวแปร | ค่าในเครื่อง host | ค่าภายใน docker network |
|---|---|---|
| `DB_HOST` | `localhost` | `db` (ชื่อ service) |
| `DB_PORT` | ตามที่ map ออกมา | `5432` เสมอ |
