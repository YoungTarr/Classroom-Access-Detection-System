-- =============================================================================
-- ข้อมูลตัวอย่างสำหรับทดสอบระบบ
--
-- ไฟล์นี้ถูก mount เข้า /docker-entrypoint-initdb.d/02-seed.sql (รันต่อจาก schema)
-- ใช้ ON CONFLICT DO NOTHING เพื่อให้รันซ้ำได้โดยไม่ error และไม่เขียนทับข้อมูลจริง
--
-- รูปใบหน้าเก็บในตาราง member_photos ไม่ได้ใส่ไว้ที่นี่
-- วางไฟล์ไว้ที่ data/faces/<รหัสนักศึกษา>/left.jpg, front.jpg, right.jpg แล้ว backend
-- จะย้ายเข้าฐานข้อมูลให้เองตอนสตาร์ท หรือนำเข้าจาก Google Form ด้วย scripts/import-members
-- (โฟลเดอร์ data/faces ถูกกันไม่ให้ขึ้น git เพราะเป็นข้อมูลส่วนบุคคล)
-- =============================================================================

INSERT INTO members (student_id, first_name, last_name)
VALUES
    ('66200407', 'สรกฤต',  'มีอินทร์'),
    ('66200425', 'จิราเดช', 'พยุหกฤษ')
ON CONFLICT (student_id) DO NOTHING;
