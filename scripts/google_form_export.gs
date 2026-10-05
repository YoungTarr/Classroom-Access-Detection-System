/**
 * CADS - คัดลอกรูปจาก Google Form ออกมาเป็นชื่อไฟล์ที่มีรหัสนักศึกษา
 *
 * วิธีใช้: เปิด Google Sheets ที่เก็บคำตอบของฟอร์ม -> ส่วนขยาย -> Apps Script
 *         วางโค้ดนี้ทั้งหมด -> กด Run (ฟังก์ชัน exportFacesForCads) -> อนุญาตสิทธิ์
 *
 * ผลลัพธ์: โฟลเดอร์ใหม่ชื่อ "CADS-export" ใน Google Drive ของคุณ ข้างในมี
 *   <รหัสนักศึกษา>_<front|left|right>_r<แถว>.<นามสกุลไฟล์>   (รูปทุกมุมของทุกคน)
 *   responses.csv                                                (รหัส อีเมล ชื่อ นามสกุล)
 * จากนั้นดาวน์โหลดโฟลเดอร์ CADS-export (ได้ไฟล์ .zip) ไปวางที่ data/import/
 *
 * สคริปต์นี้แค่ "คัดลอก" ไฟล์ ไม่ลบหรือแก้ของเดิมในฟอร์ม/Drive
 * ถ้าคนเดิมส่งซ้ำหลายรอบ จะได้ไฟล์ครบทุกรอบ (ต่างกันที่ _r<แถว>) ฝั่งนำเข้าจะเลือกรอบล่าสุดเอง
 */

// คำที่ใช้หาคอลัมน์จากหัวตาราง (ไม่ต้องตรงทั้งคำ ขอแค่มีคำนี้อยู่)
var COLUMN_KEYWORDS = {
  student_id: ['รหัส'],
  email: ['Email', 'อีเมล'],
  first_name: ['ชื่อจริง', 'ชื่อ'],
  last_name: ['นามสกุล'],
  front: ['ด้านหน้า', 'หน้าตรง'],
  left: ['ซ้าย'],
  right: ['ขวา'],
};

function exportFacesForCads() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  var rows = sheet.getDataRange().getDisplayValues();
  var header = rows[0];

  var col = findColumns_(header);
  var out = DriveApp.createFolder('CADS-export');
  var csv = [['row', 'student_id', 'email', 'first_name', 'last_name']];
  var copied = 0;
  var problems = [];

  for (var r = 1; r < rows.length; r++) {
    var row = rows[r];
    var sheetRow = r + 1;
    var sid = String(row[col.student_id] || '').replace(/\s+/g, '');
    if (!sid) {
      problems.push('แถว ' + sheetRow + ': ไม่มีรหัสนักศึกษา');
      continue;
    }
    csv.push([sheetRow, sid, row[col.email].trim(), row[col.first_name].trim(), row[col.last_name].trim()]);

    ['front', 'left', 'right'].forEach(function (angle) {
      var ids = extractFileIds_(row[col[angle]]);
      if (ids.length === 0) {
        problems.push('แถว ' + sheetRow + ' (' + sid + '): ไม่มีรูป ' + angle);
        return;
      }
      ids.forEach(function (id, i) {
        try {
          var file = DriveApp.getFileById(id);
          var ext = (file.getName().match(/\.[A-Za-z0-9]+$/) || ['.jpg'])[0].toLowerCase();
          var suffix = ids.length > 1 ? '_' + (i + 1) : '';
          file.makeCopy(sid + '_' + angle + '_r' + sheetRow + suffix + ext, out);
          copied++;
        } catch (e) {
          problems.push('แถว ' + sheetRow + ' (' + sid + '): เปิดรูป ' + angle + ' ไม่ได้ - ' + e);
        }
      });
    });
  }

  var csvText = csv.map(function (line) {
    return line.map(function (v) { return '"' + String(v).replace(/"/g, '""') + '"'; }).join(',');
  }).join('\n');
  out.createFile('responses.csv', '﻿' + csvText, MimeType.CSV);

  Logger.log('คัดลอกรูปแล้ว ' + copied + ' ไฟล์ จาก ' + (rows.length - 1) + ' คำตอบ');
  Logger.log('โฟลเดอร์ผลลัพธ์: ' + out.getUrl());
  if (problems.length) {
    Logger.log('พบปัญหา ' + problems.length + ' รายการ:\n' + problems.join('\n'));
  }
}

function findColumns_(header) {
  var col = {};
  Object.keys(COLUMN_KEYWORDS).forEach(function (key) {
    for (var i = 0; i < header.length; i++) {
      var title = header[i];
      // "ชื่อ" อย่างเดียวจะไปชนกับ "นามสกุล"/"ชื่อ-นามสกุล" จึงข้ามคอลัมน์ที่เป็นของ key อื่นแล้ว
      var taken = Object.keys(col).some(function (k) { return col[k] === i; });
      if (taken) continue;
      if (key === 'first_name' && title.indexOf('นามสกุล') !== -1) continue;
      if (COLUMN_KEYWORDS[key].some(function (word) { return title.indexOf(word) !== -1; })) {
        col[key] = i;
        break;
      }
    }
    if (col[key] === undefined) {
      throw new Error('หาคอลัมน์ ' + key + ' ไม่เจอ หัวตารางคือ: ' + header.join(' | '));
    }
  });
  Logger.log('คอลัมน์ที่ใช้: ' + JSON.stringify(Object.keys(col).reduce(function (o, k) {
    o[k] = header[col[k]]; return o;
  }, {})));
  return col;
}

function extractFileIds_(cell) {
  var ids = [];
  var re = /[?&]id=([A-Za-z0-9_-]+)|\/d\/([A-Za-z0-9_-]+)/g;
  var m;
  while ((m = re.exec(cell || '')) !== null) ids.push(m[1] || m[2]);
  return ids;
}
