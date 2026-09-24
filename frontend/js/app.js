/* =============================================================================
   Classroom Access Detection System - สคริปต์หน้าเว็บหลัก

   เฟส 1: สถานะการเชื่อมต่อฐานข้อมูล + ตารางรายชื่อสมาชิก
   เฟส 2: เปิดเว็บแคม ส่งเฟรมไปตรวจจับใบหน้า แล้ววาดกรอบทับภาพ
   เฟส 3-5: กรอบไหลลื่น + จดจำว่าเป็นใคร + ทิศทางการเคลื่อนที่
   เฟส 6-7: รับภาพจากกล้อง IP + แยกอัตรา fps สามค่า
   เฟส 8: แสดงกล้องหลายตัวพร้อมกัน (ขาเข้า / ขาออก)
   เฟส 9: กล้องทุกตัวเป็นกล้อง IP + สถานะ "ยังไม่ได้ติดตั้งกล้อง"

   ============================================================================
   ไฟล์นี้ทำหน้าที่ "ประกอบ" อย่างเดียว
   ============================================================================

   ตรรกะหนัก ๆ ถูกแยกออกไปเป็นคลาสที่สร้างซ้ำได้ เพราะเฟส 8 ต้องมีหลายจอ:

       overlay.js       FaceOverlay    วาดกรอบทับภาพหนึ่งภาพ
       stream-panel.js  StreamPanel    จอกล้องหนึ่งตัว (DOM + WebSocket + สถิติ)
       camera.js        CameraController  เปิดเว็บแคม + จับภาพเป็น JPEG
                                          (ใช้เฉพาะโหมดเว็บแคมเดี่ยว)

   ไฟล์นี้จึงเหลือแค่: โหลด config -> เลือกโหมด -> สร้างจอ -> สถานะระบบ -> รายชื่อ

   ============================================================================
   สองโหมดที่ต่างกันโดยสิ้นเชิง
   ============================================================================

   โหมดกล้องหลายตัว (FRAME_SOURCE=rtsp)  <- โหมดที่ใช้งานจริง
       backend ต่อกล้องเอง ตรวจเสร็จแล้ว push ภาพ+ผล มาให้หน้าเว็บ
       หน้าเว็บสร้างจอหนึ่งจอต่อกล้องหนึ่งตัว แล้วแสดงพร้อมกันทั้งหมด
       **โหมดนี้ไม่แตะเว็บแคมของเครื่องเลย** (ไม่เรียก getUserMedia)
       กล้องที่ยังไม่ได้ติดตั้งยังมีจอของตัวเอง แต่ขึ้นว่า "ยังไม่ได้ติดตั้งกล้อง"

   โหมดเว็บแคมเดี่ยว (FRAME_SOURCE=browser)  <- ไว้พัฒนา/ทดสอบตอนไม่มีกล้องเลย
       เบราว์เซอร์จับภาพเอง ส่งไปตรวจ แล้ววาดกรอบทับ <video> ในเครื่อง
       เป็นโหมดของทั้งระบบ ไม่ได้ผูกกับกล้องตัวใดตัวหนึ่ง

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
  cameras: '/api/cameras',
};

// ตรวจสถานะซ้ำอัตโนมัติทุก 15 วินาที เพื่อให้เห็นทันทีเมื่อ backend/DB ล่มหรือกลับมา
const AUTO_REFRESH_MS = 15000;

/* ค่าที่ใช้ตอนยังโหลด /api/config ไม่สำเร็จ
   ตั้งใจให้เป็นค่าที่ "ทำงานได้แต่ไม่เงียบ" คือถ้าโหลด config ไม่ได้
   จะมีข้อความแจ้งเตือนขึ้นเสมอ ไม่ใช่แอบใช้ค่าพวกนี้แล้วเงียบไป */
const CONFIG_FALLBACK = {
  stream: { source: 'browser', target_width: 640, send_fps: 10, jpeg_quality: 0.7, mirror: true },
  face: { det_thresh: 0.5, model_pack: '—' },
  tracking: { smoothing: 0.35, max_missing: 3 },
  direction: { reference: 'world', min_shift_ratio: 0.025, frame_gap: 8 },
};

// ---------------------------------------------------------------------------
// ตัวช่วยอ้างอิง element
// ---------------------------------------------------------------------------
const byId = (id) => document.getElementById(id);

const el = {
  phaseBadge: byId('phase-badge'),

  // ---- โหมดกล้องหลายตัว (เฟส 8) ----
  modeCameras: byId('mode-cameras'),
  camerasStatus: byId('cameras-status'),
  btnCameras: byId('btn-cameras'),
  camerasHint: byId('cameras-hint'),
  cameraGrid: byId('camera-grid'),
  camerasError: byId('cameras-error'),
  summaryFaces: byId('summary-faces'),
  summaryKnown: byId('summary-known'),
  summaryCameras: byId('summary-cameras'),
  summaryOverall: byId('summary-overall'),

  // ---- โหมดเว็บแคมเดี่ยว (เฟส 2) ----
  modeWebcam: byId('mode-webcam'),
  btnCamera: byId('btn-camera'),
  cameraSelect: byId('camera-select'),
  videoBox: byId('video-box'),
  video: byId('video'),
  overlay: byId('overlay'),
  videoIdle: byId('video-idle'),
  videoIdleText: byId('video-idle-text'),
  videoIdleHint: byId('video-idle-hint'),
  cameraError: byId('camera-error'),
  detectStatus: byId('detect-status'),
  statResolution: byId('stat-resolution'),
  statSentSize: byId('stat-sent-size'),
  statFps: byId('stat-fps'),
  statProcess: byId('stat-process'),
  statFaces: byId('stat-faces'),
  statTracks: byId('stat-tracks'),
  statKnown: byId('stat-known'),
  statRenderFps: byId('stat-render-fps'),
  statDropped: byId('stat-dropped'),

  // ---- สถานะระบบ ----
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
  healthCamerasRow: byId('health-cameras-row'),
  healthCameras: byId('health-cameras'),
  directionInfo: byId('health-direction'),
  btnReloadFaces: byId('btn-reload-faces'),
  reloadResult: byId('reload-result'),
  serverTime: byId('health-server-time'),
  healthError: byId('health-error'),

  // ---- สมาชิก ----
  memberCount: byId('member-count'),
  memberTbody: byId('member-tbody'),
  footerVersion: byId('footer-version'),
};

// ---------------------------------------------------------------------------
// สถานะของหน้าเว็บ
// ---------------------------------------------------------------------------
let config = CONFIG_FALLBACK;

/** ส่งให้คลาสต่าง ๆ ใช้อ่าน config โดยไม่ต้องรู้ว่าเก็บไว้ที่ตัวแปรไหน */
const getConfig = () => config;

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

/** ประกอบ URL ของ WebSocket จาก origin ปัจจุบัน (http -> ws, https -> wss) */
function buildDetectSocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return protocol + '//' + window.location.host + '/ws/detect';
}

/** ตอนนี้ backend อยู่ในโหมดกล้องหลายตัวหรือไม่ */
function isCameraMode() {
  return config.stream && config.stream.source === 'rtsp';
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
// ส่วนที่ 2: โหมดกล้องหลายตัว (เฟส 8)
// ===========================================================================

/** จอกล้องทั้งหมดบนหน้าเว็บ: cameraId -> StreamPanel */
const panels = new Map();

/** ผู้ใช้กดเริ่มดูภาพอยู่หรือไม่ */
let camerasRunning = false;

/**
 * สร้างจอให้ครบทุกกล้องตามรายการจาก backend
 *
 * เรียกครั้งเดียวตอนเปิดหน้า ไม่ได้สร้างใหม่ทุกครั้งที่สำรวจสถานะ
 * เพราะการรื้อ DOM ทิ้งจะทำให้ภาพกะพริบและ WebSocket ต้องต่อใหม่ทั้งหมด
 */
async function buildCameraPanels() {
  let data;
  try {
    const res = await fetch(API.cameras, { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    data = await res.json();
  } catch (err) {
    showAlert(el.camerasError, 'โหลดรายชื่อกล้องไม่สำเร็จ: ' + err.message);
    return;
  }

  const cameras = (data.cameras || []).filter((c) => c.enabled);

  if (cameras.length === 0) {
    showAlert(
      el.camerasError,
      'ไม่มีกล้องที่เปิดใช้งานเลย\n' +
      'ตรวจค่า CAMERA_IDS และ CAMERA_<ชื่อ>_ENABLED ในไฟล์ .env'
    );
    el.btnCameras.disabled = true;
    return;
  }

  el.cameraGrid.replaceChildren();
  panels.clear();

  cameras.forEach((cam) => {
    const panel = new StreamPanel(cam, {
      getConfig: getConfig,
      onUpdate: updateSummary,
    });
    panel.mount(el.cameraGrid);
    panel.applyCameraInfo(cam);
    panels.set(cam.id, panel);
  });

  updateSummary();
}

/** เริ่มดูภาพสดทุกกล้องพร้อมกัน */
function startAllCameras() {
  if (camerasRunning) return;
  camerasRunning = true;

  showAlert(el.camerasError, null);
  el.btnCameras.textContent = 'หยุดดูภาพสด';
  el.btnCameras.classList.remove('btn--primary');
  setText(el.camerasStatus, 'กำลังรับภาพสด');

  // ทุกจอเปิด WebSocket ของตัวเองแยกกัน ตัวไหนล้มไม่กระทบตัวอื่น
  // (จอของกล้องที่ยังไม่ได้ติดตั้งจะไม่เปิด WebSocket เลย - ดู StreamPanel.start)
  panels.forEach((panel) => panel.start());
  updateSummary();
}

/** หยุดดูภาพทุกกล้อง */
function stopAllCameras() {
  camerasRunning = false;

  panels.forEach((panel) => panel.stop());

  el.btnCameras.textContent = 'เริ่มดูภาพสดทุกกล้อง';
  el.btnCameras.classList.add('btn--primary');
  setText(el.camerasStatus, 'ปิดอยู่');
  updateSummary();
}

/**
 * สรุปรวมทุกกล้องด้านบนหน้าเว็บ (ข้อ 11)
 *
 * รวมจาก "ตัวเลขล่าสุดของแต่ละจอ" ไม่ได้ไปนับใหม่เอง
 * เพื่อให้ยอดรวมกับตัวเลขในจอตรงกันเสมอ ไม่มีทางขัดกันเอง
 */
function updateSummary() {
  let faces = 0;
  let known = 0;
  let alive = 0;
  let installed = 0;

  panels.forEach((panel) => {
    faces += panel.lastDetectedCount;
    known += panel.lastKnownCount;
    if (panel.alive) alive += 1;
    if (panel.isInstalled) installed += 1;
  });

  // กล้องที่ยังไม่ได้ติดตั้งไม่นับเป็น "หลุด" (เฟส 9)
  // ถ้านับรวม ระบบที่มีกล้องตัวเดียวและทำงานปกติดีจะขึ้นเตือนตลอดเวลา
  // ผู้ใช้จะชินกับคำเตือนจนไม่สนใจ แล้วพอกล้องหลุดจริงก็ไม่มีใครเห็น
  const notInstalled = panels.size - installed;
  const down = installed - alive;

  // บอกให้ชัดว่ามีกล้องกี่ตัว และติดตั้งจริงแล้วกี่ตัว
  // คำนวณตรงนี้ (ไม่ใช่ตอนสร้างจอครั้งเดียว) เพราะติดตั้งกล้องเพิ่มได้ระหว่างเปิดหน้าค้างไว้
  let hint = 'มีกล้อง ' + panels.size + ' ตัว';
  if (notInstalled > 0) {
    hint += ' (ติดตั้งแล้ว ' + installed + ' · ยังไม่ได้ติดตั้ง ' + notInstalled + ')';
  }
  setText(el.camerasHint, hint);

  setText(el.summaryFaces, faces + ' คน');
  setText(el.summaryKnown, known + ' คน');
  setText(
    el.summaryCameras,
    alive + ' / ' + installed + ' ตัว' +
    (notInstalled > 0 ? ' (ยังไม่ได้ติดตั้ง ' + notInstalled + ')' : '')
  );

  // สถานะรวม: บอกภาพใหญ่ในบรรทัดเดียว โดยไม่กลบรายละเอียดของแต่ละจอ
  let overall;
  let cls = 'summary__value';

  if (!camerasRunning) {
    overall = 'ยังไม่ได้เริ่มดูภาพ';
  } else if (installed === 0) {
    overall = 'ยังไม่ได้ติดตั้งกล้องเลย';
    cls += ' summary__value--warn';
  } else if (down === 0) {
    overall = 'ปกติทุกกล้องที่ติดตั้ง';
    cls += ' summary__value--ok';
  } else if (alive === 0) {
    overall = 'ไม่มีกล้องส่งภาพเลย';
    cls += ' summary__value--error';
  } else {
    overall = 'มีกล้องหลุด ' + down + ' ตัว';
    cls += ' summary__value--warn';
  }

  el.summaryOverall.className = cls;
  setText(el.summaryOverall, overall);
}

/** สำรวจสถานะกล้องเป็นระยะ เพื่อให้เห็นว่ากล้องตัวไหนหลุดแม้ยังไม่ได้กดดูภาพ */
async function refreshCameraInfo() {
  if (!isCameraMode() || panels.size === 0) return;

  try {
    const res = await fetch(API.cameras, { cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();

    (data.cameras || []).forEach((cam) => {
      const panel = panels.get(cam.id);
      if (panel) panel.applyCameraInfo(cam);
    });

    // สถานะการติดตั้งอาจเปลี่ยน (เพิ่งใส่ HOST ให้กล้องตัวที่ 2) ต้องสรุปรวมใหม่ด้วย
    updateSummary();
  } catch (err) {
    // สำรวจไม่สำเร็จไม่ใช่เรื่องใหญ่ รอบหน้าค่อยลองใหม่
    // (ถ้า backend ล่มจริง การ์ดสถานะด้านล่างจะฟ้องอยู่แล้ว)
  }
}

el.btnCameras.addEventListener('click', () => {
  if (camerasRunning) {
    stopAllCameras();
  } else {
    startAllCameras();
  }
});

// อัปเดตตัวเลขอัตราการวาดของทุกจอทุกครึ่งวินาทีพอ
// ไม่ต้องอัปเดตทุกเฟรมให้เปลืองเปล่า
setInterval(() => {
  panels.forEach((panel) => panel.refreshRenderFps());
}, 500);

// ===========================================================================
// ส่วนที่ 3: โหมดเว็บแคมเดี่ยว (เฟส 2 - ใช้ตอนไม่มีกล้อง IP เลย)
// ===========================================================================

const camera = new CameraController(el.video);

/** ตัววาดกรอบของโหมดนี้ (โหมดกล้องหลายตัวมีของตัวเองอยู่ในแต่ละ StreamPanel) */
const webcamOverlay = new FaceOverlay({
  box: el.videoBox,
  canvas: el.overlay,
  getStreamSize: () => camera.streamSize,
  getSmoothing: () => config.tracking.smoothing,
  isMirrored: () => Boolean(config.stream.mirror),
});

/** สถานะการตรวจจับทั้งหมดรวมไว้ที่เดียว อ่านง่ายกว่าตัวแปรลอย ๆ กระจัดกระจาย */
const detection = {
  socket: null,          // WebSocket
  running: false,        // กำลังส่งเฟรมอยู่หรือไม่
  frameId: 0,            // เลขลำดับเฟรมที่จะส่งครั้งถัดไป
  awaitingResult: false, // ส่งไปแล้วยังไม่ได้ผลกลับ (ใช้ทำ backpressure)
  sendTimer: null,
  lastSentAt: 0,         // performance.now() ตอนส่งเฟรมล่าสุด
  watchdogTimer: null,   // กันค้างถ้า backend ไม่ตอบกลับมาเลย

  // สถิติ
  sentTimestamps: [],    // เวลาที่ส่งแต่ละเฟรม ใช้คำนวณ fps จริง
  droppedFrames: 0,
};

/** เติมรายชื่อกล้องลง dropdown */
async function refreshWebcamList(selectedId) {
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
async function startWebcam() {
  showAlert(el.cameraError, null);
  el.btnCamera.disabled = true;

  try {
    // ขอสิทธิ์ก่อนถ้ายังไม่เคยได้ เพื่อให้ enumerateDevices() คืน "ชื่อ" กล้องมาด้วย
    const before = await camera.listCameras();
    if (!before.hasLabels) {
      await camera.requestPermission();
    }

    const deviceId = el.cameraSelect.value || null;
    const size = await camera.start(deviceId);

    // เปิดได้แล้วค่อยเติมรายชื่อ ตอนนี้จะได้ชื่อจริงของกล้องมาแล้ว
    await refreshWebcamList(deviceId);

    el.videoIdle.hidden = true;
    el.btnCamera.textContent = 'ปิดกล้อง';
    el.btnCamera.classList.remove('btn--primary');
    setText(el.statResolution, size ? size.width + ' × ' + size.height : null);

    // ปรับภาพให้เป็นกระจกเงาถ้า config สั่ง (ธรรมชาติกว่าสำหรับเว็บแคม)
    el.video.classList.toggle('is-mirrored', Boolean(config.stream.mirror));

    // ตั้งสัดส่วนกล่องตามความละเอียดจริงของเว็บแคม (เช่น 1280x720 หรือ 640x480)
    // เพื่อให้เห็นภาพทั้งเฟรมโดยไม่มีแถบว่างเหลือทิ้งไว้เปล่า ๆ
    if (size) {
      el.videoBox.style.aspectRatio = size.width + ' / ' + size.height;
    }

    webcamOverlay.syncSize();
    webcamOverlay.start();
    startDetection();

  } catch (err) {
    showAlert(el.cameraError, err.message);
    camera.stop();
  } finally {
    el.btnCamera.disabled = false;
  }
}

/** ปิดกล้อง + หยุดส่งเฟรม */
function stopWebcam() {
  stopDetection();
  webcamOverlay.stop();
  camera.stop();

  el.videoIdle.hidden = false;
  el.btnCamera.textContent = 'เปิดกล้อง';
  el.btnCamera.classList.add('btn--primary');

  ['statResolution', 'statSentSize', 'statFps', 'statProcess', 'statFaces',
   'statTracks', 'statKnown', 'statRenderFps', 'statDropped',
  ].forEach((key) => setText(el[key], null));
}

el.btnCamera.addEventListener('click', () => {
  if (camera.isRunning) {
    stopWebcam();
  } else {
    startWebcam();
  }
});

// สลับกล้องระหว่างที่เปิดอยู่ = เปิดตัวใหม่ทันที
el.cameraSelect.addEventListener('change', () => {
  if (camera.isRunning) {
    stopWebcam();
    startWebcam();
  }
});

function startDetection() {
  if (detection.socket) return;

  const socket = new WebSocket(buildDetectSocketUrl());
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
    webcamOverlay.setFrameSize(data.source_size || null);

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
    webcamOverlay.setTracks(tracks);
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

// ขนาดที่แสดงเปลี่ยนได้ตลอด (ย่อ/ขยายหน้าต่าง) ต้องปรับขนาด canvas ตาม
// ไม่ต้องสั่งวาดเอง เพราะวงวาดเดินอยู่ทุกเฟรมของจออยู่แล้ว
const webcamResizeObserver = new ResizeObserver(() => {
  if (webcamOverlay.isRunning) webcamOverlay.syncSize();
});
webcamResizeObserver.observe(el.videoBox);

// อัปเดตตัวเลขอัตราการวาดของโหมดเว็บแคมเดี่ยว
setInterval(() => {
  if (!webcamOverlay.isRunning) return;
  setText(el.statRenderFps, webcamOverlay.renderFps + ' fps');
}, 500);

// ===========================================================================
// ส่วนที่ 4: เลือกโหมดให้ตรงกับที่ backend ใช้อยู่
// ===========================================================================
async function applySourceMode() {
  const cameraMode = isCameraMode();

  el.modeCameras.hidden = !cameraMode;
  el.modeWebcam.hidden = cameraMode;

  if (cameraMode) {
    await buildCameraPanels();
  } else {
    setText(el.videoIdleText, 'ยังไม่ได้เปิดกล้อง');
    setText(el.videoIdleHint, 'ต้องเปิดหน้านี้ผ่าน http://localhost:3000 เท่านั้น');
  }
}

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

    // ---- สถานะกล้องแต่ละตัวแยกกัน (เฟส 8) ----
    renderCameraHealth(src.cameras);

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

/**
 * แสดงสถานะกล้องทีละตัวในการ์ดสถานะ (ข้อ 7)
 *
 * ต้องบอกให้ครบว่า ชื่อ / ทิศทาง / เชื่อมต่อหรือหลุด / fps
 * เพื่อให้ดูออกจากหน้านี้ว่ากล้องตัวไหนมีปัญหา โดยไม่ต้องกดดูภาพ
 */
function renderCameraHealth(cameras) {
  if (!cameras || cameras.length === 0) {
    el.healthCamerasRow.hidden = true;
    return;
  }

  el.healthCamerasRow.hidden = false;
  el.healthCameras.replaceChildren();

  cameras.forEach((cam) => {
    const line = document.createElement('div');
    line.className = 'cam-line';

    // ใช้ตัวแปลสถานะชุดเดียวกับจอกล้อง (stream-panel.js) ข้อความจะได้ตรงกันทุกที่
    // เทา = ยังไม่ได้ติดตั้ง / เหลือง = กำลังเชื่อมต่อ / เขียว = ส่งภาพอยู่ / แดง = หลุด
    const view = describeCameraState(cam);

    const dot = document.createElement('span');
    dot.className = 'status__dot status__dot--' + view.dot;

    const text = document.createElement('span');
    const parts = [cam.name, '(' + cam.direction + ')', view.label];

    if (cam.state === 'not_installed') {
      // บอกตรง ๆ ว่าต้องไปตั้งตัวแปรไหน ไม่ต้องให้ผู้ใช้เดา
      parts.push('— ตั้ง ' + (cam.host_env || 'HOST') + ' ในไฟล์ .env เมื่อติดตั้งกล้องแล้ว');
    } else {
      // fps ที่อ่านได้จริงจากกล้องตัวนี้
      if (cam.read_fps !== undefined && cam.read_fps !== null) {
        parts.push(cam.read_fps + ' fps');
      }
      if (cam.reconnects) {
        parts.push('หลุดแล้วต่อใหม่ ' + cam.reconnects + ' ครั้ง');
      }
      if (!cam.alive && cam.connect_attempts) {
        parts.push('พยายามต่อ ' + cam.connect_attempts + ' ครั้ง');
      }
      if (!cam.alive && cam.error) {
        parts.push('— ' + cam.error);
      }
    }

    text.textContent = parts.join('  ');
    line.append(dot, text);
    el.healthCameras.appendChild(line);
  });
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

// ปิดกล้องและช่องส่งภาพให้เรียบร้อยเมื่อออกจากหน้า ไม่ปล่อยให้ไฟกล้องค้าง
window.addEventListener('beforeunload', () => {
  stopDetection();
  webcamOverlay.stop();
  camera.stop();
  panels.forEach((panel) => panel.stop());
});

// เริ่มทำงาน
(async function init() {
  await loadConfig();
  await applySourceMode();
  await refreshAll();
  setInterval(refreshAll, AUTO_REFRESH_MS);

  // โหมดกล้องหลายตัว: สำรวจสถานะกล้องเป็นระยะ
  // เพื่อให้เห็นทันทีเมื่อกล้องหลุดหรือกลับมา แม้ยังไม่ได้กดดูภาพ
  if (isCameraMode()) {
    setInterval(refreshCameraInfo, AUTO_REFRESH_MS);
  }
})();
