-- =============================================================================
-- Classroom Access Detection System - โครงสร้างฐานข้อมูล
--
-- ไฟล์นี้ถูก mount เข้า /docker-entrypoint-initdb.d/01-schema.sql
-- PostgreSQL จะรันให้อัตโนมัติ "ครั้งแรกครั้งเดียว" ตอนสร้าง volume ใหม่
-- ถ้าแก้ไฟล์นี้แล้วอยากให้มีผล ต้องลบ volume ทิ้งก่อน: docker compose down -v
-- =============================================================================

-- -----------------------------------------------------------------------------
-- ตาราง members : รายชื่อสมาชิกของหลักสูตร (คนที่ระบบต้องจดจำใบหน้าได้)
--
-- หมายเหตุเรื่องรูป: ในฐานข้อมูลเก็บแค่ "path" ของไฟล์ ไม่ได้เก็บตัวรูป
-- ไฟล์รูปจริงอยู่ที่  data/faces/<student_id>/   บนเครื่อง host
-- ซึ่งถูก mount เข้า container ของ backend ที่ /data/faces (อ่านอย่างเดียว)
-- เก็บ 3 มุมต่อคน เพื่อให้จดจำได้แม้หันหน้าไม่ตรงกล้อง
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS members (
    id          SERIAL       PRIMARY KEY,
    student_id  VARCHAR(20)  NOT NULL UNIQUE,
    first_name  VARCHAR(100) NOT NULL,
    last_name   VARCHAR(100) NOT NULL,
    photo_left  VARCHAR(255),
    photo_front VARCHAR(255),
    photo_right VARCHAR(255),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- คำอธิบายรายคอลัมน์ (มองเห็นได้ใน Adminer / \d+ members)
COMMENT ON TABLE  members             IS 'รายชื่อสมาชิกหลักสูตรวิศวกรรมคอมพิวเตอร์ที่ระบบใช้จดจำใบหน้า';
COMMENT ON COLUMN members.id          IS 'รหัสอ้างอิงภายในระบบ (running number) ใช้เป็น primary key';
COMMENT ON COLUMN members.student_id  IS 'รหัสนักศึกษา ห้ามซ้ำ ใช้เป็นชื่อโฟลเดอร์เก็บรูปใน data/faces/';
COMMENT ON COLUMN members.first_name  IS 'ชื่อจริง (ภาษาไทย) ใช้แสดงบนกรอบใบหน้าเมื่อจดจำได้';
COMMENT ON COLUMN members.last_name   IS 'นามสกุล (ภาษาไทย) ใช้แสดงบนกรอบใบหน้าเมื่อจดจำได้';
COMMENT ON COLUMN members.photo_left  IS 'path ไฟล์รูปใบหน้าด้านซ้าย เทียบจากรากโปรเจกต์ เช่น data/faces/66200407/left.jpg';
COMMENT ON COLUMN members.photo_front IS 'path ไฟล์รูปใบหน้าด้านหน้า เทียบจากรากโปรเจกต์ เช่น data/faces/66200407/front.jpg';
COMMENT ON COLUMN members.photo_right IS 'path ไฟล์รูปใบหน้าด้านขวา เทียบจากรากโปรเจกต์ เช่น data/faces/66200407/right.jpg';
COMMENT ON COLUMN members.created_at  IS 'เวลาที่เพิ่มรายชื่อเข้าระบบ (โซนเวลา Asia/Bangkok)';
COMMENT ON COLUMN members.updated_at  IS 'เวลาที่แก้ไขล่าสุด อัปเดตอัตโนมัติด้วย trigger trg_members_updated_at';

-- index สำหรับเรียง/ค้นหาตามชื่อ (หน้าเว็บแสดงรายชื่อเรียงตามนามสกุล)
CREATE INDEX IF NOT EXISTS idx_members_name ON members (last_name, first_name);

-- -----------------------------------------------------------------------------
-- อัปเดต updated_at อัตโนมัติทุกครั้งที่มีการแก้ไขแถว
-- ใช้ CREATE OR REPLACE + DROP TRIGGER IF EXISTS เพื่อให้รันไฟล์นี้ซ้ำได้ไม่ error
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION set_updated_at() IS 'ตั้งค่า updated_at เป็นเวลาปัจจุบันก่อนบันทึกการแก้ไข';

DROP TRIGGER IF EXISTS trg_members_updated_at ON members;
CREATE TRIGGER trg_members_updated_at
    BEFORE UPDATE ON members
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- ตาราง access_logs : ประวัติการเข้า-ออกห้อง (เฟส 12)
--
-- หนึ่งแถว = หนึ่งครั้งที่ระบบจดจำใบหน้าสมาชิกได้ที่กล้องประตู
--   กล้องที่ตั้งทิศทาง IN  -> บันทึก direction = 'IN'  (เข้า)
--   กล้องที่ตั้งทิศทาง OUT -> บันทึก direction = 'OUT' (ออก)
-- ทิศทางมาจากการตั้งค่ากล้อง (CAMERA_n_DIRECTION) ไม่ได้มาจากการเดินซ้าย/ขวาในภาพ
--
-- เก็บชื่อ-นามสกุลซ้ำไว้ในแถว (snapshot) โดยตั้งใจ และไม่ทำ foreign key ไป members
-- เพื่อให้ประวัติยังถูกต้องแม้ภายหลังมีการแก้ชื่อหรือลบสมาชิกออก
--
-- ตารางนี้ถูกสร้างซ้ำด้วย CREATE TABLE IF NOT EXISTS ตอน backend สตาร์ทด้วย
-- (app/db/database.py) เพราะไฟล์นี้รันเฉพาะตอนสร้าง volume ใหม่เท่านั้น
-- ถ้าแก้โครงสร้างตรงนี้ ต้องแก้ใน database.py ให้ตรงกันด้วย
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS access_logs (
    id           BIGSERIAL    PRIMARY KEY,
    student_id   VARCHAR(20)  NOT NULL,
    first_name   VARCHAR(100) NOT NULL,
    last_name    VARCHAR(100) NOT NULL,
    logged_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    direction    VARCHAR(3)   NOT NULL CHECK (direction IN ('IN', 'OUT')),
    camera_id    VARCHAR(20)  NOT NULL,
    camera_name  VARCHAR(100),
    bbox_x       INTEGER      NOT NULL,
    bbox_y       INTEGER      NOT NULL,
    bbox_w       INTEGER      NOT NULL,
    bbox_h       INTEGER      NOT NULL,
    frame_width  INTEGER      NOT NULL,
    frame_height INTEGER      NOT NULL,
    confidence   REAL,
    track_id     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_access_logs_logged_at ON access_logs (logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_access_logs_student   ON access_logs (student_id, logged_at DESC);

COMMENT ON TABLE  access_logs              IS 'ประวัติการเข้า-ออกห้องที่ระบบบันทึกอัตโนมัติจากการจดจำใบหน้า';
COMMENT ON COLUMN access_logs.id           IS 'ลำดับของรายการ (running number)';
COMMENT ON COLUMN access_logs.student_id   IS 'รหัสนักศึกษาของคนที่จดจำได้';
COMMENT ON COLUMN access_logs.first_name   IS 'ชื่อ ณ เวลาที่บันทึก (snapshot)';
COMMENT ON COLUMN access_logs.last_name    IS 'นามสกุล ณ เวลาที่บันทึก (snapshot)';
COMMENT ON COLUMN access_logs.logged_at    IS 'วันเวลาที่ระบบจดจำได้ (เวลาที่ตรวจเจอ ไม่ใช่เวลาที่เขียนลงฐานข้อมูล)';
COMMENT ON COLUMN access_logs.direction    IS 'IN = เข้าห้อง (กล้องขาเข้า), OUT = ออกจากห้อง (กล้องขาออก)';
COMMENT ON COLUMN access_logs.camera_id    IS 'รหัสกล้องที่จับภาพได้ (CAMERA_IDS ใน .env)';
COMMENT ON COLUMN access_logs.camera_name  IS 'ชื่อกล้อง ณ เวลาที่บันทึก';
COMMENT ON COLUMN access_logs.bbox_x       IS 'พิกัดภาพ: มุมซ้ายบนของกรอบใบหน้า แกน X (พิกเซลของเฟรมต้นฉบับ)';
COMMENT ON COLUMN access_logs.bbox_y       IS 'พิกัดภาพ: มุมซ้ายบนของกรอบใบหน้า แกน Y (พิกเซลของเฟรมต้นฉบับ)';
COMMENT ON COLUMN access_logs.bbox_w       IS 'พิกัดภาพ: ความกว้างกรอบใบหน้า (พิกเซล)';
COMMENT ON COLUMN access_logs.bbox_h       IS 'พิกัดภาพ: ความสูงกรอบใบหน้า (พิกเซล)';
COMMENT ON COLUMN access_logs.frame_width  IS 'ความกว้างของเฟรมต้นฉบับที่ใช้ตรวจ (ไว้แปลงพิกัดเป็นสัดส่วน)';
COMMENT ON COLUMN access_logs.frame_height IS 'ความสูงของเฟรมต้นฉบับที่ใช้ตรวจ';
COMMENT ON COLUMN access_logs.confidence   IS 'คะแนนความคล้าย (cosine similarity 0-1) ของการจดจำครั้งนั้น';
COMMENT ON COLUMN access_logs.track_id     IS 'เลข track ของใบหน้าในกล้องนั้น (ไว้ debug)';
