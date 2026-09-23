/* =============================================================================
   StreamPanel - จอแสดงภาพของกล้อง "หนึ่งตัว" (เฟส 8)

   หนึ่ง instance = หนึ่งกล้อง = หนึ่งจอบนหน้าเว็บ และมีของครบในตัวเองทั้งหมด:

       DOM ของตัวเอง        (หัวข้อ + จุดสถานะ + canvas + ชุดสถิติ + กล่องแจ้งเหตุ)
       WebSocket ของตัวเอง  (/ws/stream?camera=<id>)
       canvas ของตัวเอง     (ภาพ) + FaceOverlay ของตัวเอง (กรอบ)
       ชุดสถิติของตัวเอง    (ไม่ปนกับกล้องตัวอื่นแม้แต่ตัวเลขเดียว)

   ============================================================================
   ทำไมต้องแยก WebSocket ต่อกล้อง
   ============================================================================

   กล้องตัวหนึ่งหลุด ช่องของอีกตัวไม่ต้องรู้เรื่องเลย
   ไม่มีทางที่ปัญหาของกล้องหนึ่งจะไปหยุดภาพของอีกตัว (เกณฑ์ข้อ 4 ของเฟสนี้)
   และคิวรับภาพของสองกล้องแยกกันตามธรรมชาติ ตัวที่รับไม่ทันไม่ถ่วงอีกตัว

   แต่กฎเดิมยังอยู่ครบ: **ภาพกับผลตรวจของเฟรมนั้นมาด้วยกันในข้อความเดียว**
   จึงไม่มีทางที่กรอบจะไปวาดทับภาพคนละเฟรม

   ============================================================================
   ข้อห้ามสำคัญของไฟล์นี้
   ============================================================================

   **ห้ามใช้ document.getElementById หาของในจอนี้**
   ต้องหาจากใต้ this.root เท่านั้น เพราะสองจอมีโครงสร้างเหมือนกันเป๊ะ
   ถ้าใช้ id จะชนกันทันที แล้วจอหนึ่งจะไปเขียนตัวเลขทับอีกจอ
   ซึ่งเป็นบั๊ก "ตัวเลขปนกัน" ที่เกณฑ์ข้อ 5 ห้ามไว้
   ============================================================================= */

'use strict';

/* ป้ายหัวจอตามทิศทางที่กล้องรับผิดชอบ
   เอาจากค่า direction ที่ backend ส่งมา (ซึ่งมาจาก .env) ไม่ได้เดาจากชื่อกล้อง */
const DIRECTION_LABELS = {
  IN: 'ประตูทางเข้า (IN)',
  OUT: 'ประตูทางออก (OUT)',
};

/* คำอธิบายที่มาของภาพ ใช้บอกผู้ใช้ว่าจอนี้เป็นกล้อง IP จริงหรือเว็บแคมชั่วคราว */
const SOURCE_LABELS = {
  rtsp: 'กล้อง IP',
  browser: 'เว็บแคมของเครื่องนี้',
};

/* รายการสถิติของจอหนึ่งจอ - ประกาศไว้ที่เดียวแล้วสร้าง DOM จากรายการนี้
   เพิ่ม/ลด/สลับลำดับได้ที่นี่จุดเดียว ทั้งสองจอจะเหมือนกันเสมอโดยไม่ต้องแก้สองที่ */
const PANEL_STATS = [
  ['resolution', 'ความละเอียดกล้อง'],
  ['sentSize', 'ขนาดที่ส่งตรวจ'],
  ['fps', 'FPS ที่ได้รับ'],
  ['rates', 'อ่าน / ตรวจ / ส่ง'],
  ['latency', 'อายุเฟรม (latency)'],
  ['process', 'เวลาตรวจจับ'],
  ['faces', 'ใบหน้าที่เจอ'],
  ['tracks', 'กำลังติดตาม'],
  ['known', 'จดจำได้'],
  ['renderFps', 'อัตราวาดกรอบ'],
  ['reconnects', 'ต่อใหม่'],
];

// รอเท่าไรก่อนลองต่อ WebSocket ใหม่เมื่อการเชื่อมต่อหลุดแบบไม่ได้สั่ง
// เพิ่มเป็นเท่าตัวทุกครั้ง (หลักการเดียวกับฝั่ง backend ตอนต่อกล้องใหม่)
const WS_RECONNECT_MIN_MS = 1000;
const WS_RECONNECT_MAX_MS = 15000;


class StreamPanel {
  /**
   * @param {object} camera  ข้อมูลกล้องจาก /api/cameras {id, name, direction, source, ...}
   * @param {object} options
   * @param {function} options.getConfig  ()=>config ที่โหลดมาจาก /api/config
   * @param {function} [options.onUpdate] เรียกเมื่อมีผลตรวจชุดใหม่ (ใช้อัปเดตยอดรวมด้านบน)
   */
  constructor(camera, options) {
    this.camera = camera;
    this.cameraId = camera.id;
    this.getConfig = options.getConfig;
    this.onUpdate = options.onUpdate || function () {};

    this.socket = null;
    this.running = false;       // ผู้ใช้สั่งให้จอนี้ทำงานอยู่หรือไม่
    this.alive = false;         // กล้องตัวนี้ส่งภาพอยู่จริงหรือไม่
    this.ctx = null;            // context ของ canvas ที่วาดภาพ

    this.reconnectDelay = WS_RECONNECT_MIN_MS;
    this.reconnectTimer = null;

    // ใช้คำนวณ fps ที่ได้รับจริงของจอนี้
    this.frameTimestamps = [];

    // ผลตรวจล่าสุดของจอนี้ (ใช้ทำยอดรวมด้านบนหน้าเว็บ)
    this.lastDetectedCount = 0;
    this.lastKnownCount = 0;

    this._buildDom();

    // ตัววาดกรอบของจอนี้ ผูกกับ canvas และกล่องของจอนี้เท่านั้น
    this.overlay = new FaceOverlay({
      box: this.videoBox,
      canvas: this.overlayCanvas,
      // ในโหมดกล้องหลายตัว ภาพที่ได้รับคือเฟรมเต็มที่ใช้ตรวจพอดี
      // ขนาดจึงเท่ากับขนาดของ canvas ที่เพิ่งวาดภาพลงไป
      getStreamSize: () => {
        if (!this.streamCanvas.width) return null;
        return { width: this.streamCanvas.width, height: this.streamCanvas.height };
      },
      getSmoothing: () => this.getConfig().tracking.smoothing,
      // ไม่มีการพลิกกระจกในโหมดนี้เลย ทั้งกล้อง IP และเว็บแคมที่ป้อนเข้ามา
      // (backend บังคับ mirror=false ไว้แล้ว - ดูคำอธิบายใน config.py)
      isMirrored: () => false,
    });

    // ขนาดที่แสดงเปลี่ยนได้ตลอด (ย่อ/ขยายหน้าต่าง, สลับ 1 คอลัมน์เป็น 2)
    // ต้องปรับขนาด canvas ที่วาดกรอบตาม ไม่งั้นกรอบจะไม่ตรงกับใบหน้า
    // เฝ้าที่กล่องของจอนี้เท่านั้น เพราะสองจอมีขนาดไม่เท่ากันและเปลี่ยนไม่พร้อมกัน
    this.resizeObserver = new ResizeObserver(() => {
      if (this.overlay.isRunning) this.overlay.syncSize();
    });
    this.resizeObserver.observe(this.videoBox);
  }

  // ==========================================================================
  // สร้าง DOM ของจอนี้
  // ==========================================================================
  _buildDom() {
    const cam = this.camera;

    const root = document.createElement('article');
    root.className = 'cam';
    // เก็บชื่อกล้องไว้ใน data attribute เพื่อให้เปิด DevTools แล้วดูออกว่าจอไหนเป็นตัวไหน
    root.dataset.camera = cam.id;

    // ---------- หัวจอ ----------
    const head = document.createElement('header');
    head.className = 'cam__head';

    const titleWrap = document.createElement('div');
    titleWrap.className = 'cam__titles';

    // ป้ายทิศทางเป็นตัวหลัก เพราะผู้ใช้ต้องรู้ทันทีว่าจอไหนคือขาเข้า/ขาออก
    const title = document.createElement('h3');
    title.className = 'cam__title';
    title.textContent = DIRECTION_LABELS[cam.direction] || ('ทิศทาง ' + cam.direction);

    // บรรทัดรองบอกชื่อกล้องที่ตั้งใน .env และที่มาของภาพ
    const subtitle = document.createElement('p');
    subtitle.className = 'cam__subtitle';
    subtitle.textContent = cam.name + ' · ' + (SOURCE_LABELS[cam.source] || cam.source);

    titleWrap.append(title, subtitle);

    // จุดสถานะของกล้องตัวนี้ (แยกจากสถานะรวมของระบบ)
    const state = document.createElement('div');
    state.className = 'cam__state';
    this.stateDot = document.createElement('span');
    this.stateDot.className = 'status__dot status__dot--pending';
    this.stateText = document.createElement('span');
    this.stateText.className = 'cam__state-text';
    this.stateText.textContent = 'ยังไม่เริ่ม';
    state.append(this.stateDot, this.stateText);

    head.append(titleWrap, state);

    // ---------- แถบควบคุมเฉพาะจอนี้ (กล้องแบบเว็บแคมใช้เลือกอุปกรณ์) ----------
    this.controls = document.createElement('div');
    this.controls.className = 'cam__controls';
    this.controls.hidden = true;

    // ---------- กล่องภาพ ----------
    // โครงเหมือนโหมดเว็บแคมเดี่ยว: canvas ภาพ + canvas กรอบวางทับกันพอดีเป๊ะ
    this.videoBox = document.createElement('div');
    this.videoBox.className = 'video-box cam__video';

    this.streamCanvas = document.createElement('canvas');
    this.streamCanvas.className = 'cam__stream';

    this.overlayCanvas = document.createElement('canvas');
    this.overlayCanvas.className = 'cam__overlay';

    // ข้อความตอนยังไม่เริ่มรับภาพ
    this.idle = document.createElement('div');
    this.idle.className = 'video-box__idle';
    const idleText = document.createElement('p');
    idleText.textContent = 'ยังไม่ได้เริ่มดูภาพสด';
    this.idle.appendChild(idleText);

    // กรอบเทาตอนกล้องหลุด พร้อมข้อความบอกสาเหตุ (เกณฑ์ข้อ 10)
    // เป็นของจอนี้เท่านั้น จึงไม่ทำให้ภาพของกล้องอีกตัวหายไปด้วย
    this.offline = document.createElement('div');
    this.offline.className = 'cam__offline';
    this.offline.hidden = true;
    this.offlineTitle = document.createElement('p');
    this.offlineTitle.className = 'cam__offline-title';
    this.offlineReason = document.createElement('p');
    this.offlineReason.className = 'cam__offline-reason';
    this.offlineHint = document.createElement('p');
    this.offlineHint.className = 'cam__offline-hint';
    this.offline.append(this.offlineTitle, this.offlineReason, this.offlineHint);

    this.videoBox.append(this.streamCanvas, this.overlayCanvas, this.idle, this.offline);

    // ---------- ชุดสถิติของจอนี้ ----------
    const stats = document.createElement('div');
    stats.className = 'stats stats--cam';

    this.stat = {};
    PANEL_STATS.forEach((pair) => {
      const key = pair[0];
      const label = pair[1];

      const cell = document.createElement('div');
      cell.className = 'stat';

      const labelEl = document.createElement('span');
      labelEl.className = 'stat__label';
      labelEl.textContent = label;

      const valueEl = document.createElement('span');
      valueEl.className = 'stat__value';
      valueEl.textContent = '—';

      cell.append(labelEl, valueEl);
      stats.appendChild(cell);

      // เก็บ reference ไว้ใน object ของ instance นี้ ไม่ใช้ id เลย
      this.stat[key] = valueEl;
    });

    // ---------- กล่องแจ้งเหตุของจอนี้ ----------
    this.alert = document.createElement('div');
    this.alert.className = 'alert alert--error';
    this.alert.hidden = true;

    // ลำดับสำคัญ: กล่องภาพต้องมาต่อจากหัวจอทันที แล้วค่อยเป็นแถบควบคุม
    //
    // ถ้าเอาแถบควบคุมไว้เหนือภาพ จอที่มีแถบ (กล้องแบบเว็บแคม) จะถูกดันลงมา
    // ทำให้ภาพสองจอไม่อยู่ระดับเดียวกัน ซึ่งดูแล้วรู้สึกว่าหน้าเว็บเพี้ยน
    // พอย้ายมาไว้ใต้ภาพ ภาพของทุกจอจะเริ่มที่ความสูงเดียวกันเสมอ
    // ไม่ว่าจอไหนจะมีแถบควบคุมหรือไม่
    root.append(head, this.videoBox, this.controls, stats, this.alert);
    this.root = root;
  }

  /** เอาจอนี้ไปแปะในหน้าเว็บ */
  mount(parent) {
    parent.appendChild(this.root);
  }

  // ==========================================================================
  // เริ่ม / หยุด
  // ==========================================================================
  start() {
    if (this.running) return;
    this.running = true;
    this.frameTimestamps = [];
    this.ctx = this.streamCanvas.getContext('2d', { alpha: false });
    this.reconnectDelay = WS_RECONNECT_MIN_MS;
    this.overlay.start();
    this._setState('pending', 'กำลังเชื่อมต่อ…');
    this._openSocket();
  }

  stop() {
    this.running = false;

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    if (this.socket) {
      // ปิดแบบ "สั่งปิด" (code 1000) เพื่อให้ตัวจัดการ close รู้ว่าไม่ต้องต่อใหม่
      const socket = this.socket;
      this.socket = null;
      socket.close(1000, 'ผู้ใช้หยุดดูภาพ');
    }

    this.overlay.stop();
    this.alive = false;

    this.idle.hidden = false;
    this.offline.hidden = true;
    this._setState('pending', 'ปิดอยู่');
    this._resetStats();
    this.showAlert(null);
  }

  /** เก็บกวาดให้หมดก่อนทิ้งจอนี้ (ใช้ตอนรายชื่อกล้องเปลี่ยน) */
  destroy() {
    this.stop();
    this.resizeObserver.disconnect();
    this.root.remove();
  }

  // ==========================================================================
  // WebSocket
  // ==========================================================================
  _openSocket() {
    if (!this.running) return;

    const url = buildWebSocketUrl('/ws/stream?camera=' + encodeURIComponent(this.cameraId));
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.addEventListener('open', () => {
      // ต่อติดแล้วรีเซ็ตเวลารอกลับไปค่าเริ่มต้น (เหมือน backoff ฝั่ง backend)
      this.reconnectDelay = WS_RECONNECT_MIN_MS;
      this.idle.hidden = true;

      // ล้างข้อความ "การเชื่อมต่อหลุด" ของรอบก่อนทิ้ง ไม่งั้นจะค้างอยู่ตลอด
      // ทั้งที่ต่อกลับมาได้แล้ว ซึ่งทำให้ผู้ใช้เข้าใจผิดว่ายังมีปัญหาอยู่
      // (ถ้ากล้องยังหลุดจริง กรอบเทาในกล่องภาพจะบอกอยู่แล้ว เป็นคนละเรื่องกัน)
      this.showAlert(null);
      this._setState('pending', 'รอภาพเฟรมแรก…');
    });

    socket.addEventListener('message', (event) => {
      // ข้อความที่เป็นข้อความล้วน = แจ้งสถานะหรือ error
      if (typeof event.data === 'string') {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (err) {
          return;
        }

        if (data.type === 'error') {
          this.showAlert('backend แจ้งข้อผิดพลาด: ' + data.message);
          this._setState('error', 'ใช้งานไม่ได้');
          return;
        }

        if (data.type === 'camera_status') {
          this._handleCameraStatus(data);
        }
        return;
      }

      // ข้อความ binary = หนึ่งเฟรมพร้อมผลตรวจจับของเฟรมนั้น
      this._handleFrame(event.data);
    });

    socket.addEventListener('close', (event) => {
      // ไม่ใช่ socket ตัวปัจจุบันแล้ว (เกิดจากการสั่งปิด) ไม่ต้องทำอะไร
      if (this.socket !== socket) return;
      this.socket = null;

      if (!this.running) return;

      // การเชื่อมต่อหลุดแบบไม่ได้สั่ง เช่น backend รีสตาร์ท หรือ nginx ตัด
      // ต้องต่อใหม่ให้เองเรื่อย ๆ ผู้ใช้ไม่ควรต้องกดรีเฟรชหน้าเว็บ
      this._setState('error', 'การเชื่อมต่อหลุด');
      this.showAlert(
        'การเชื่อมต่อกับ backend ของจอนี้หลุด (code ' + event.code + ') — ' +
        'จะลองต่อใหม่ในอีก ' + Math.round(this.reconnectDelay / 1000) + ' วินาที\n' +
        'ถ้าไม่กลับมา ตรวจ log ด้วย docker compose logs backend'
      );

      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this._openSocket();
      }, this.reconnectDelay);

      this.reconnectDelay = Math.min(this.reconnectDelay * 2, WS_RECONNECT_MAX_MS);
    });
  }

  // ==========================================================================
  // กล้องหลุด / กลับมา
  // ==========================================================================
  _handleCameraStatus(data) {
    const status = data.status || {};

    // อัปเดตตัวนับต่อใหม่ให้เห็นทันทีแม้ยังไม่มีภาพ
    if (status.reconnects !== undefined) {
      this.stat.reconnects.textContent = status.reconnects + ' ครั้ง';
    }

    if (data.alive) {
      this.alive = true;
      this.offline.hidden = true;
      this.showAlert(null);
      this._setState('ok', 'รับภาพสดอยู่');
      return;
    }

    // ---- กล้องตัวนี้หลุด ----
    // แสดงกรอบเทาทับ "จอนี้เท่านั้น" พร้อมบอกสาเหตุ
    // จอของกล้องอีกตัวไม่ถูกแตะต้องเลย เพราะทุก element อยู่ใต้ this.root
    this.alive = false;
    this._setState('error', 'ไม่ส่งภาพ');

    // ล้างกรอบทิ้ง เพราะตำแหน่งที่ค้างอยู่ไม่ตรงกับความจริงแล้ว
    // ถ้าปล่อยไว้จะเห็นกรอบลอยนิ่งทับภาพค้าง ซึ่งทำให้เข้าใจผิดว่ายังตรวจได้อยู่
    this.overlay.setTracks([]);

    const isWebcam = this.camera.source === 'browser';

    this.offlineTitle.textContent = 'กล้องนี้ไม่ส่งภาพ';
    this.offlineReason.textContent = status.error || 'ไม่ทราบสาเหตุ';
    this.offlineHint.textContent = isWebcam
      ? 'กล้องนี้ใช้เว็บแคมของเครื่องที่เปิดหน้าเว็บ — ต้องกดอนุญาตใช้กล้องและเปิดหน้านี้ค้างไว้'
      : 'ต่อใหม่ไปแล้ว ' + (status.reconnects || 0) + ' ครั้ง — ' +
        'ระบบจะพยายามต่อใหม่ให้เองเรื่อย ๆ เสียบปลั๊กกลับแล้วภาพจะกลับมาเอง';
    this.offline.hidden = false;

    // ตัวเลขที่ไม่มีความหมายแล้วต้องล้าง ไม่ใช่ปล่อยค้างค่าเดิมไว้ให้เข้าใจผิด
    ['fps', 'rates', 'latency', 'process', 'faces', 'tracks', 'known'].forEach((key) => {
      this.stat[key].textContent = '—';
    });
    this.lastDetectedCount = 0;
    this.lastKnownCount = 0;
    this.onUpdate();
  }

  // ==========================================================================
  // หนึ่งเฟรม
  // ==========================================================================
  async _handleFrame(buffer) {
    let decoded;
    try {
      decoded = decodeStreamMessage(buffer);
    } catch (err) {
      this.showAlert('อ่านข้อมูลเฟรมไม่สำเร็จ: ' + err.message);
      return;
    }

    const meta = decoded.meta;

    // ---- ตาข่ายกันภาพไปโผล่ผิดจอ ----
    // ทุกข้อความจาก backend ระบุ camera มาด้วย ถ้าไม่ตรงกับจอนี้ต้องทิ้งทันที
    // ปกติจะไม่เกิดเพราะแยก WebSocket ต่อกล้องอยู่แล้ว แต่ตรวจไว้เพราะบั๊กชนิดนี้
    // ("กรอบขึ้นสลับจอ") หาสาเหตุยากมากถ้าไม่มีอะไรดักไว้เลย
    if (meta.camera && meta.camera !== this.cameraId) {
      this.showAlert(
        'ได้รับภาพของกล้อง "' + meta.camera + '" มาที่จอของ "' + this.cameraId + '" — ' +
        'ทิ้งเฟรมนี้แล้ว (ถ้าเห็นข้อความนี้บ่อย แจ้งผู้ดูแลระบบ)'
      );
      return;
    }

    // ---- วาดภาพลง canvas ของจอนี้ ----
    // ใช้ createImageBitmap เพราะถอดรหัส JPEG นอก main thread ได้
    // เร็วกว่าการสร้าง <img> แล้วรอ onload มาก และไม่ต้องคอย revoke object URL
    let bitmap;
    try {
      bitmap = await createImageBitmap(new Blob([decoded.jpeg], { type: 'image/jpeg' }));
    } catch (err) {
      return; // เฟรมเสียหนึ่งเฟรมไม่ใช่เรื่องใหญ่ รอเฟรมถัดไป
    }

    // ภาพกลับมาแล้ว = กล้องตัวนี้ยังดีอยู่
    if (!this.alive) {
      this.alive = true;
      this.offline.hidden = true;
      this.showAlert(null);
      this._setState('ok', 'รับภาพสดอยู่');
    }
    this.idle.hidden = true;

    if (this.streamCanvas.width !== bitmap.width || this.streamCanvas.height !== bitmap.height) {
      this.streamCanvas.width = bitmap.width;
      this.streamCanvas.height = bitmap.height;

      // ตั้งสัดส่วนกล่องตามขนาดจริงที่กล้องตัวนี้ส่งมา ไม่ได้ hardcode ไว้
      // สำคัญมากในเฟสนี้เพราะสองกล้องมีสัดส่วนไม่เท่ากันได้
      // (Tapo /stream2 เป็น 640x360 ส่วนเว็บแคมมักเป็น 4:3)
      this.videoBox.style.aspectRatio = bitmap.width + ' / ' + bitmap.height;
      this.overlay.syncSize();
      this.stat.resolution.textContent = bitmap.width + ' × ' + bitmap.height;
    }

    this.ctx.drawImage(bitmap, 0, 0);
    bitmap.close();

    // ---- อัปเดตตัวเลขที่เปลี่ยนทุกเฟรม ----
    this.overlay.setFrameSize(meta.source_size || null);

    this.stat.sentSize.textContent = meta.source_size
      ? meta.source_size[0] + ' × ' + meta.source_size[1]
      : '—';
    this.stat.latency.textContent = meta.frame_age_ms + ' ms';

    // อัตราสามค่าที่วัดได้จริงของกล้องตัวนี้ (เฟส 7) - ดูออกว่าขั้นไหนเป็นคอขวด
    if (meta.fps) {
      this.stat.rates.textContent =
        meta.fps.capture + ' / ' + meta.fps.detect + ' / ' + meta.fps.stream;
    }

    this._recordFrame();

    // ---- อัปเดตกรอบ "เฉพาะตอนได้ผลตรวจชุดใหม่" (หัวใจของเฟส 7) ----
    //
    // ภาพมาที่ STREAM_FPS (เช่น 15) แต่ผลตรวจมาที่ DETECT_FPS (เช่น 5)
    // เฟรมส่วนใหญ่จึงแนบผลชุดเดิมมาด้วย (is_fresh=false)
    // ช่วงที่ไม่มีผลใหม่ วงวาดของจอนี้จะ interpolate ต่อเอง (กลไกจากเฟส 3)
    if (meta.is_fresh) {
      const tracks = meta.tracks || [];
      const knownCount = tracks.filter((t) => t.identity_state === 'recognized').length;

      this.stat.process.textContent =
        (meta.detect_ms + meta.identify_ms).toFixed(1) +
        ' ms (ตรวจ ' + meta.detect_ms + ' + จดจำ ' + meta.identify_ms + ')';
      this.stat.faces.textContent = meta.detected_count + ' คน';
      this.stat.tracks.textContent = tracks.length + ' track';
      this.stat.known.textContent = knownCount + ' / ' + tracks.length + ' คน';

      this.overlay.setTracks(tracks);

      // เก็บไว้ทำยอดรวมด้านบนหน้าเว็บ (ข้อ 11)
      this.lastDetectedCount = meta.detected_count || 0;
      this.lastKnownCount = knownCount;
      this.onUpdate();
    }

    // ผลตรวจเก่าเกินไป: ค่อย ๆ จางกรอบลง เพื่อสื่อว่ากำลังเดาตำแหน่งอยู่
    this.overlay.setStale(Boolean(meta.is_stale));
  }

  /** นับ fps ของภาพที่จอนี้ได้รับจริง */
  _recordFrame() {
    const now = performance.now();
    this.frameTimestamps.push(now);

    while (this.frameTimestamps.length && now - this.frameTimestamps[0] > 2000) {
      this.frameTimestamps.shift();
    }

    const span = now - this.frameTimestamps[0];
    if (span > 500 && this.frameTimestamps.length > 1) {
      const fps = ((this.frameTimestamps.length - 1) * 1000) / span;
      this.stat.fps.textContent = fps.toFixed(1) + ' fps';
    }
  }

  // ==========================================================================
  // สถานะจากการสำรวจเป็นระยะ (/api/cameras) - ใช้ตอนยังไม่ได้กดดูภาพ
  // ==========================================================================
  applyCameraInfo(info) {
    this.camera = Object.assign({}, this.camera, info);

    if (info.reconnects !== undefined) {
      this.stat.reconnects.textContent = info.reconnects + ' ครั้ง';
    }

    // ระหว่างที่ยังไม่ได้ดูภาพ ก็ควรเห็นว่ากล้องตัวไหนพร้อมหรือหลุดอยู่
    if (!this.running) {
      if (info.alive) {
        this._setState('ok', 'พร้อม (ยังไม่ได้ดู)');
      } else if (info.connected) {
        this._setState('pending', 'ต่อได้แต่ยังไม่มีภาพ');
      } else {
        this._setState('error', 'ยังต่อไม่ได้');
      }
    }
  }

  // ==========================================================================
  // ตัวช่วยเล็ก ๆ
  // ==========================================================================
  _setState(state, text) {
    this.stateDot.className = 'status__dot status__dot--' + state;
    this.stateText.textContent = text;
  }

  _resetStats() {
    Object.keys(this.stat).forEach((key) => {
      this.stat[key].textContent = '—';
    });
    this.lastDetectedCount = 0;
    this.lastKnownCount = 0;
  }

  /** ใส่ข้อความแบบ textContent เสมอ (ไม่ใช้ innerHTML) กัน XSS */
  showAlert(message) {
    if (message) {
      this.alert.textContent = message;
      this.alert.hidden = false;
    } else {
      this.alert.textContent = '';
      this.alert.hidden = true;
    }
  }

  /** อัปเดตตัวเลขอัตราการวาดกรอบ (ตัวเรียกเป็นคนกำหนดจังหวะ) */
  refreshRenderFps() {
    if (!this.overlay.isRunning) return;
    this.stat.renderFps.textContent = this.overlay.renderFps + ' fps';
  }
}


/** ประกอบ URL ของ WebSocket จาก origin ปัจจุบัน (http -> ws, https -> wss) */
function buildWebSocketUrl(path) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return protocol + '//' + window.location.host + path;
}

/** แกะข้อความ binary ที่ backend ส่งมา: [4 ไบต์ความยาว JSON][JSON][JPEG] */
function decodeStreamMessage(buffer) {
  const view = new DataView(buffer);
  const jsonLength = view.getUint32(0, true);   // true = little-endian

  const jsonBytes = new Uint8Array(buffer, 4, jsonLength);
  const meta = JSON.parse(new TextDecoder('utf-8').decode(jsonBytes));

  const jpegBytes = new Uint8Array(buffer, 4 + jsonLength);

  return { meta: meta, jpeg: jpegBytes };
}
