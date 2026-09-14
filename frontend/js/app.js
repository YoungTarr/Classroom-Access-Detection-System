/* =============================================================================
   Classroom Access Detection System - สคริปต์หน้าเว็บหลัก

   เฟส 1: สถานะการเชื่อมต่อฐานข้อมูล + ตารางรายชื่อสมาชิก
   เฟส 2: เปิดเว็บแคม ส่งเฟรมไปตรวจจับใบหน้า แล้ววาดกรอบทับภาพ

   สำคัญ: เรียก API ด้วย path สัมพัทธ์ "/api/..." และ "/ws/..." เสมอ
   ห้ามเขียน http://localhost:8000 ตรง ๆ เพราะ
     1) จะกลายเป็นคนละ origin แล้วติด CORS
     2) พอย้ายขึ้น Raspberry Pi เครื่องอื่นจะเรียกไม่ถูก (localhost = เครื่องผู้ใช้เอง)
   nginx ทำ reverse proxy /api/ และ /ws/ ไปให้ backend อยู่แล้ว
   ============================================================================= */

'use strict';

// ---------------------------------------------------------------------------
// ค่าคงที่
// ---------------------------------------------------------------------------
const API = {
  health: '/api/health',
  members: '/api/members',
  config: '/api/config',
};

// ตรวจสถานะซ้ำอัตโนมัติทุก 15 วินาที เพื่อให้เห็นทันทีเมื่อ backend/DB ล่มหรือกลับมา
const AUTO_REFRESH_MS = 15000;

/* ค่าที่ใช้ตอนยังโหลด /api/config ไม่สำเร็จ
   ตั้งใจให้เป็นค่าที่ "ทำงานได้แต่ไม่เงียบ" คือถ้าโหลด config ไม่ได้
   จะมีข้อความแจ้งเตือนขึ้นเสมอ ไม่ใช่แอบใช้ค่าพวกนี้แล้วเงียบไป */
const CONFIG_FALLBACK = {
  stream: { target_width: 640, send_fps: 10, jpeg_quality: 0.7, mirror: true },
  face: { det_thresh: 0.5, model_pack: '—' },
};

// สีที่ใช้วาดกรอบ (เฟส 4 จะเพิ่มสีเขียว/แดงตามผลการจดจำ)
const BOX_COLOR = '#4c8dff';
const BOX_LINE_WIDTH = 3;

// ---------------------------------------------------------------------------
// ตัวช่วยอ้างอิง element
// ---------------------------------------------------------------------------
const byId = (id) => document.getElementById(id);

const el = {
  phaseBadge: byId('phase-badge'),

  // กล้อง
  btnCamera: byId('btn-camera'),
  cameraSelect: byId('camera-select'),
  videoBox: byId('video-box'),
  video: byId('video'),
  overlay: byId('overlay'),
  videoIdle: byId('video-idle'),
  cameraError: byId('camera-error'),
  detectStatus: byId('detect-status'),

  // ตัวเลขสถานะ
  statResolution: byId('stat-resolution'),
  statSentSize: byId('stat-sent-size'),
  statFps: byId('stat-fps'),
  statProcess: byId('stat-process'),
  statFaces: byId('stat-faces'),
  statDropped: byId('stat-dropped'),

  // สถานะระบบ
  btnRefresh: byId('btn-refresh'),
  statusDot: byId('status-dot'),
  statusText: byId('status-text'),
  healthDetail: byId('health-detail'),
  dbVersion: byId('health-db-version'),
  dbTarget: byId('health-db-target'),
  dbCount: byId('health-db-count'),
  faceModel: byId('health-face-model'),
  frameSource: byId('health-frame-source'),
  serverTime: byId('health-server-time'),
  healthError: byId('health-error'),

  // สมาชิก
  memberCount: byId('member-count'),
  memberTbody: byId('member-tbody'),
  footerVersion: byId('footer-version'),
};

// ---------------------------------------------------------------------------
// สถานะของหน้าเว็บ
// ---------------------------------------------------------------------------
let config = CONFIG_FALLBACK;

const camera = new CameraController(el.video);

/** สถานะการตรวจจับทั้งหมดรวมไว้ที่เดียว อ่านง่ายกว่าตัวแปรลอย ๆ กระจัดกระจาย */
const detection = {
  socket: null,          // WebSocket
  running: false,        // กำลังส่งเฟรมอยู่หรือไม่
  frameId: 0,            // เลขลำดับเฟรมที่จะส่งครั้งถัดไป
  awaitingResult: false, // ส่งไปแล้วยังไม่ได้ผลกลับ (ใช้ทำ backpressure)
  sendTimer: null,

  // ผลล่าสุดที่ได้จาก backend
  lastFaces: [],
  lastSourceSize: null,  // ขนาดของภาพที่ส่งไปตรวจ [w, h]

  // สถิติ
  sentTimestamps: [],    // เวลาที่ส่งแต่ละเฟรม ใช้คำนวณ fps จริง
  droppedFrames: 0,
  lastProcessMs: null,
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

/** แสดง/ซ่อนกล่องข้อความผิดพลาด */
function showAlert(node, message) {
  if (message) {
    node.textContent = message;
    node.hidden = false;
  } else {
    node.textContent = '';
    node.hidden = true;
  }
}

/** จัดรูปแบบเวลา ISO ให้อ่านง่ายแบบไทย */
function formatTime(isoString) {
  if (!isoString) return '—';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  return d.toLocaleString('th-TH', { dateStyle: 'medium', timeStyle: 'medium' });
}

// ===========================================================================
// ส่วนที่ 1: โหลด config จาก backend
// ===========================================================================
async function loadConfig() {
  try {
    const res = await fetch(API.config, { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    config = await res.json();
    return true;
  } catch (err) {
    // ไม่เงียบ: บอกให้เห็นว่ากำลังใช้ค่าสำรองอยู่ ไม่ใช่ค่าที่ตั้งไว้จริง
    config = CONFIG_FALLBACK;
    showAlert(
      el.cameraError,
      'โหลดค่าตั้งค่าจาก ' + API.config + ' ไม่สำเร็จ: ' + err.message +
      '\nกำลังใช้ค่าสำรองชั่วคราว (ความกว้าง 640 / 10 fps) ซึ่งอาจไม่ตรงกับที่ตั้งไว้ใน .env'
    );
    return false;
  }
}

// ===========================================================================
// ส่วนที่ 2: กล้อง
// ===========================================================================

/** เติมรายชื่อกล้องลง dropdown */
async function refreshCameraList(selectedId) {
  try {
    const result = await camera.listCameras();

    el.cameraSelect.replaceChildren();

    if (result.cameras.length === 0) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '— ไม่พบกล้องบนเครื่องนี้ —';
      el.cameraSelect.appendChild(opt);
      return;
    }

    result.cameras.forEach((device, index) => {
      const opt = document.createElement('option');
      opt.value = device.deviceId;
      // ถ้ายังไม่ได้สิทธิ์ label จะว่าง ต้องตั้งชื่อชั่วคราวให้ผู้ใช้พอเลือกได้
      opt.textContent = device.label || ('กล้องตัวที่ ' + (index + 1) + ' (ยังไม่ทราบชื่อ)');
      if (device.deviceId === selectedId) opt.selected = true;
      el.cameraSelect.appendChild(opt);
    });

  } catch (err) {
    showAlert(el.cameraError, 'ดูรายชื่อกล้องไม่สำเร็จ: ' + err.message);
  }
}

/** เปิดกล้อง + เริ่มส่งเฟรมไปตรวจจับ */
async function startCamera() {
  showAlert(el.cameraError, null);
  el.btnCamera.disabled = true;

  try {
    // ขอสิทธิ์ก่อนถ้ายังไม่เคยได้ เพื่อให้ enumerateDevices() คืน "ชื่อ" กล้องมาด้วย
    // ถ้าไม่ทำขั้นนี้ dropdown จะมีแต่ "กล้องตัวที่ 1/2/3" ซึ่งผู้ใช้เลือกไม่ถูก
    const before = await camera.listCameras();
    if (!before.hasLabels) {
      await camera.requestPermission();
    }

    const deviceId = el.cameraSelect.value || null;
    const size = await camera.start(deviceId);

    // เปิดได้แล้วค่อยเติมรายชื่อ ตอนนี้จะได้ชื่อจริงของกล้องมาแล้ว
    await refreshCameraList(deviceId);

    el.videoIdle.hidden = true;
    el.btnCamera.textContent = 'ปิดกล้อง';
    el.btnCamera.classList.remove('btn--primary');
    setText(el.statResolution, size ? size.width + ' × ' + size.height : null);

    // ปรับภาพให้เป็นกระจกเงาถ้า config สั่ง (ธรรมชาติกว่าสำหรับเว็บแคม)
    el.video.classList.toggle('is-mirrored', Boolean(config.stream.mirror));

    syncOverlaySize();
    startDetection();

  } catch (err) {
    showAlert(el.cameraError, err.message);
    camera.stop();
  } finally {
    el.btnCamera.disabled = false;
  }
}

/** ปิดกล้อง + หยุดส่งเฟรม */
function stopCamera() {
  stopDetection();
  camera.stop();

  el.videoIdle.hidden = false;
  el.btnCamera.textContent = 'เปิดกล้อง';
  el.btnCamera.classList.add('btn--primary');

  clearOverlay();
  setText(el.statResolution, null);
  setText(el.statSentSize, null);
  setText(el.statFps, null);
  setText(el.statProcess, null);
  setText(el.statFaces, null);
  setText(el.statDropped, null);
}

el.btnCamera.addEventListener('click', () => {
  if (camera.isRunning) {
    stopCamera();
  } else {
    startCamera();
  }
});

// สลับกล้องระหว่างที่เปิดอยู่ = เปิดตัวใหม่ทันที
el.cameraSelect.addEventListener('change', () => {
  if (camera.isRunning) {
    stopCamera();
    startCamera();
  }
});

// ===========================================================================
// ส่วนที่ 3: WebSocket ตรวจจับใบหน้า
// ===========================================================================

/** ประกอบ URL ของ WebSocket จาก origin ปัจจุบัน (http -> ws, https -> wss) */
function buildWebSocketUrl(path) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return protocol + '//' + window.location.host + path;
}

function startDetection() {
  if (detection.socket) return;

  const socket = new WebSocket(buildWebSocketUrl('/ws/detect'));
  socket.binaryType = 'arraybuffer';
  detection.socket = socket;

  setText(el.detectStatus, 'กำลังเชื่อมต่อ…');

  socket.addEventListener('open', () => {
    detection.running = true;
    detection.frameId = 0;
    detection.awaitingResult = false;
    detection.droppedFrames = 0;
    detection.sentTimestamps = [];
    setText(el.detectStatus, 'กำลังตรวจจับ');
    scheduleNextFrame();
  });

  socket.addEventListener('message', (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      showAlert(el.cameraError, 'ได้รับข้อมูลที่อ่านไม่ออกจาก backend: ' + err.message);
      return;
    }

    // backend ส่ง type=error มาเมื่อมีปัญหาเฉพาะเฟรมหรือปัญหาทั้งระบบ
    if (data.type === 'error') {
      showAlert(el.cameraError, 'backend แจ้งข้อผิดพลาด: ' + data.message);
      detection.awaitingResult = false;
      return;
    }

    detection.awaitingResult = false;
    detection.lastFaces = data.faces || [];
    detection.lastSourceSize = data.source_size || null;
    detection.lastProcessMs = data.process_ms;

    setText(el.statProcess, data.process_ms !== undefined ? data.process_ms + ' ms' : null);
    setText(el.statFaces, detection.lastFaces.length + ' คน');
    setText(el.statSentSize, data.source_size ? data.source_size[0] + ' × ' + data.source_size[1] : null);

    drawDetections();
  });

  socket.addEventListener('error', () => {
    // event error ของ WebSocket ไม่บอกรายละเอียด ต้องรอ close ถึงจะรู้สาเหตุ
    setText(el.detectStatus, 'การเชื่อมต่อผิดพลาด');
  });

  socket.addEventListener('close', (event) => {
    detection.running = false;
    detection.socket = null;
    setText(el.detectStatus, 'ตัดการเชื่อมต่อแล้ว');

    if (camera.isRunning && !event.wasClean) {
      showAlert(
        el.cameraError,
        'การเชื่อมต่อกับ backend หลุด (code ' + event.code + ') — ' +
        'ตรวจ log ด้วยคำสั่ง docker compose logs backend'
      );
    }
  });
}

function stopDetection() {
  detection.running = false;

  if (detection.sendTimer) {
    clearTimeout(detection.sendTimer);
    detection.sendTimer = null;
  }

  if (detection.socket) {
    detection.socket.close(1000, 'ผู้ใช้ปิดกล้อง');
    detection.socket = null;
  }

  detection.lastFaces = [];
  detection.lastSourceSize = null;
  setText(el.detectStatus, 'ปิดอยู่');
}

/** ตั้งเวลาส่งเฟรมถัดไปตามอัตราที่ config กำหนด */
function scheduleNextFrame() {
  if (!detection.running) return;

  const intervalMs = 1000 / Math.max(1, config.stream.send_fps);
  detection.sendTimer = setTimeout(sendFrame, intervalMs);
}

/**
 * ส่งหนึ่งเฟรมไปตรวจจับ
 *
 * backpressure: ถ้าเฟรมก่อนหน้ายังไม่ได้ผลกลับมา ให้ "ทิ้งเฟรมนี้ไปเลย"
 * ห้ามเข้าคิวรอ เพราะถ้า AI ช้ากว่าอัตราที่เราส่ง คิวจะยาวขึ้นเรื่อย ๆ
 * แล้วกรอบที่เห็นจะช้ากว่าภาพจริงมากขึ้นทุกวินาทีจนใช้งานไม่ได้
 */
async function sendFrame() {
  if (!detection.running || !detection.socket) return;
  if (detection.socket.readyState !== WebSocket.OPEN) return;

  if (detection.awaitingResult) {
    detection.droppedFrames += 1;
    setText(el.statDropped, detection.droppedFrames + ' เฟรม');
    scheduleNextFrame();
    return;
  }

  try {
    const captured = await camera.captureJpeg(
      config.stream.target_width,
      config.stream.jpeg_quality
    );

    if (!captured) {
      scheduleNextFrame();
      return;
    }

    const jpegBuffer = await captured.blob.arrayBuffer();

    // ประกอบข้อความ: [frame_id 4 ไบต์ little-endian][ข้อมูล JPEG]
    const frameId = detection.frameId++;
    const message = new Uint8Array(4 + jpegBuffer.byteLength);
    new DataView(message.buffer).setUint32(0, frameId, true); // true = little-endian
    message.set(new Uint8Array(jpegBuffer), 4);

    detection.socket.send(message);
    detection.awaitingResult = true;

    recordSentFrame();

  } catch (err) {
    showAlert(el.cameraError, 'ส่งเฟรมไม่สำเร็จ: ' + err.message);
  }

  scheduleNextFrame();
}

/** บันทึกเวลาที่ส่งเฟรม แล้วคำนวณ fps จริงจากช่วง 2 วินาทีล่าสุด */
function recordSentFrame() {
  const now = performance.now();
  detection.sentTimestamps.push(now);

  // เก็บเฉพาะ 2 วินาทีล่าสุดพอ ไม่ให้ array โตไม่สิ้นสุด
  while (detection.sentTimestamps.length > 0 && now - detection.sentTimestamps[0] > 2000) {
    detection.sentTimestamps.shift();
  }

  const span = now - detection.sentTimestamps[0];
  if (span > 500 && detection.sentTimestamps.length > 1) {
    const fps = ((detection.sentTimestamps.length - 1) * 1000) / span;
    setText(el.statFps, fps.toFixed(1) + ' fps');
  }
}

// ===========================================================================
// ส่วนที่ 4: วาดกรอบทับภาพ
//
// การแปลงพิกัดมี 3 ชั้น ห้ามข้ามชั้นใดชั้นหนึ่ง:
//
//   ชั้นที่ 1  ภาพที่ส่งไปตรวจ   เช่น 640 × 360   <- พิกัดที่ backend ส่งกลับมาอยู่ในระบบนี้
//   ชั้นที่ 2  สตรีมจริงของกล้อง เช่น 1280 × 720
//   ชั้นที่ 3  ขนาดที่แสดงบนจอ   เช่น 820 × 461   <- ขนาดตาม CSS ซึ่งเปลี่ยนตามความกว้างหน้าต่าง
//
// ถ้าคำนวณผิดชั้น กรอบจะเลื่อนหรือขนาดไม่พอดีกับใบหน้า
// และเมื่อภาพเป็นกระจกเงา ต้องกลับพิกัดแกน X อีกชั้นหนึ่งด้วย
// ===========================================================================

/** ปรับขนาดและตำแหน่ง canvas ให้ทับ <video> พอดีเป๊ะ */
function syncOverlaySize() {
  const videoRect = el.video.getBoundingClientRect();
  if (videoRect.width === 0 || videoRect.height === 0) return;

  // ต้องวาง canvas ตาม "ตำแหน่งจริงของวิดีโอภายในกล่อง" ไม่ใช่ปักไว้ที่มุมซ้ายบนเฉย ๆ
  // เพราะกล่องมี min-height และจัดกลางแนวตั้ง ถ้าวิดีโอเตี้ยกว่ากล่อง (เช่นตอนจอแคบมาก)
  // วิดีโอจะถูกดันลงมากลางกล่อง แล้วกรอบที่วาดจะเลื่อนขึ้นไปจากใบหน้า
  const boxRect = el.videoBox.getBoundingClientRect();
  const offsetLeft = videoRect.left - boxRect.left;
  const offsetTop = videoRect.top - boxRect.top;

  // คูณด้วย devicePixelRatio เพื่อให้เส้นคมบนจอความละเอียดสูง
  // ถ้าไม่คูณ กรอบจะเบลอบนจอ Retina / จอ 4K ที่ตั้ง scale ไว้
  const dpr = window.devicePixelRatio || 1;

  el.overlay.width = Math.round(videoRect.width * dpr);
  el.overlay.height = Math.round(videoRect.height * dpr);
  el.overlay.style.width = videoRect.width + 'px';
  el.overlay.style.height = videoRect.height + 'px';
  el.overlay.style.left = offsetLeft + 'px';
  el.overlay.style.top = offsetTop + 'px';

  const ctx = el.overlay.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // ทำงานในหน่วย CSS pixel ต่อจากนี้
}

function clearOverlay() {
  const ctx = el.overlay.getContext('2d');
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, el.overlay.width, el.overlay.height);
  ctx.restore();
}

function drawDetections() {
  const ctx = el.overlay.getContext('2d');
  clearOverlay();

  if (!detection.lastSourceSize || detection.lastFaces.length === 0) return;

  const sentWidth = detection.lastSourceSize[0];
  const sentHeight = detection.lastSourceSize[1];

  const streamSize = camera.streamSize;
  if (!streamSize) return;

  const rect = el.video.getBoundingClientRect();
  const displayWidth = rect.width;
  const displayHeight = rect.height;
  if (displayWidth === 0) return;

  // ชั้นที่ 1 -> ชั้นที่ 2 : จากภาพที่ส่ง ไปเป็นพิกัดบนสตรีมจริง
  const toStreamX = streamSize.width / sentWidth;
  const toStreamY = streamSize.height / sentHeight;

  // ชั้นที่ 2 -> ชั้นที่ 3 : จากสตรีมจริง ไปเป็นพิกัดบนจอ
  const toDisplayX = displayWidth / streamSize.width;
  const toDisplayY = displayHeight / streamSize.height;

  const mirrored = Boolean(config.stream.mirror);

  ctx.lineWidth = BOX_LINE_WIDTH;
  ctx.strokeStyle = BOX_COLOR;
  ctx.font = '600 13px "Sarabun", "Segoe UI", sans-serif';
  ctx.textBaseline = 'top';

  detection.lastFaces.forEach((face) => {
    const b = face.bbox; // [x, y, w, h] ในระบบพิกัดของภาพที่ส่งไป

    let x = b[0] * toStreamX * toDisplayX;
    let y = b[1] * toStreamY * toDisplayY;
    const w = b[2] * toStreamX * toDisplayX;
    const h = b[3] * toStreamY * toDisplayY;

    // ภาพถูกพลิกกระจกด้วย CSS transform: scaleX(-1) แต่ canvas ไม่ได้ถูกพลิกตาม
    // จึงต้องกลับพิกัดแกน X เอง ไม่งั้นกรอบจะไปโผล่คนละฝั่งกับใบหน้า
    if (mirrored) {
      x = displayWidth - (x + w);
    }

    ctx.strokeRect(x, y, w, h);

    // ป้ายคะแนน พร้อมพื้นหลังทึบเพื่อให้อ่านออกบนภาพสว่าง
    const label = (face.score * 100).toFixed(0) + '%';
    const textWidth = ctx.measureText(label).width;
    const padding = 5;
    const labelHeight = 20;

    // ถ้าป้ายล้นขอบบนของภาพ ให้ย้ายไปไว้ใต้กรอบแทน
    const labelY = (y - labelHeight - 2 < 0) ? (y + h + 2) : (y - labelHeight - 2);

    ctx.fillStyle = BOX_COLOR;
    ctx.fillRect(x, labelY, textWidth + padding * 2, labelHeight);

    ctx.fillStyle = '#ffffff';
    ctx.fillText(label, x + padding, labelY + 3);

    ctx.fillStyle = BOX_COLOR; // คืนค่าให้กรอบถัดไป
  });
}

// ขนาดที่แสดงเปลี่ยนได้ตลอด (ย่อ/ขยายหน้าต่าง) ต้องปรับ canvas แล้ววาดใหม่
const resizeObserver = new ResizeObserver(() => {
  if (!camera.isRunning) return;
  syncOverlaySize();
  drawDetections();
});
resizeObserver.observe(el.video);

// ===========================================================================
// ส่วนที่ 5: สถานะระบบ + รายชื่อสมาชิก (เฟส 1)
// ===========================================================================
async function loadHealth() {
  try {
    const res = await fetch(API.health, { cache: 'no-store' });

    // backend ตอบ 503 เมื่อมีส่วนใดใช้งานไม่ได้ แต่ body ยังเป็น JSON ที่มีสาเหตุอยู่
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

    const face = data.face || {};
    setText(
      el.faceModel,
      face.loaded ? (face.model_pack + ' (det ' + face.det_size + ')') : 'ยังไม่ได้โหลด'
    );

    const src = data.frame_source || {};
    setText(el.frameSource, src.type ? (src.type + (src.alive ? ' (พร้อม)' : ' (ไม่พร้อม)')) : null);

    if (data.status === 'ok') {
      setStatus('ok', 'ระบบพร้อมใช้งาน');
      showAlert(el.healthError, null);
      return true;
    }

    // แยกให้ชัดว่าพังที่ส่วนไหน ผู้ใช้จะได้ไม่ต้องเดา
    const problems = [];
    if (!db.connected || db.error) {
      problems.push('ฐานข้อมูล: ' + (db.error || 'เชื่อมต่อไม่ได้'));
    }
    if (!face.loaded) {
      problems.push('โมเดลใบหน้า: ' + (face.error || 'ยังไม่ได้โหลด'));
    }

    setStatus('error', 'ระบบทำงานได้ไม่ครบ');
    showAlert(el.healthError, problems.join('\n\n') || 'ไม่ทราบสาเหตุ');
    return false;

  } catch (err) {
    // มาถึงตรงนี้แปลว่าเรียก backend ไม่ถึงเลย (nginx proxy ไม่ได้ / backend ยังไม่ขึ้น)
    el.healthDetail.hidden = true;
    setStatus('error', 'ติดต่อ backend ไม่ได้');
    showAlert(
      el.healthError,
      'เรียก ' + API.health + ' ไม่สำเร็จ: ' + err.message + '\n\n' +
      'ตรวจสอบ:\n' +
      '• container ขึ้นครบหรือยัง -> docker compose ps\n' +
      '• log ของ backend -> docker compose logs backend'
    );
    return false;
  }
}

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

// ===========================================================================
// วงจรหลัก
// ===========================================================================
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

// ปิดกล้องให้เรียบร้อยเมื่อออกจากหน้า ไม่ปล่อยให้ไฟกล้องค้าง
window.addEventListener('beforeunload', () => {
  stopDetection();
  camera.stop();
});

// เริ่มทำงาน
(async function init() {
  await loadConfig();
  await refreshAll();
  setInterval(refreshAll, AUTO_REFRESH_MS);
})();
