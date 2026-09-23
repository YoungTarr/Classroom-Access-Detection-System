/* =============================================================================
   WebcamFeeder - ส่งภาพเว็บแคมของเครื่องนี้ขึ้นไปเป็น "กล้องตัวหนึ่ง" (เฟส 8)

   ใช้กับกล้องที่ตั้ง CAMERA_<ID>_SOURCE=browser ในไฟล์ .env

   ============================================================================
   ทำไมต้องส่งขึ้นไปแล้วรับกลับลงมา แทนที่จะวาดกรอบเองในเครื่อง
   ============================================================================

       เบราว์เซอร์ --(/ws/feed)--> backend --> AI --(/ws/stream)--> เบราว์เซอร์
        (ไฟล์นี้)                                                  (StreamPanel)

   1. ภาพที่ผู้ใช้เห็น เป็นเฟรมเดียวกับที่ AI ตรวจจริงเสมอ
      ไม่มีทางที่กรอบจะไปวาดทับภาพคนละเฟรมกัน
   2. หน้าเว็บมองกล้องสองตัวเหมือนกันเป๊ะ ใช้ StreamPanel ชุดเดียวกัน
      ไม่ต้องมี if แยกชนิดกล้องกระจายไปทั่วโค้ดแสดงผล
   3. backend เห็นเว็บแคมเป็นกล้องจริง จึงได้สถิติรายกล้อง /api/health
      คิวตรวจจับร่วมกับกล้อง IP และทิศทาง IN/OUT ครบเหมือนกัน
   4. พอกล้อง Tapo ตัวที่สองมาถึง เปลี่ยน .env บรรทัดเดียว ไฟล์นี้เลิกทำงานเอง
      โดยที่ส่วนแสดงผลไม่ต้องแก้อะไรเลย

   ============================================================================
   จังหวะการส่ง (backpressure)
   ============================================================================

   ส่งเฟรมถัดไป "เมื่อได้ ack ของเฟรมก่อนหน้ากลับมาแล้ว" เท่านั้น
   ไม่ใช่ยิงตามนาฬิกาตายตัว

   ถ้ายิงตามนาฬิกาโดยไม่รอ ack เฟรมจะไปกองในบัฟเฟอร์ของ WebSocket
   เมื่อเครือข่ายหรือ backend ช้ากว่าที่เราส่ง แล้วภาพจะช้ากว่าความจริง
   มากขึ้นเรื่อย ๆ ไม่มีที่สิ้นสุด ซึ่งเป็นอาการเดียวกับที่ RTSPFrameSource
   ฝั่ง backend ป้องกันไว้ด้วยการเก็บแค่เฟรมล่าสุด
   ============================================================================= */

'use strict';

// ครบเวลานี้แล้วยังไม่ได้ ack ถือว่าเฟรมนั้นหายไป แล้วเดินจังหวะต่อ
// ต้องมี ไม่งั้นวงจรจะหยุดนิ่งถาวรถ้า ack หายแม้แต่ครั้งเดียว
const FEED_ACK_TIMEOUT_MS = 5000;

// รอเท่าไรก่อนลองต่อ WebSocket ใหม่ (เพิ่มเป็นเท่าตัวทุกครั้ง)
const FEED_RECONNECT_MIN_MS = 1000;
const FEED_RECONNECT_MAX_MS = 15000;


class WebcamFeeder {
  /**
   * @param {string} cameraId  ชื่อกล้องปลายทางใน .env เช่น "door_out"
   * @param {object} options
   * @param {function} options.getConfig  ()=>config จาก /api/config
   * @param {function} [options.onStatus] (state, message) รายงานสถานะให้หน้าเว็บแสดง
   */
  constructor(cameraId, options) {
    this.cameraId = cameraId;
    this.getConfig = options.getConfig;
    this.onStatus = options.onStatus || function () {};

    // <video> ที่ไม่ได้แสดงบนหน้าเว็บ มีไว้ให้ CameraController วาดภาพจากมัน
    // ที่ผู้ใช้เห็นคือภาพที่วนกลับมาจาก backend ไม่ใช่ element นี้
    // (ถ้าแสดง element นี้ด้วย จะเห็นภาพสองชุดที่ไม่ตรงเฟรมกัน ซึ่งสับสน)
    this.video = document.createElement('video');
    this.video.playsInline = true;
    this.video.muted = true;
    this.video.hidden = true;
    document.body.appendChild(this.video);

    this.camera = new CameraController(this.video);

    this.socket = null;
    this.running = false;
    this.deviceId = null;

    this.frameId = 0;
    this.awaitingAck = false;
    this.sendTimer = null;
    this.ackTimer = null;
    this.lastSentAt = 0;

    this.reconnectDelay = FEED_RECONNECT_MIN_MS;
    this.reconnectTimer = null;

    // นับเฟรมที่ต้องทิ้งเพราะยังไม่ได้ ack ของเฟรมก่อน
    this.droppedFrames = 0;
  }

  get isRunning() {
    return this.running;
  }

  // ==========================================================================
  // รายชื่อเว็บแคมในเครื่อง
  // ==========================================================================
  async listDevices() {
    // ขอสิทธิ์ก่อนถ้ายังไม่เคยได้ เพื่อให้ enumerateDevices() คืน "ชื่อ" กล้องมาด้วย
    // ถ้าไม่ทำขั้นนี้ รายการจะมีแต่ "กล้องตัวที่ 1/2/3" ซึ่งผู้ใช้เลือกไม่ถูก
    let result = await this.camera.listCameras();
    if (!result.hasLabels && result.cameras.length > 0) {
      try {
        await this.camera.requestPermission();
        result = await this.camera.listCameras();
      } catch (err) {
        // ขอสิทธิ์ไม่ผ่านก็ยังคืนรายการเท่าที่มี ไม่ทำให้ทั้งหน้าพัง
        // ส่วนข้อความบอกสาเหตุจะไปโผล่ตอนกด start() จริง
      }
    }
    return result;
  }

  // ==========================================================================
  // เริ่ม / หยุด
  // ==========================================================================

  /**
   * เปิดเว็บแคมแล้วเริ่มส่งภาพขึ้นไป
   * @param {string|null} deviceId  รหัสกล้องที่เลือก หรือ null = ให้เบราว์เซอร์เลือกเอง
   */
  async start(deviceId) {
    if (this.running) return;

    this.deviceId = deviceId || null;
    this.onStatus('pending', 'กำลังเปิดเว็บแคม…');

    try {
      await this.camera.start(this.deviceId);
    } catch (err) {
      // ล้มตั้งแต่เปิดกล้อง ต้องบอกสาเหตุให้ชัด ไม่ใช่ปล่อยให้จอว่างเงียบ ๆ
      this.onStatus('error', err.message);
      throw err;
    }

    this.running = true;
    this.frameId = 0;
    this.droppedFrames = 0;
    this.reconnectDelay = FEED_RECONNECT_MIN_MS;
    this._openSocket();
  }

  stop() {
    this.running = false;
    this._clearTimers();

    if (this.socket) {
      const socket = this.socket;
      this.socket = null;
      socket.close(1000, 'หยุดส่งภาพเว็บแคม');
    }

    this.camera.stop();
    this.onStatus('pending', 'ปิดอยู่');
  }

  /** สลับไปใช้เว็บแคมตัวอื่นระหว่างที่กำลังส่งอยู่ */
  async switchDevice(deviceId) {
    const wasRunning = this.running;
    this.stop();
    if (wasRunning) {
      await this.start(deviceId);
    } else {
      this.deviceId = deviceId || null;
    }
  }

  _clearTimers() {
    [this.sendTimer, this.ackTimer, this.reconnectTimer].forEach((t) => {
      if (t) clearTimeout(t);
    });
    this.sendTimer = null;
    this.ackTimer = null;
    this.reconnectTimer = null;
    this.awaitingAck = false;
  }

  // ==========================================================================
  // WebSocket ที่ใช้ป้อนภาพ
  // ==========================================================================
  _openSocket() {
    if (!this.running) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = protocol + '//' + window.location.host +
                '/ws/feed?camera=' + encodeURIComponent(this.cameraId);

    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.addEventListener('message', (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }

      if (data.type === 'error') {
        // error ระดับระบบ (เช่นตั้ง .env ผิด) ต้องแสดงให้เห็น ไม่กลืนเงียบ
        this.onStatus('error', 'backend แจ้งข้อผิดพลาด: ' + data.message);
        this._onAckSettled();
        return;
      }

      if (data.type === 'ready') {
        this.reconnectDelay = FEED_RECONNECT_MIN_MS;
        this.onStatus('ok', 'กำลังส่งภาพเว็บแคมขึ้นไป');
        this._scheduleNextFrame(0);
        return;
      }

      if (data.type === 'ack') {
        this._onAckSettled();
      }
    });

    socket.addEventListener('close', (event) => {
      if (this.socket !== socket) return;   // ถูกสั่งปิดไปแล้ว
      this.socket = null;
      this._clearTimers();

      if (!this.running) return;

      this.onStatus('error',
        'ช่องส่งภาพเว็บแคมหลุด (code ' + event.code + ') — ' +
        'จะลองต่อใหม่ในอีก ' + Math.round(this.reconnectDelay / 1000) + ' วินาที');

      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this._openSocket();
      }, this.reconnectDelay);

      this.reconnectDelay = Math.min(this.reconnectDelay * 2, FEED_RECONNECT_MAX_MS);
    });
  }

  // ==========================================================================
  // จังหวะการส่ง
  // ==========================================================================

  /** ช่วงเวลาขั้นต่ำระหว่างเฟรม ตามอัตราที่ config กำหนด */
  _frameIntervalMs() {
    return 1000 / Math.max(1, this.getConfig().stream.send_fps);
  }

  _scheduleNextFrame(delayMs) {
    if (!this.running) return;
    if (this.sendTimer) clearTimeout(this.sendTimer);
    this.sendTimer = setTimeout(() => this._sendFrame(), Math.max(0, delayMs));
  }

  /** เรียกเมื่อได้ ack (หรือ error) กลับมา เพื่อเดินจังหวะเฟรมถัดไปต่อ */
  _onAckSettled() {
    this.awaitingAck = false;

    if (this.ackTimer) {
      clearTimeout(this.ackTimer);
      this.ackTimer = null;
    }

    // หักเวลาที่ใช้ไปแล้วออก จะได้ไม่ช้ากว่าอัตราที่ตั้งไว้โดยไม่จำเป็น
    const elapsed = performance.now() - this.lastSentAt;
    this._scheduleNextFrame(this._frameIntervalMs() - elapsed);
  }

  async _sendFrame() {
    if (!this.running || !this.socket) return;
    if (this.socket.readyState !== WebSocket.OPEN) return;

    // ตาข่ายกันพลาด: ปกติจะไม่เข้าเงื่อนไขนี้ เพราะส่งต่อเมื่อได้ ack แล้วเท่านั้น
    if (this.awaitingAck) {
      this.droppedFrames += 1;
      this._scheduleNextFrame(this._frameIntervalMs());
      return;
    }

    try {
      const cfg = this.getConfig();
      const captured = await this.camera.captureJpeg(
        cfg.stream.target_width,
        cfg.stream.jpeg_quality
      );

      if (!captured) {
        // กล้องยังไม่พร้อมส่งภาพ รอรอบถัดไป
        this._scheduleNextFrame(this._frameIntervalMs());
        return;
      }

      const jpegBuffer = await captured.blob.arrayBuffer();

      // ประกอบข้อความ: [frame_id 4 ไบต์ little-endian][ข้อมูล JPEG]
      // รูปแบบเดียวกับ /ws/detect ของเฟส 2 เพื่อไม่ต้องมีสองมาตรฐานในโปรเจกต์เดียว
      const frameId = this.frameId++;
      const message = new Uint8Array(4 + jpegBuffer.byteLength);
      new DataView(message.buffer).setUint32(0, frameId, true);
      message.set(new Uint8Array(jpegBuffer), 4);

      this.socket.send(message);
      this.awaitingAck = true;
      this.lastSentAt = performance.now();

      // ไม่ได้ ack เลยภายในเวลาที่กำหนด = ถือว่าเฟรมนั้นหาย แล้วเดินต่อ
      // ห้ามปล่อยให้วงจรหยุดนิ่งถาวร ไม่งั้นจอจะค้างโดยไม่มีอะไรบอกสาเหตุ
      this.ackTimer = setTimeout(() => {
        if (!this.awaitingAck) return;
        this.droppedFrames += 1;
        this.onStatus('error', 'backend ไม่ตอบรับภายใน 5 วินาที — กำลังลองเฟรมถัดไป');
        this._onAckSettled();
      }, FEED_ACK_TIMEOUT_MS);

    } catch (err) {
      this.onStatus('error', 'ส่งภาพเว็บแคมไม่สำเร็จ: ' + err.message);
      this._scheduleNextFrame(this._frameIntervalMs());
    }
  }
}
