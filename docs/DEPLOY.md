# ย้ายระบบจากโน้ตบุ๊กขึ้น Raspberry Pi 5

เอกสารนี้อธิบายขั้นตอนนำระบบที่พัฒนาบนโน้ตบุ๊ก (Windows + Docker Desktop)
ไปรันจริงบน Raspberry Pi 5 (ARM64) ตั้งแต่เตรียมไฟล์จนถึงตรวจรับ

> **หลักที่ต้องจำ:** โค้ดย้ายด้วย `git` แต่ **ค่าตั้งค่าและข้อมูลจริงไม่ได้อยู่ใน git**
> ต้องเตรียมบน Pi เองทุกครั้ง ระบบที่ขึ้นมาได้โดยไม่มี error ไม่ได้แปลว่าตั้งค่าถูก
> ให้ทำ checklist ตรวจรับท้ายเอกสารให้ครบเสมอ

---

## 1. สิ่งที่ต้องมีบน Pi ก่อนเริ่ม

- Ubuntu 24.04 LTS **ARM64** (หรือ OS 64-bit ตัวอื่น — ต้องเป็น aarch64 เพราะ wheel ของ onnxruntime / faiss ที่เลือกไว้เป็นแบบ 64-bit)
- Docker Engine + Docker Compose plugin (`docker compose version` ต้องใช้ได้)
- `git`
- ต่อเน็ตได้ **ตอน build** (ต้องดาวน์โหลด base image, library และโมเดล)
- อยู่ในวง LAN เดียวกับกล้อง Tapo ทั้งสองตัว

---

## 2. ไฟล์ที่ไม่ได้อยู่ใน git — ต้องเตรียมใหม่บน Pi

ไฟล์เหล่านี้ถูกกันไว้ใน `.gitignore` โดยตั้งใจ `git pull` จึงไม่ได้มาด้วย

| ไฟล์ / โฟลเดอร์ | ทำไมไม่อยู่ใน git | วิธีเตรียมบน Pi |
|---|---|---|
| `.env` | มีรหัสผ่านฐานข้อมูลและรหัสผ่านกล้อง | `cp .env.example .env` แล้วแก้ตามหัวข้อ 3 (**อย่า**คัดลอก `.env` ของโน้ตบุ๊กไปทั้งไฟล์ มีค่าหลายตัวที่ต้องต่างกัน) |
| **ข้อมูลในฐานข้อมูล** (สมาชิก + รูปใบหน้า + ประวัติ) | ข้อมูลส่วนบุคคล และอยู่ใน Docker volume ไม่ใช่ไฟล์ในโปรเจกต์ | ย้ายด้วย `pg_dump` / `pg_restore` ตามหัวข้อ 2.1 |
| `models/` | ไฟล์โมเดลใหญ่หลายร้อย MB | **ปกติไม่ต้องทำอะไร** Dockerfile ดาวน์โหลดโมเดลฝังไว้ใน image ตอน `--build` ให้เอง (ตามชื่อชุดใน `FACE_MODEL_PACK` / `FACE_REC_PACK`) — ขอแค่ให้ Pi ต่อเน็ตได้ตอน build ถ้าต้อง build แบบไม่มีเน็ต ต้องเตรียมไฟล์โมเดลเองให้ครบก่อน |

> **ไม่ต้องคัดลอก `data/faces/` แล้ว** รูปใบหน้าสมาชิกทั้งหมดเก็บในฐานข้อมูล (ตาราง `member_photos`)
> ย้ายฐานข้อมูลอย่างเดียวได้ครบทั้งรายชื่อและรูป

### 2.1 ย้ายฐานข้อมูลจากโน้ตบุ๊กไป Pi

ถ้าไม่ย้าย ฐานข้อมูลบน Pi จะเริ่มจาก `db/schema.sql` + `db/seed.sql` (สมาชิกตัวอย่าง 2 คน ไม่มีรูป)
ต้องนำเข้าสมาชิกใหม่บน Pi เอง (ดู README หัวข้อ "สมาชิกและรูปใบหน้า")

**ขั้นที่ 1 — บนโน้ตบุ๊ก (PowerShell)** export ทั้งฐานข้อมูลเป็นไฟล์เดียว:

```powershell
docker exec cads-postgres pg_dump -U cads -d cads -Fc -f /tmp/cads.dump
docker cp cads-postgres:/tmp/cads.dump .\cads.dump
docker exec cads-postgres rm /tmp/cads.dump
```

(เปลี่ยน `-U cads -d cads` ให้ตรงกับ `DB_USER` / `DB_NAME` ใน `.env` ของโน้ตบุ๊ก ได้ไฟล์ ~25 MB)

ส่งไฟล์ไป Pi (เปลี่ยน `pi@192.168.1.50` เป็นของจริง):

```powershell
scp .\cads.dump pi@192.168.1.50:~/Classroom-Access-Detection-System/
```

**ขั้นที่ 2 — บน Pi** ทำ**หลัง**แก้ `.env` แล้ว แต่**ก่อน**รันระบบทั้งหมด (หัวข้อ 4):

```bash
cd ~/Classroom-Access-Detection-System
docker compose up -d db                      # เปิดเฉพาะฐานข้อมูลก่อน
docker compose ps db                         # รอจนขึ้น (healthy)
docker cp cads.dump cads-postgres:/tmp/cads.dump
docker exec cads-postgres pg_restore -U <DB_USER> -d <DB_NAME> --clean --if-exists --no-owner /tmp/cads.dump
docker exec cads-postgres rm /tmp/cads.dump
rm cads.dump                                 # เป็นข้อมูลส่วนบุคคล ไม่ต้องเก็บไว้
```

`<DB_USER>` / `<DB_NAME>` คือค่าใน `.env` **ของ Pi** (ตั้งต่างจากโน้ตบุ๊กได้ `--no-owner` จัดการเรื่องเจ้าของตารางให้)
`--clean` ลบสมาชิกตัวอย่างจาก seed.sql ทิ้งแล้วใส่ข้อมูลจากโน้ตบุ๊กแทน

ตรวจว่าข้อมูลมาครบ:

```bash
docker exec cads-postgres psql -U <DB_USER> -d <DB_NAME> -c "select (select count(*) from members) as members, (select count(*) from member_photos) as photos, (select count(*) from access_logs) as logs;"
```

> ประวัติเข้า-ออกที่เกิดจากการทดสอบบนโน้ตบุ๊กจะติดมาด้วย ลบได้ทีหลังด้วยปุ่ม "ลบประวัติทั้งหมด" บนหน้าเว็บ
>
> ทดสอบแล้ว: restore ลงฐานข้อมูลใหม่ที่มี schema + seed อยู่แล้ว และใช้ชื่อผู้ใช้ต่างจากต้นทาง ได้ข้อมูลครบทุกแถว

---

## 3. ค่าใน `.env` ที่ต้องต่างจากโน้ตบุ๊ก

| ตัวแปร | บนโน้ตบุ๊ก | บน Pi | เหตุผล |
|---|---|---|---|
| `FACE_MODEL_PACK` | `buffalo_l` | **`buffalo_s`** | โมเดลตรวจจับตัวเล็ก เร็วกว่ามากบน CPU ของ Pi ค่านี้ถูกใช้เป็น build arg ด้วย จึงต้องสั่ง `--build` ทุกครั้งที่เปลี่ยน |
| `DB_HOST` | `localhost` | **`db`** | บน Pi ไม่ได้ต่อฐานข้อมูลจากนอก Docker ตั้งเป็นชื่อ service ไว้ให้ตรงกับที่ container ใช้จริง (compose override ให้อยู่แล้ว แต่ตั้งให้ตรงกันจะไม่สับสนตอนอ่าน) |
| `DB_PASSWORD` | ค่าทดสอบ | รหัสผ่านใหม่ที่เดายาก | Pi เปิดพอร์ตอยู่ในวง LAN จริง |
| `FRAME_SOURCE` | `browser` หรือ `rtsp` | **`rtsp`** | Pi ไม่มีเว็บแคม ใช้กล้อง IP เท่านั้น |
| `CAMERA_1_HOST` / `CAMERA_2_HOST` | อาจเป็น IP เดียวกัน (ทดสอบกล้องตัวเดียว) | **IP จริงของแต่ละตัว ห้ามซ้ำกัน** | ถ้าซ้ำ ระบบจะขึ้นแถบเตือนสีเหลือง |
| `CAMERA_2_PATH` | อาจเป็น `/stream1` (ทดสอบ) | **`/stream2`** | สองกล้อง Wi-Fi 2.4GHz ใช้ `/stream1` พร้อมกันไม่ไหว |
| `FACE_NUM_THREADS` | `4` | `3` | Pi 5 มี 4 core เหลือไว้ 1 core ให้ถอดรหัสวิดีโอ |
| `CAPTURE_FPS` | `15` | `8`-`10` | ลดภาระถอดรหัส H.264 |
| `FACE_DET_SIZE` | `640` | `480` | ลดขนาดภาพที่ป้อนเข้า AI |
| `DETECT_FPS` | `5` | `3` | Pi ตรวจจับได้ช้ากว่า ชื่อจะขึ้นช้าลงเป็น ~1–2 วินาที |
| `DETECT_FPS_TOTAL` | `0` | `6` | เพดานรวมสองกล้อง (ดู README หัวข้อ "ค่าที่ควรปรับตอนรันบน Raspberry Pi 5") |

> IP กล้องควรผูกไว้ด้วย **DHCP reservation** ที่ router ก่อน deploy
> ไม่งั้นพอ router รีสตาร์ท IP กล้องอาจเปลี่ยน แล้วกล้องจะ "หลุด" ทั้งที่ไม่ได้เสีย

---

## 4. ขั้นตอน deploy

ครั้งแรก:

```bash
git clone <URL ของ repo> ~/Classroom-Access-Detection-System
cd ~/Classroom-Access-Detection-System
cp .env.example .env
nano .env                      # แก้ตามหัวข้อ 3
# ย้ายฐานข้อมูลจากโน้ตบุ๊ก (หัวข้อ 2.1) — ข้ามได้ถ้าจะนำเข้าสมาชิกใหม่บน Pi
docker compose up -d --build
```

backend พร้อมใช้งานเมื่อ `docker compose logs backend` ขึ้น `Application startup complete`
(บน Pi รอบแรกอาจใช้ 1–2 นาที เพราะต้องสร้างคลังใบหน้าจากรูปสมาชิกทุกคน
ถ้าเปิดหน้าเว็บก่อนนั้นจะเจอ 502 / ตารางค้าง "กำลังโหลด" ให้รอแล้วรีเฟรช)

ครั้งต่อไป (อัปเดตโค้ด):

```bash
cd ~/Classroom-Access-Detection-System
git pull
nano .env                      # เทียบกับ .env.example ว่ามีตัวแปรใหม่ไหม
docker compose up -d --build
```

สรุปลำดับ: **`git pull` → แก้ `.env` → `docker compose up -d --build`**

build ครั้งแรกบน Pi ใช้เวลานาน (หลายสิบนาที) เพราะต้องลง library และโหลดโมเดล
ครั้งต่อ ๆ ไป Docker ใช้ cache จึงเร็วขึ้นมาก เว้นแต่ `requirements.txt` หรือชุดโมเดลเปลี่ยน

### ⚠️ แก้ `.env` แล้วต้องใช้ `up -d` ไม่ใช่ `restart`

```bash
docker compose up -d backend        # ถูก: สร้าง container ใหม่ อ่าน .env ใหม่
docker compose restart backend      # ผิด: ใช้ environment เดิมตอนสร้าง container
```

`restart` แค่หยุดแล้วเริ่ม container **ตัวเดิม** ซึ่งจำค่า environment ไว้ตั้งแต่ตอนสร้าง
แก้ `.env` ไปเท่าไรก็ไม่มีผล และไม่มีอะไรฟ้องเลย ระบบจะขึ้นมาด้วยค่าเก่าเงียบ ๆ

ส่วนค่าที่เป็น build arg (`FACE_MODEL_PACK`, `FACE_REC_PACK`) ต้องใส่ `--build` ด้วย:

```bash
docker compose up -d --build backend
```

---

## 5. Checklist ตรวจรับหลัง deploy

ทำครบทุกข้อก่อนถือว่า deploy เสร็จ (เปิดหน้าเว็บจากเครื่องอื่นในวง LAN ที่ `http://<IP ของ Pi>:3000`)

**Container**

- [ ] `docker compose ps` — ทั้ง 4 service (`db`, `adminer`, `backend`, `frontend`) สถานะ `running` และ `db` เป็น `healthy`
- [ ] `docker compose logs backend | tail -80` — ไม่มี `[ERROR]` และมีบรรทัด `ตรวจ config กล้องแล้ว ไม่พบสิ่งผิดปกติ`
      (ถ้าเห็นกล่อง `!!!!` "พบการตั้งค่ากล้องที่ควรตรวจสอบ" ให้แก้ตามข้อความก่อนไปต่อ)

**`GET /api/health`** (`curl http://<IP ของ Pi>:8000/api/health`)

- [ ] `status` เป็น `ok`
- [ ] `face.model_pack` เป็น `buffalo_s` (ถ้ายังเป็น `buffalo_l` แปลว่าลืม `--build`)
- [ ] `database.connected` เป็น `true` และ `member_count` ตรงกับจำนวนสมาชิกจริง
- [ ] `config_warnings` เป็นลิสต์ว่าง `[]`
- [ ] `frame_source.type` เป็น `rtsp`, `cameras_installed` = 2, `cameras_alive` = 2

**หน้าเว็บ**

- [ ] ไม่มีแถบเตือนสีเหลืองเหนือจอกล้อง
- [ ] กด "เริ่มดูภาพสดทุกกล้อง" แล้วสถานะรวมขึ้น **"ปกติทุกกล้องที่ติดตั้ง"**
      (ถ้าขึ้น "ทำงานอยู่ แต่มีข้อควรตรวจสอบ" = ยังมีคำเตือนค้าง)
- [ ] URL ใต้ป้ายหัวจอของสองกล้อง **เป็นคนละ IP** และตรงกับกล้องที่ติดอยู่ที่ประตูนั้นจริง
- [ ] เดินผ่านกล้องขาเข้า → ภาพขึ้นในจอ "ขาเข้า" เท่านั้น / เดินผ่านกล้องขาออก → ขึ้นในจอ "ขาออก" เท่านั้น
      (ข้อนี้จับกรณีสลับ IP ระหว่าง CAMERA_1 กับ CAMERA_2 ซึ่งตัวตรวจ config จับไม่ได้)
- [ ] ตารางรายชื่อสมาชิกมีจำนวนคนตรงกับโน้ตบุ๊ก และจุดรูปใบหน้าเป็นสีเขียว (คลิกแล้วเห็นรูป)
- [ ] กดปุ่ม "โหลดรูปใบหน้าใหม่" → จำนวนเวกเตอร์ไม่เป็น 0 และไม่มีรูปที่ลงทะเบียนไม่สำเร็จ
- [ ] สมาชิกที่ลงทะเบียนแล้วเดินผ่าน → ขึ้นชื่อถูกต้อง และมีแถวใหม่ในตาราง "ประวัติการเข้า-ออกห้อง" พร้อมรูปหน้า
- [ ] คนที่ไม่ได้อยู่ในระบบเดินผ่าน → ขึ้นในประวัติเป็น "Unknown (ไม่รู้จัก)" พร้อมรูปหน้า
- [ ] ตัวเลข "ตรวจ" ของแต่ละจอใกล้กับที่ตั้งไว้ (ถ้าต่ำกว่ามาก = Pi เป็นคอขวด ให้ลด `DETECT_FPS_TOTAL` / `FACE_DET_SIZE`)

**ความทนทาน**

- [ ] ถอดปลั๊กกล้องตัวหนึ่ง → จอนั้นขึ้นสีแดงพร้อมสาเหตุ อีกจอยังทำงานปกติ → เสียบกลับ → กลับมาเองภายในไม่กี่สิบวินาที
- [ ] `sudo reboot` Pi → ระบบขึ้นมาเองครบทุก service โดยไม่ต้องสั่งอะไร (`restart: unless-stopped`)
