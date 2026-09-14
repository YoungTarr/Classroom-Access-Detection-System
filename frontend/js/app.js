/* =============================================================================
   Classroom Access Detection System - สคริปต์หน้าเว็บหลัก (เฟส 1)
   หน้าที่: แสดงสถานะการเชื่อมต่อฐานข้อมูล + ตารางรายชื่อสมาชิก

   สำคัญ: เรียก API ด้วย path สัมพัทธ์ "/api/..." เสมอ
   ห้ามเขียน http://localhost:8000 ตรง ๆ เพราะ
     1) จะกลายเป็นคนละ origin แล้วติด CORS
     2) พอย้ายขึ้น Raspberry Pi เครื่องอื่นจะเรียกไม่ถูก (localhost = เครื่องผู้ใช้เอง)
   nginx ทำ reverse proxy /api/ ไปให้ backend อยู่แล้ว
   ============================================================================= */

'use strict';

// ---------------------------------------------------------------------------
// ค่าคงที่
// ---------------------------------------------------------------------------
const API = {
  health: '/api/health',
  members: '/api/members',
};

// ตรวจสถานะซ้ำอัตโนมัติทุก 15 วินาที เพื่อให้เห็นทันทีเมื่อ backend/DB ล่มหรือกลับมา
const AUTO_REFRESH_MS = 15000;

// ---------------------------------------------------------------------------
// ตัวช่วยอ้างอิง element
// ---------------------------------------------------------------------------
const byId = (id) => document.getElementById(id);

const el = {
  phaseBadge: byId('phase-badge'),
  btnRefresh: byId('btn-refresh'),
  statusDot: byId('status-dot'),
  statusText: byId('status-text'),
  healthDetail: byId('health-detail'),
  dbVersion: byId('health-db-version'),
  dbTarget: byId('health-db-target'),
  dbCount: byId('health-db-count'),
  serverTime: byId('health-server-time'),
  healthError: byId('health-error'),
  memberCount: byId('member-count'),
  memberTbody: byId('member-tbody'),
  footerVersion: byId('footer-version'),
};

// ---------------------------------------------------------------------------
// ตัวช่วยทั่วไป
// ---------------------------------------------------------------------------

/** ใส่ข้อความแบบ textContent เสมอ (ไม่ใช้ innerHTML) กัน XSS จากข้อมูลใน DB */
function setText(node, value, fallback) {
  const blank = (value === null || value === undefined || value === '');
  node.textContent = blank ? (fallback || '—') : String(value);
}

/** เปลี่ยนสถานะจุดกลม + ข้อความ  state: pending | ok | error */
function setStatus(state, message) {
  el.statusDot.className = 'status__dot status__dot--' + state;
  el.statusText.textContent = message;
}

/** แสดง/ซ่อนกล่อง error พร้อมสาเหตุ */
function showError(message) {
  if (message) {
    el.healthError.textContent = message;
    el.healthError.hidden = false;
  } else {
    el.healthError.textContent = '';
    el.healthError.hidden = true;
  }
}

/** จัดรูปแบบเวลา ISO ให้อ่านง่ายแบบไทย */
function formatTime(isoString) {
  if (!isoString) return '—';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  return d.toLocaleString('th-TH', { dateStyle: 'medium', timeStyle: 'medium' });
}

// ---------------------------------------------------------------------------
// เรียก /api/health
// ---------------------------------------------------------------------------
async function loadHealth() {
  try {
    const res = await fetch(API.health, { cache: 'no-store' });

    // backend ตอบ 503 เมื่อต่อฐานข้อมูลไม่ได้ แต่ body ยังเป็น JSON ที่มีสาเหตุอยู่
    // จึงต้องอ่าน body ก่อนเสมอ ไม่ใช่ทิ้งไปเพราะ status ไม่ใช่ 200
    const data = await res.json();

    el.healthDetail.hidden = false;

    const phase = data.app && data.app.phase;
    setText(el.phaseBadge, phase ? 'เฟส ' + phase : null, 'เฟส —');
    setText(el.footerVersion, data.app ? 'v' + data.app.version : null);

    const db = data.database || {};
    setText(el.dbVersion, db.version);
    setText(el.dbTarget, db.target);
    const count = db.member_count;
    setText(el.dbCount, (count === null || count === undefined) ? null : count + ' คน');
    setText(el.serverTime, formatTime(data.server_time));

    if (data.status === 'ok') {
      setStatus('ok', 'เชื่อมต่อฐานข้อมูลสำเร็จ');
      showError(null);
      return true;
    }

    setStatus('error', 'เชื่อมต่อฐานข้อมูลไม่สำเร็จ');
    showError('สาเหตุ: ' + (db.error || 'ไม่ทราบสาเหตุ (backend ไม่ได้ส่งรายละเอียดมา)'));
    return false;

  } catch (err) {
    // มาถึงตรงนี้แปลว่าเรียก backend ไม่ถึงเลย (nginx proxy ไม่ได้ / backend ยังไม่ขึ้น)
    el.healthDetail.hidden = true;
    setStatus('error', 'ติดต่อ backend ไม่ได้');
    showError(
      'เรียก ' + API.health + ' ไม่สำเร็จ: ' + err.message + '\n\n' +
      'ตรวจสอบ:\n' +
      '• container ขึ้นครบหรือยัง -> docker compose ps\n' +
      '• log ของ backend -> docker compose logs backend'
    );
    return false;
  }
}

// ---------------------------------------------------------------------------
// เรียก /api/members แล้ววาดตาราง
// ---------------------------------------------------------------------------

/** แถวข้อความกลางตาราง ใช้ตอนกำลังโหลด / ไม่มีข้อมูล / เกิดข้อผิดพลาด */
function renderTableMessage(message) {
  el.memberTbody.replaceChildren();
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.colSpan = 4;
  td.className = 'table__empty';
  td.textContent = message;
  tr.appendChild(td);
  el.memberTbody.appendChild(tr);
}

/** จุดสามจุดบอกว่ามี path รูปซ้าย/หน้า/ขวา ครบหรือไม่ */
function buildPhotoDots(photos) {
  const wrap = document.createElement('span');
  wrap.className = 'photo-dots';

  const angles = [['left', 'ซ้าย'], ['front', 'หน้า'], ['right', 'ขวา']];
  angles.forEach(function (pair) {
    const key = pair[0];
    const label = pair[1];
    const dot = document.createElement('span');
    const has = Boolean(photos && photos[key]);
    dot.className = 'photo-dot' + (has ? ' photo-dot--set' : '');
    dot.title = label + ': ' + (has ? photos[key] : 'ยังไม่ได้กำหนด');
    wrap.appendChild(dot);
  });

  return wrap;
}

async function loadMembers() {
  try {
    const res = await fetch(API.members, { cache: 'no-store' });

    if (!res.ok) {
      // ไม่กลืน error เงียบ ๆ ต้องบอกว่าเป็น HTTP อะไร
      throw new Error('HTTP ' + res.status + ' ' + res.statusText);
    }

    const data = await res.json();
    const members = data.members || [];

    setText(el.memberCount, members.length + ' คน');

    if (members.length === 0) {
      renderTableMessage('ยังไม่มีข้อมูลสมาชิก (ตรวจสอบว่า db/seed.sql ถูกรันแล้วหรือยัง)');
      return;
    }

    el.memberTbody.replaceChildren();
    members.forEach(function (m) {
      const tr = document.createElement('tr');

      const tdId = document.createElement('td');
      tdId.textContent = m.student_id;

      const tdFirst = document.createElement('td');
      tdFirst.textContent = m.first_name;

      const tdLast = document.createElement('td');
      tdLast.textContent = m.last_name;

      const tdPhotos = document.createElement('td');
      tdPhotos.appendChild(buildPhotoDots(m.photos));

      tr.append(tdId, tdFirst, tdLast, tdPhotos);
      el.memberTbody.appendChild(tr);
    });

  } catch (err) {
    setText(el.memberCount, 'ผิดพลาด');
    renderTableMessage('โหลดรายชื่อไม่สำเร็จ: ' + err.message);
  }
}

// ---------------------------------------------------------------------------
// วงจรหลัก
// ---------------------------------------------------------------------------
async function refreshAll() {
  el.btnRefresh.disabled = true;
  setStatus('pending', 'กำลังตรวจสอบ…');

  const healthy = await loadHealth();

  if (healthy) {
    await loadMembers();
  } else {
    // ต่อฐานข้อมูลไม่ได้ ก็ไม่ต้องยิง /api/members ให้ error ซ้ำซ้อน
    setText(el.memberCount, null);
    renderTableMessage('รอการเชื่อมต่อฐานข้อมูล');
  }

  el.btnRefresh.disabled = false;
}

el.btnRefresh.addEventListener('click', refreshAll);

refreshAll();
setInterval(refreshAll, AUTO_REFRESH_MS);
