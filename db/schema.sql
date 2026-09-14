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
