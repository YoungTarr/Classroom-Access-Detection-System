-- =============================================================================
-- ข้อมูลตัวอย่างสำหรับทดสอบระบบ
--
-- ไฟล์นี้ถูก mount เข้า /docker-entrypoint-initdb.d/02-seed.sql (รันต่อจาก schema)
-- ใช้ ON CONFLICT DO NOTHING เพื่อให้รันซ้ำได้โดยไม่ error และไม่เขียนทับข้อมูลจริง
--
-- อย่าลืมวางไฟล์รูปจริงไว้ที่  data/faces/<รหัสนักศึกษา>/left.jpg, front.jpg, right.jpg
-- (โฟลเดอร์ data/faces ถูกกันไม่ให้ขึ้น git เพราะเป็นข้อมูลส่วนบุคคล)
-- =============================================================================

INSERT INTO members (student_id, first_name, last_name, photo_left, photo_front, photo_right)
VALUES
    ('66200407', 'สรกฤต',  'มีอินทร์',
     'data/faces/66200407/left.jpg',
     'data/faces/66200407/front.jpg',
     'data/faces/66200407/right.jpg'),

    ('66200425', 'จิราเดช', 'พยุหกฤษ',
     'data/faces/66200425/left.jpg',
     'data/faces/66200425/front.jpg',
     'data/faces/66200425/right.jpg')
ON CONFLICT (student_id) DO NOTHING;
