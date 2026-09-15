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
  tracking: { smoothing: 0.35, max_missing: 3 },
  direction: { reference: 'world', min_shift_ratio: 0.025, frame_gap: 8 },
};

/* สีกรอบตามผลการจดจำ (เฟส 4)
   เขียว = รู้ว่าเป็นใคร / แดง = ไม่ใช่คนในระบบ / น้ำเงิน = กำลังตรวจสอบอยู่

   ที่ต้องมีสีน้ำเงินด้วย เพราะ "ยังตรวจไม่เสร็จ" กับ "ตรวจแล้วไม่รู้จัก"
   เป็นคนละเรื่องกัน ถ้าใช้สีแดงทั้งคู่ ทุกคนที่เดินเข้ามาจะเห็นกรอบแดง
   แวบหนึ่งก่อนเปลี่ยนเป็นเขียวเสมอ ซึ่งสะดุดตาและทำให้เข้าใจผิด */
const BOX_COLORS = {
  recognized: '#3ecf8e',
  unknown: '#ff5c5c',
  pending: '#4c8dff',
};

const BOX_LINE_WIDTH = 3;

// จำนวนเฟรมที่ต้องเจอติดกันก่อนจะเริ่มวาดกรอบ
// กันสัญญาณรบกวนที่ตัวตรวจจับเจอแวบเดียวแล้วหายไป ไม่ให้กรอบผุดขึ้นมาแวบหนึ่ง
const MIN_HITS_TO_DRAW = 2;

// ความจางของกรอบที่กำลัง "ค้างไว้" เพราะหาใบหน้าไม่เจอชั่วคราว
const FADED_ALPHA = 0.35;

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
  statTracks: byId('stat-tracks'),
  statKnown: byId('stat-known'),
  statRenderFps: byId('stat-render-fps'),
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
  identifyInfo: byId('health-identify'),
  frameSource: byId('health-frame-source'),
  directionInfo: byId('health-direction'),
  btnReloadFaces: byId('btn-reload-faces'),
  reloadResult: byId('reload-result'),
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
  lastSentAt: 0,         // performance.now() ตอนส่งเฟรมล่าสุด
  watchdogTimer: null,   // กันค้างถ้า backend ไม่ตอบกลับมาเลย

  // ผลล่าสุดที่ได้จาก backend
  lastSourceSize: null,  // ขนาดของภาพที่ส่งไปตรวจ [w, h]

  // สถิติ
  sentTimestamps: [],    // เวลาที่ส่งแต่ละเฟรม ใช้คำนวณ fps จริง
  droppedFrames: 0,
  lastProcessMs: null,
};

/* ---------------------------------------------------------------------------
   สถานะการวาดกรอบ (เฟส 3)

   แยกออกจาก detection โดยตั้งใจ เพราะสองอย่างนี้เดินคนละจังหวะกัน:
     - detection เดินตามผลจาก backend  (~8-10 ครั้ง/วินาที)
     - การวาดเดินตามจังหวะรีเฟรชของจอ (~60 ครั้ง/วินาที)

   กรอบแต่ละอันจึงมีทั้ง "ตำแหน่งเป้าหมาย" (ผลล่าสุดจาก backend)
   และ "ตำแหน่งที่วาดอยู่จริง" ซึ่งค่อย ๆ ไหลเข้าหาเป้าหมายทุกเฟรมของจอ
   --------------------------------------------------------------------------- */
const render = {
  // Map: track_id -> { current, target, score, hits, visible, missing, alpha, targetAlpha, dead }
  boxes: new Map(),
  rafId: null,
  lastTickAt: 0,

  // นับอัตราการวาดจริง เพื่อยืนยันว่าวาดทุกเฟรมของจอจริง ไม่ใช่วาดเฉพาะตอนได้ผลใหม่
  tickTimestamps: [],
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
    startRenderLoop();
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
  stopRenderLoop();
  camera.stop();

  el.videoIdle.hidden = false;
  el.btnCamera.textContent = 'เปิดกล้อง';
  el.btnCamera.classList.add('btn--primary');

  setText(el.statResolution, null);
  setText(el.statSentSize, null);
  setText(el.statFps, null);
  setText(el.statProcess, null);
  setText(el.statFaces, null);
  setText(el.statTracks, null);
  setText(el.statRenderFps, null);
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
    setText(el.statDropped, '0 เฟรม');
    scheduleNextFrame(0);
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
      // ต้องเดินจังหวะต่อ ไม่งั้นวงจรจะหยุดนิ่งถาวรหลังเจอ error ครั้งเดียว
      onResultSettled();
      return;
    }

    onResultSettled();
    detection.lastSourceSize = data.source_size || null;
    detection.lastProcessMs = data.process_ms;

    const tracks = data.tracks || [];
    const visibleCount = tracks.filter((t) => t.visible).length;
    const knownCount = tracks.filter((t) => t.identity_state === 'recognized').length;

    // แสดงเวลาแยกตามขั้น จะได้รู้ว่าเวลาหมดไปกับอะไร
    const timing = (data.detect_ms !== undefined)
      ? (data.process_ms + ' ms (ตรวจ ' + data.detect_ms +
         ' + จดจำ ' + (data.identify_ms !== undefined ? data.identify_ms : 0) + ')')
      : (data.process_ms + ' ms');
    setText(el.statProcess, data.process_ms !== undefined ? timing : null);

    setText(el.statFaces, (data.detected_count !== undefined ? data.detected_count : visibleCount) + ' คน');
    setText(el.statTracks, tracks.length + ' track');
    setText(el.statKnown, knownCount + ' / ' + tracks.length + ' คน');
    setText(el.statSentSize, data.source_size ? data.source_size[0] + ' × ' + data.source_size[1] : null);

    // ป้อนผลใหม่ให้ตัววาด แต่ไม่วาดตรงนี้ - ปล่อยให้วงวาดของจอเป็นคนวาด
    updateRenderTargets(tracks);
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

  if (detection.watchdogTimer) {
    clearTimeout(detection.watchdogTimer);
    detection.watchdogTimer = null;
  }
  detection.awaitingResult = false;

  if (detection.socket) {
    detection.socket.close(1000, 'ผู้ใช้ปิดกล้อง');
    detection.socket = null;
  }

  detection.lastSourceSize = null;
  setText(el.detectStatus, 'ปิดอยู่');
}

/** ช่วงเวลาขั้นต่ำระหว่างเฟรม ตามอัตราที่ config กำหนด */
function frameIntervalMs() {
  return 1000 / Math.max(1, config.stream.send_fps);
}

/**
 * ตั้งเวลาส่งเฟรมถัดไป
 *
 * จังหวะการส่งเป็นแบบ "ยิงต่อเมื่อได้ผลของเฟรมก่อนกลับมาแล้ว" ไม่ใช่ยิงตามนาฬิกาตายตัว
 *
 * เหตุผล: ถ้าตั้ง setInterval คงที่ 100 ms แล้วการตรวจจับใช้เวลา ~98 ms
 * จังหวะจะชนกันพอดี คือ timer ดังตอนที่ผลยังมาไม่ถึง เลยต้องทิ้งเฟรมนั้น
 * แล้วไปรออีก 100 ms เต็ม ผลคือได้จริงแค่ครึ่งเดียว (~5 fps) และทิ้งเฟรมครึ่งหนึ่งทิ้ง ๆ
 *
 * การนับจากเวลาที่ "พร้อมจริง" ทำให้ได้อัตราใกล้เคียงที่ตั้งไว้โดยไม่มีเฟรมค้างในคิว
 */
function scheduleNextFrame(delayMs) {
  if (!detection.running) return;

  if (detection.sendTimer) clearTimeout(detection.sendTimer);
  detection.sendTimer = setTimeout(sendFrame, Math.max(0, delayMs));
}

/** เรียกเมื่อได้ผล (หรือ error) กลับมา เพื่อเดินจังหวะเฟรมถัดไปต่อ */
function onResultSettled() {
  detection.awaitingResult = false;

  if (detection.watchdogTimer) {
    clearTimeout(detection.watchdogTimer);
    detection.watchdogTimer = null;
  }

  // หักเวลาที่ใช้ตรวจจับไปแล้วออก จะได้ไม่ช้ากว่าอัตราที่ตั้งไว้โดยไม่จำเป็น
  const elapsed = performance.now() - detection.lastSentAt;
  scheduleNextFrame(frameIntervalMs() - elapsed);
}

/**
 * ส่งหนึ่งเฟรมไปตรวจจับ
 *
 * backpressure: ห้ามส่งเฟรมใหม่ขณะที่เฟรมก่อนยังไม่ได้ผลกลับ ให้ทิ้งเฟรมนั้นไปเลย
 * ห้ามเข้าคิวรอ เพราะถ้า AI ช้ากว่าอัตราที่เราส่ง คิวจะยาวขึ้นเรื่อย ๆ
 * แล้วกรอบที่เห็นจะช้ากว่าภาพจริงมากขึ้นทุกวินาทีจนใช้งานไม่ได้
 */
async function sendFrame() {
  if (!detection.running || !detection.socket) return;
  if (detection.socket.readyState !== WebSocket.OPEN) return;

  // ปกติจะไม่เข้าเงื่อนไขนี้แล้ว เพราะเราส่งต่อเมื่อได้ผลกลับมาแล้วเท่านั้น
  // เก็บไว้เป็นตาข่ายกันพลาด (เช่นมี timer ค้างจากการสลับกล้อง)
  if (detection.awaitingResult) {
    detection.droppedFrames += 1;
    setText(el.statDropped, detection.droppedFrames + ' เฟรม');
    scheduleNextFrame(frameIntervalMs());
    return;
  }

  try {
    const captured = await camera.captureJpeg(
      config.stream.target_width,
      config.stream.jpeg_quality
    );

    if (!captured) {
      // กล้องยังไม่พร้อมส่งภาพ รอรอบถัดไป
      scheduleNextFrame(frameIntervalMs());
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
    detection.lastSentAt = performance.now();

    recordSentFrame();

    // ถ้า backend ไม่ตอบกลับมาเลย (ค้าง/ตาย) ต้องไม่ปล่อยให้วงจรหยุดนิ่งถาวร
    // ครบเวลาแล้วให้ถือว่าเฟรมนั้นหายไป แล้วเดินต่อ พร้อมแจ้งให้ผู้ใช้เห็น
    detection.watchdogTimer = setTimeout(() => {
      if (!detection.awaitingResult) return;
      detection.droppedFrames += 1;
      setText(el.statDropped, detection.droppedFrames + ' เฟรม');
      showAlert(el.cameraError, 'backend ไม่ตอบกลับภายใน 5 วินาที — กำลังลองเฟรมถัดไป');
      onResultSettled();
    }, 5000);

  } catch (err) {
    showAlert(el.cameraError, 'ส่งเฟรมไม่สำเร็จ: ' + err.message);
    scheduleNextFrame(frameIntervalMs());
  }
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

/**
 * คำนวณตัวคูณสำหรับแปลงพิกัด "ภาพที่ส่งไปตรวจ" ให้เป็น "พิกัดบนจอ"
 * คืน null ถ้ายังคำนวณไม่ได้ (กล้องยังไม่พร้อม หรือยังไม่เคยได้ผลจาก backend)
 */
function getCoordinateTransform() {
  if (!detection.lastSourceSize) return null;

  const streamSize = camera.streamSize;
  if (!streamSize) return null;

  const rect = el.video.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;

  const sentWidth = detection.lastSourceSize[0];
  const sentHeight = detection.lastSourceSize[1];

  return {
    // ชั้นที่ 1 -> ชั้นที่ 2 : จากภาพที่ส่ง ไปเป็นพิกัดบนสตรีมจริง
    toStreamX: streamSize.width / sentWidth,
    toStreamY: streamSize.height / sentHeight,
    // ชั้นที่ 2 -> ชั้นที่ 3 : จากสตรีมจริง ไปเป็นพิกัดบนจอ
    toDisplayX: rect.width / streamSize.width,
    toDisplayY: rect.height / streamSize.height,
    displayWidth: rect.width,
    displayHeight: rect.height,
  };
}

/**
 * รับผลชุดใหม่จาก backend มาตั้งเป็น "เป้าหมาย" ของแต่ละกรอบ
 *
 * ไม่วาดที่นี่ เพราะการวาดเป็นหน้าที่ของวงวาดที่เดินตามจังหวะจอ
 * ที่นี่แค่บอกว่า "กรอบควรจะไปอยู่ตรงไหน" เท่านั้น
 */
function updateRenderTargets(tracks) {
  const seen = new Set();

  tracks.forEach((t) => {
    const box = { x: t.bbox[0], y: t.bbox[1], w: t.bbox[2], h: t.bbox[3] };
    seen.add(t.track_id);

    let entry = render.boxes.get(t.track_id);

    if (!entry) {
      // กรอบใหม่: ให้ตำแหน่งที่วาดเริ่มตรงกับเป้าหมายเลย
      // ถ้าเริ่มจากที่อื่นแล้วค่อยไหลเข้ามา จะเห็นกรอบวิ่งมาจากมุมจอ ซึ่งดูแปลก
      entry = {
        current: Object.assign({}, box),
        target: box,
        score: t.score,
        hits: t.hits,
        visible: t.visible,
        missing: t.missing,
        identityState: t.identity_state || 'pending',
        identity: t.identity || null,
        direction: t.direction || null,
        alpha: 0,          // เริ่มจากโปร่งใสแล้วค่อย ๆ ชัดขึ้น
        targetAlpha: 1,
      };
      render.boxes.set(t.track_id, entry);
    } else {
      entry.target = box;
      entry.score = t.score;
      entry.hits = t.hits;
      entry.visible = t.visible;
      entry.missing = t.missing;
      entry.identityState = t.identity_state || 'pending';
      entry.identity = t.identity || null;
      entry.direction = t.direction || null;
    }

    // กรอบที่ backend ค้างไว้ (หาใบหน้าไม่เจอชั่วคราว) ให้วาดจาง ๆ
    // ผู้ใช้จะได้รู้ว่าระบบ "กำลังเดาตำแหน่งอยู่" ไม่ใช่เห็นจริง
    if (!t.visible) {
      entry.targetAlpha = FADED_ALPHA;
    } else if (t.hits >= MIN_HITS_TO_DRAW) {
      entry.targetAlpha = 1;
    } else {
      // เพิ่งเจอเฟรมเดียว ยังไม่แน่ว่าใช่ใบหน้าจริง รอดูอีกเฟรม
      entry.targetAlpha = 0;
    }
  });

  // track ที่หายไปจากผลชุดนี้ = backend ลบทิ้งแล้ว ให้ค่อย ๆ จางหายไป
  // ไม่ลบทันทีเพื่อไม่ให้กรอบ "ดับวับ" ซึ่งสะดุดตากว่าการจางหาย
  render.boxes.forEach((entry, trackId) => {
    if (!seen.has(trackId)) {
      entry.targetAlpha = 0;
      entry.dead = true;
    }
  });
}

/**
 * ค่าที่ใช้ไหลเข้าหาเป้าหมาย โดยไม่ขึ้นกับว่าจอรีเฟรชเร็วแค่ไหน
 *
 * ถ้าใช้ค่าคงที่ตรง ๆ (current += (target-current) * 0.35) ความลื่นจะเปลี่ยนไป
 * ตามอัตรารีเฟรชของจอ คือจอ 144Hz จะไหลเร็วกว่าจอ 60Hz กว่าสองเท่า
 * สูตรนี้ปรับให้ได้ผลเท่ากันทุกจอ โดยอิงจากเวลาจริงที่ผ่านไป
 */
function smoothingFactor(dtMs) {
  const s = Math.min(0.999, Math.max(0.001, config.tracking.smoothing));
  return 1 - Math.pow(1 - s, dtMs / 16.667); // 16.667 ms = หนึ่งเฟรมที่ 60Hz
}

/** ไหลค่าหนึ่งค่าเข้าหาเป้าหมาย */
function lerp(from, to, k) {
  return from + (to - from) * k;
}

/**
 * วงวาดหลัก - ทำงานทุกเฟรมของจอ (ปกติ 60 ครั้ง/วินาที)
 *
 * นี่คือหัวใจของเฟส 3: ของเดิมวาดเฉพาะตอนได้ผลใหม่จาก backend (~8 ครั้ง/วินาที)
 * ตาคนจึงเห็นกรอบกระโดดเป็นจังหวะ ๆ
 * พอแยกการวาดออกมาเดินตามจอแล้วไหลตำแหน่งทีละนิด กรอบจะเกาะใบหน้าลื่นขึ้นมาก
 */
function renderTick(now) {
  render.rafId = requestAnimationFrame(renderTick);

  const dt = render.lastTickAt ? (now - render.lastTickAt) : 16.667;
  render.lastTickAt = now;

  // วัดอัตราการวาดจริงจากช่วง 1 วินาทีล่าสุด
  render.tickTimestamps.push(now);
  while (render.tickTimestamps.length && now - render.tickTimestamps[0] > 1000) {
    render.tickTimestamps.shift();
  }

  const k = smoothingFactor(dt);

  // ไหลทุกกรอบเข้าหาเป้าหมาย แล้วเก็บกวาดตัวที่จางจนมองไม่เห็นแล้ว
  render.boxes.forEach((entry, trackId) => {
    entry.current.x = lerp(entry.current.x, entry.target.x, k);
    entry.current.y = lerp(entry.current.y, entry.target.y, k);
    entry.current.w = lerp(entry.current.w, entry.target.w, k);
    entry.current.h = lerp(entry.current.h, entry.target.h, k);
    entry.alpha = lerp(entry.alpha, entry.targetAlpha, k);

    if (entry.dead && entry.alpha < 0.02) {
      render.boxes.delete(trackId);
    }
  });

  drawBoxes();
}

/**
 * ประกอบข้อความบนป้ายกำกับจากผลการจดจำ
 *
 * ชื่อ-นามสกุลมาจากฐานข้อมูลผ่าน backend เสมอ ที่นี่แค่เอามาต่อกัน
 * ห้ามมีชื่อคนเขียนตายตัวอยู่ในไฟล์นี้เด็ดขาด
 */
function buildLabel(entry) {
  const ident = entry.identity;

  if (entry.identityState === 'recognized' && ident) {
    const name = [ident.first_name, ident.last_name].filter(Boolean).join(' ');
    const percent = Math.round((ident.confidence || 0) * 100);
    return ident.student_id + '  ' + name + '  ' + percent + '%';
  }

  if (entry.identityState === 'unknown') {
    return 'Unknown';
  }

  // pending: กำลังรวบรวมผลโหวตอยู่
  return 'กำลังตรวจสอบ…';
}

/** วาดกรอบทั้งหมดตามตำแหน่งที่ไหลมาถึงตอนนี้ */
function drawBoxes() {
  const ctx = el.overlay.getContext('2d');
  clearOverlay();

  const tf = getCoordinateTransform();
  if (!tf) return;

  const mirrored = Boolean(config.stream.mirror);

  ctx.lineWidth = BOX_LINE_WIDTH;
  ctx.font = '600 14px "Sarabun", "Segoe UI", sans-serif';
  ctx.textBaseline = 'top';
  ctx.lineJoin = 'round';

  render.boxes.forEach((entry) => {
    if (entry.alpha < 0.02) return;

    const c = entry.current;

    let x = c.x * tf.toStreamX * tf.toDisplayX;
    const y = c.y * tf.toStreamY * tf.toDisplayY;
    const w = c.w * tf.toStreamX * tf.toDisplayX;
    const h = c.h * tf.toStreamY * tf.toDisplayY;

    // ภาพถูกพลิกกระจกด้วย CSS transform: scaleX(-1) แต่ canvas ไม่ได้ถูกพลิกตาม
    // จึงต้องกลับพิกัดแกน X เอง ไม่งั้นกรอบจะไปโผล่คนละฝั่งกับใบหน้า
    if (mirrored) {
      x = tf.displayWidth - (x + w);
    }

    const color = BOX_COLORS[entry.identityState] || BOX_COLORS.pending;

    ctx.globalAlpha = entry.alpha;
    ctx.strokeStyle = color;
    ctx.strokeRect(x, y, w, h);

    // ---- ป้ายกำกับ ----
    const label = buildLabel(entry);
    const padding = 7;
    const labelHeight = 23;
    const textWidth = ctx.measureText(label).width;
    const labelWidth = textWidth + padding * 2;

    // ถ้าป้ายล้นขอบบนของภาพ ให้ย้ายไปไว้ใต้กรอบแทน
    let labelY = y - labelHeight - 2;
    if (labelY < 0) {
      labelY = y + h + 2;
    }

    // ถ้าป้ายยาวจนล้นขอบขวา ให้ดันกลับเข้ามาให้อ่านครบ
    // (ชื่อไทยเต็ม ๆ กับรหัสนักศึกษารวมกันยาวกว่ากรอบใบหน้าเสมอ)
    let labelX = x;
    if (labelX + labelWidth > tf.displayWidth) {
      labelX = Math.max(0, tf.displayWidth - labelWidth);
    }

    // พื้นหลังทึบรองข้อความ เพื่อให้อ่านออกแม้ฉากหลังสว่าง
    ctx.fillStyle = color;
    ctx.fillRect(labelX, labelY, labelWidth, labelHeight);

    ctx.fillStyle = '#0f1420';  // ตัวอักษรสีเข้มบนพื้นสีสด อ่านง่ายกว่าสีขาว
    ctx.fillText(label, labelX + padding, labelY + 4);

    // ---- ป้ายทิศทางการเคลื่อนที่ (เฟส 5) ----
    // วางไว้ "ใต้กรอบ" เสมอ เพื่อไม่ให้ชนกับป้ายชื่อที่อยู่ด้านบน
    // ถ้าล้นขอบล่างของภาพก็ย้ายขึ้นมาไว้ในกรอบแทน
    if (entry.direction) {
      const dirLabel = entry.direction.label;
      const dirWidth = ctx.measureText(dirLabel).width + padding * 2;
      const dirHeight = 21;

      // ถ้าป้ายชื่อถูกดันลงมาอยู่ใต้กรอบแล้ว ให้ป้ายทิศทางลงมาต่อท้ายอีกชั้น
      const nameLabelIsBelow = labelY > y;
      let dirY = y + h + 2 + (nameLabelIsBelow ? labelHeight + 2 : 0);

      if (dirY + dirHeight > tf.displayHeight) {
        dirY = Math.max(0, y + h - dirHeight - 2);
      }

      let dirX = x;
      if (dirX + dirWidth > tf.displayWidth) {
        dirX = Math.max(0, tf.displayWidth - dirWidth);
      }

      // "อยู่กับที่" ใช้สีเทาเข้มให้ดูเงียบกว่า เพราะไม่ใช่เหตุการณ์ที่ต้องสนใจ
      // ส่วนตอนเคลื่อนที่ใช้พื้นเข้มตัวหนังสือสว่าง ให้สะดุดตากว่า
      const moving = entry.direction.value !== 'still';
      ctx.fillStyle = moving ? 'rgba(15, 20, 32, 0.85)' : 'rgba(15, 20, 32, 0.55)';
      ctx.fillRect(dirX, dirY, dirWidth, dirHeight);

      ctx.fillStyle = moving ? '#ffffff' : '#93a0b8';
      ctx.fillText(dirLabel, dirX + padding, dirY + 3);
    }
  });

  ctx.globalAlpha = 1;
}

function startRenderLoop() {
  if (render.rafId !== null) return;
  render.lastTickAt = 0;
  render.tickTimestamps = [];
  render.rafId = requestAnimationFrame(renderTick);
}

function stopRenderLoop() {
  if (render.rafId !== null) {
    cancelAnimationFrame(render.rafId);
    render.rafId = null;
  }
  render.boxes.clear();
  clearOverlay();
}

// อัปเดตตัวเลขอัตราการวาดทุกครึ่งวินาทีพอ ไม่ต้องอัปเดตทุกเฟรมให้เปลืองเปล่า
setInterval(() => {
  if (render.rafId === null) return;
  setText(el.statRenderFps, render.tickTimestamps.length + ' fps');
}, 500);

// ขนาดที่แสดงเปลี่ยนได้ตลอด (ย่อ/ขยายหน้าต่าง) ต้องปรับขนาด canvas ตาม
// ไม่ต้องสั่งวาดเอง เพราะวงวาดเดินอยู่ทุกเฟรมของจออยู่แล้ว
const resizeObserver = new ResizeObserver(() => {
  if (!camera.isRunning) return;
  syncOverlaySize();
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
      face.loaded
        ? (face.model_pack + ' (det ' + face.det_size + ') + ' + (face.recognition_model || '—'))
        : 'ยังไม่ได้โหลด'
    );

    // สรุปสถานะคลังใบหน้า พร้อมเตือนถ้ามีคนที่ระบบจะจำไม่ได้
    const idn = data.identify || {};
    if (idn.ready) {
      let text = idn.vector_count + ' เวกเตอร์ จาก ' + idn.enrolled_count + '/' + idn.member_count + ' คน';
      if (idn.problem_count) text += '  (มีปัญหา ' + idn.problem_count + ' ไฟล์)';
      setText(el.identifyInfo, text);
    } else {
      setText(el.identifyInfo, idn.error ? ('ใช้ไม่ได้: ' + idn.error) : 'ยังไม่มีรูปใบหน้าในระบบ');
    }

    const src = data.frame_source || {};
    setText(el.frameSource, src.type ? (src.type + (src.alive ? ' (พร้อม)' : ' (ไม่พร้อม)')) : null);

    // บอกให้ชัดว่า "ซ้าย/ขวา" ที่ระบบรายงาน หมายถึงด้านไหน
    const dirRef = (config.direction && config.direction.reference) || '—';
    const mirrored = Boolean(config.stream.mirror);
    let dirText = dirRef;
    if (dirRef === 'world') {
      dirText += mirrored ? '  (ตามที่กล้องเห็นจริง — กลับด้านกับที่เห็นบนจอ)' : '  (ตามที่กล้องเห็นจริง)';
    } else {
      dirText += mirrored ? '  (ตามที่เห็นบนจอ — จอพลิกกระจกอยู่)' : '  (ตามที่เห็นบนจอ)';
    }
    setText(el.directionInfo, dirText);

    // คลังใบหน้าว่างไม่ถือว่าระบบพัง (ตรวจจับ/ติดตามยังทำงานได้)
    // แต่ต้องเตือนให้เห็นชัด ไม่งั้นผู้ใช้จะงงว่าทำไมทุกคนขึ้น Unknown
    const faceLibraryEmpty = idn.ready === false && !idn.error;

    if (data.status === 'ok') {
      if (faceLibraryEmpty) {
        setStatus('pending', 'ระบบพร้อม แต่ยังไม่มีรูปใบหน้าให้เทียบ');
        showAlert(
          el.healthError,
          'ยังไม่มีเวกเตอร์ใบหน้าในระบบ ทุกคนจะขึ้นเป็น Unknown\n\n' +
          'วิธีแก้: วางไฟล์รูปไว้ที่ data/faces/<รหัสนักศึกษา>/left.jpg, front.jpg, right.jpg ' +
          'แล้วกดปุ่ม "โหลดรูปใบหน้าใหม่" ด้านล่าง'
        );
      } else {
        setStatus('ok', 'ระบบพร้อมใช้งาน');
        showAlert(el.healthError, null);
      }
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
    if (idn.error) {
      problems.push('การระบุตัวตน: ' + idn.error);
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

/**
 * สร้างคลังเวกเตอร์ใบหน้าใหม่ โดยไม่ต้องรีสตาร์ท container
 * ใช้หลังเพิ่มรูปลงใน data/faces/ หรือแก้ข้อมูลสมาชิกในฐานข้อมูล
 */
async function reloadFaces() {
  el.btnReloadFaces.disabled = true;
  el.btnReloadFaces.textContent = 'กำลังอ่านรูป…';
  showAlert(el.reloadResult, null);

  try {
    const res = await fetch('/api/faces/reload', { cache: 'no-store' });
    const data = await res.json();

    if (data.status !== 'ok') {
      el.reloadResult.className = 'alert alert--error';
      showAlert(el.reloadResult, 'สร้างคลังใหม่ไม่สำเร็จ: ' + (data.message || 'ไม่ทราบสาเหตุ'));
      return;
    }

    const report = data.report || {};
    let text = data.message + '  (ใช้เวลา ' + report.build_ms + ' ms)';

    // รายงานปัญหาให้ครบทุกไฟล์ ไม่สรุปรวมว่า "มีบางไฟล์ใช้ไม่ได้"
    // เพราะผู้ใช้ต้องรู้ว่าต้องไปแก้ไฟล์ไหนของใคร
    if (report.members_without_vectors && report.members_without_vectors.length) {
      text += '\n\nคนที่ระบบจะจำไม่ได้ (ไม่มีรูปที่ใช้ได้เลย):\n  • ' +
        report.members_without_vectors.join('\n  • ');
    }
    if (report.problems && report.problems.length) {
      text += '\n\nไฟล์ที่มีปัญหา:\n  • ' + report.problems.join('\n  • ');
    }

    const hasProblem = (report.problems && report.problems.length) ||
                       (report.members_without_vectors && report.members_without_vectors.length);
    el.reloadResult.className = hasProblem ? 'alert alert--warn' : 'alert alert--ok';
    showAlert(el.reloadResult, text);

    // อัปเดตสถานะด้านบนให้ตรงกับคลังใหม่ทันที
    await loadHealth();

  } catch (err) {
    el.reloadResult.className = 'alert alert--error';
    showAlert(el.reloadResult, 'เรียก /api/faces/reload ไม่สำเร็จ: ' + err.message);
  } finally {
    el.btnReloadFaces.disabled = false;
    el.btnReloadFaces.textContent = 'โหลดรูปใบหน้าใหม่';
  }
}

el.btnReloadFaces.addEventListener('click', reloadFaces);

// ปิดกล้องให้เรียบร้อยเมื่อออกจากหน้า ไม่ปล่อยให้ไฟกล้องค้าง
window.addEventListener('beforeunload', () => {
  stopDetection();
  stopRenderLoop();
  camera.stop();
});

// เริ่มทำงาน
(async function init() {
  await loadConfig();
  await refreshAll();
  setInterval(refreshAll, AUTO_REFRESH_MS);
})();
