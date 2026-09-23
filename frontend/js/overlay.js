/* =============================================================================
   FaceOverlay - ตัววาดกรอบใบหน้าทับภาพหนึ่งภาพ

   เดิมโค้ดส่วนนี้อยู่ใน app.js และผูกกับ "ภาพเดียวบนหน้าเว็บ" ผ่านตัวแปรรวม
   พอเฟส 8 ต้องแสดงสองจอพร้อมกัน จึงต้องแยกออกมาเป็นคลาสที่สร้างกี่ตัวก็ได้
   แต่ละตัวมีของครบในตัวเอง: กรอบที่กำลังไหล, วงวาด, มาตรวัดอัตราการวาด

   **ห้ามมีตัวแปรระดับไฟล์ที่เก็บสถานะของภาพใด ๆ ในไฟล์นี้**
   ถ้ามีเมื่อไร สองจอจะใช้สถานะร่วมกัน แล้วกรอบของกล้องหนึ่งจะไปโผล่อีกจอทันที
   ซึ่งตรงกับข้อห้ามข้อ 3 ของเฟสนี้พอดี
   ("เดินผ่านกล้องไหน กรอบและชื่อขึ้นถูกจอนั้น ไม่สลับจอกัน")

   ใช้กับทั้งสองโหมด:
     - โหมดเว็บแคมเดี่ยว : วาดทับ <video> ที่เล่นเว็บแคมในเครื่อง
     - โหมดกล้องหลายตัว  : วาดทับ <canvas> ที่รับภาพจาก backend (จอละตัว)
   ============================================================================= */

'use strict';

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

// ความจางของกรอบ "ทั้งหมด" เมื่อผลตรวจเก่าเกิน STALE_AFTER_FRAMES (เฟส 7)
// สื่อว่าตำแหน่งที่เห็นเป็นการเดาต่อจากผลเดิม ไม่ใช่ผลสด
const STALE_ALPHA = 0.55;


class FaceOverlay {
  /**
   * @param {object} options
   * @param {HTMLElement} options.box           กล่องที่ภาพวางอยู่ (ต้อง position:relative)
   * @param {HTMLCanvasElement} options.canvas  canvas ที่จะวาดกรอบลงไป
   * @param {function} options.getStreamSize    ()=>{width,height}|null ขนาดจริงของภาพต้นทาง
   * @param {function} [options.getSmoothing]   ()=>number ความนุ่มนวลของการไหล 0-1
   * @param {function} [options.isMirrored]     ()=>boolean ภาพถูกพลิกกระจกอยู่หรือไม่
   */
  constructor(options) {
    this.box = options.box;
    this.canvas = options.canvas;
    this.getStreamSize = options.getStreamSize;
    this.getSmoothing = options.getSmoothing || (() => 0.35);
    this.isMirrored = options.isMirrored || (() => false);

    /* Map: track_id -> { current, target, visible, hits, identityState,
                          identity, direction, alpha, targetAlpha, dead }

       แยก "ตำแหน่งเป้าหมาย" (ผลล่าสุดจาก backend) ออกจาก "ตำแหน่งที่วาดอยู่จริง"
       เพราะสองอย่างนี้เดินคนละจังหวะกัน:
         - ผลตรวจมาถึงราว 5 ครั้ง/วินาที
         - การวาดเดินตามจังหวะรีเฟรชของจอ ~60 ครั้ง/วินาที
       ตำแหน่งที่วาดจึงค่อย ๆ ไหลเข้าหาเป้าหมายทุกเฟรมของจอ กรอบจึงเกาะหน้าลื่น */
    this.boxes = new Map();

    // ขนาดของภาพที่ "ผลตรวจจับอ้างอิงอยู่" (อาจเล็กกว่าภาพที่แสดงจริง)
    this.frameSize = null;

    this.rafId = null;
    this.lastTickAt = 0;

    // นับอัตราการวาดจริงของจอนี้ เพื่อยืนยันว่าวาดทุกเฟรมของจอจริง
    // ไม่ใช่วาดเฉพาะตอนได้ผลใหม่ (และเป็นสถิติแยกของกล้องตัวนี้)
    this.tickTimestamps = [];

    // ตัวคูณความทึบรวม ใช้ตอนผลตรวจเก่าเกินไป (เฟส 7)
    // แยกจาก alpha ของแต่ละกรอบ เพื่อไม่ให้ค่าเพี้ยนสะสม
    this.staleFactor = 1;
    this.staleTarget = 1;

    // ผูก this ไว้ตั้งแต่ตอนสร้าง เพื่อให้ requestAnimationFrame เรียกแล้ว
    // this ยังเป็นจอนี้อยู่ (ถ้าไม่ผูก this จะกลายเป็น undefined)
    this._tick = this._tick.bind(this);
  }

  // ==========================================================================
  // วงจรชีวิต
  // ==========================================================================
  start() {
    if (this.rafId !== null) return;
    this.lastTickAt = 0;
    this.tickTimestamps = [];
    this.rafId = requestAnimationFrame(this._tick);
  }

  stop() {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this.boxes.clear();
    this.frameSize = null;
    this.staleFactor = 1;
    this.staleTarget = 1;
    this.tickTimestamps = [];
    this.clear();
  }

  get isRunning() {
    return this.rafId !== null;
  }

  /** อัตราการวาดจริงในหนึ่งวินาทีที่ผ่านมา (สถิติของจอนี้เท่านั้น) */
  get renderFps() {
    return this.tickTimestamps.length;
  }

  // ==========================================================================
  // ป้อนข้อมูลเข้า
  // ==========================================================================

  /** บอกขนาดของภาพที่ผลตรวจจับอ้างอิงอยู่ เช่น [640, 360] */
  setFrameSize(size) {
    this.frameSize = (size && size.length === 2) ? size : null;
  }

  /**
   * ตั้งเป้าหมายความจางของกรอบทั้งหมด เมื่อผลตรวจเก่าเกินไป
   *
   * ใช้เป็น "ตัวคูณรวม" แยกต่างหาก ไม่ไปแก้ targetAlpha ของแต่ละกรอบ
   * เพราะค่านั้นเป็นของระบบจดจำใบหน้า (กรอบที่ backend ค้างไว้ก็จางอยู่แล้ว)
   * ถ้าไปคูณทับกัน ค่าจะเพี้ยนสะสมทุกครั้งที่สลับสถานะ
   */
  setStale(stale) {
    this.staleTarget = stale ? STALE_ALPHA : 1;
  }

  /**
   * รับผลชุดใหม่จาก backend มาตั้งเป็น "เป้าหมาย" ของแต่ละกรอบ
   *
   * ไม่วาดที่นี่ เพราะการวาดเป็นหน้าที่ของวงวาดที่เดินตามจังหวะจอ
   * ที่นี่แค่บอกว่า "กรอบควรจะไปอยู่ตรงไหน" เท่านั้น
   */
  setTracks(tracks) {
    const seen = new Set();

    (tracks || []).forEach((t) => {
      const box = { x: t.bbox[0], y: t.bbox[1], w: t.bbox[2], h: t.bbox[3] };
      seen.add(t.track_id);

      let entry = this.boxes.get(t.track_id);

      if (!entry) {
        // กรอบใหม่: ให้ตำแหน่งที่วาดเริ่มตรงกับเป้าหมายเลย
        // ถ้าเริ่มจากที่อื่นแล้วค่อยไหลเข้ามา จะเห็นกรอบวิ่งมาจากมุมจอ ซึ่งดูแปลก
        entry = {
          current: Object.assign({}, box),
          target: box,
          alpha: 0,        // เริ่มจากโปร่งใสแล้วค่อย ๆ ชัดขึ้น
          targetAlpha: 1,
        };
        this.boxes.set(t.track_id, entry);
      } else {
        entry.target = box;
      }

      entry.score = t.score;
      entry.hits = t.hits;
      entry.visible = t.visible;
      entry.missing = t.missing;
      entry.identityState = t.identity_state || 'pending';
      entry.identity = t.identity || null;
      entry.direction = t.direction || null;
      entry.dead = false;

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
    this.boxes.forEach((entry, trackId) => {
      if (!seen.has(trackId)) {
        entry.targetAlpha = 0;
        entry.dead = true;
      }
    });
  }

  // ==========================================================================
  // ขนาดของ canvas และการแปลงพิกัด
  //
  // การแปลงพิกัดมี 3 ชั้น ห้ามข้ามชั้นใดชั้นหนึ่ง:
  //
  //   ชั้นที่ 1  ภาพที่ส่งไปตรวจ   เช่น 640 x 360  <- พิกัดจาก backend อยู่ในระบบนี้
  //   ชั้นที่ 2  สตรีมจริงของกล้อง เช่น 1280 x 720
  //   ชั้นที่ 3  ขนาดที่แสดงบนจอ   เช่น 420 x 236  <- ตาม CSS เปลี่ยนตามความกว้างหน้าต่าง
  //
  // **สองจอมีค่าทั้งสามชั้นไม่เท่ากันแน่นอน** เพราะวางข้างกันคนละคอลัมน์
  // และกล้องคนละตัวมีความละเอียดไม่เท่ากันได้ (เช่น Tapo 640x360 กับเว็บแคม 640x480)
  // การคำนวณจึงต้องเป็นของใครของมันล้วน ๆ ห้ามใช้ค่าร่วมกันแม้แต่ค่าเดียว
  // ==========================================================================

  /**
   * ปรับขนาด canvas ที่วาดกรอบ ให้ครอบ "ทั้งกล่อง" ไม่ใช่แค่ส่วนที่มีภาพ
   *
   * ที่ให้ครอบทั้งกล่องเพราะเมื่อใช้ object-fit: contain แล้วอาจเกิดแถบว่าง
   * การมีระบบพิกัดจุดเริ่มเดียว (มุมซ้ายบนของกล่อง) ทำให้คิดง่ายและพลาดยาก
   * ส่วนการชดเชยแถบว่างไปอยู่ที่ getTransform ที่เดียว
   */
  syncSize() {
    const rect = this.box.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    // คูณด้วย devicePixelRatio เพื่อให้เส้นคมบนจอความละเอียดสูง
    // ถ้าไม่คูณ กรอบจะเบลอบนจอ Retina / จอ 4K ที่ตั้ง scale ไว้
    const dpr = window.devicePixelRatio || 1;

    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(rect.height * dpr);
    this.canvas.style.width = rect.width + 'px';
    this.canvas.style.height = rect.height + 'px';
    this.canvas.style.left = '0px';
    this.canvas.style.top = '0px';

    const ctx = this.canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // ทำงานในหน่วย CSS pixel ต่อจากนี้
  }

  clear() {
    const ctx = this.canvas.getContext('2d');
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.restore();
  }

  /**
   * =========================================================================
   * ฟังก์ชันแปลงพิกัดหลัก - "เฟรมต้นฉบับ -> พิกัดบน canvas ที่วาดกรอบ"
   *
   * ทุกที่ที่ต้องวาดทับภาพต้องเรียกฟังก์ชันนี้ที่เดียว ห้ามกระจายสูตรไว้หลายจุด
   * เพราะถ้าสูตรไม่ตรงกันแม้แต่ที่เดียว กรอบจะเลื่อนโดยหาสาเหตุยากมาก
   *
   * มี 3 อย่างที่ต้องคิดให้ครบ:
   *
   *   1. สเกล   - ภาพถูกย่อลงเท่าไรจึงพอดีกล่อง (object-fit: contain)
   *   2. offset - แถบว่าง (letterbox) ที่เกิดเมื่อสัดส่วนกล่องไม่ตรงกับภาพ
   *               contain จะจัดภาพไว้กลางกล่องเสมอ จึงมีแถบว่างแบ่งครึ่งสองข้าง
   *   3. mirror - โหมดเว็บแคมเดี่ยวพลิกภาพกระจก แต่ canvas ไม่ได้ถูกพลิกตาม
   *               จึงต้องกลับพิกัดแกน X เอง
   *
   * คืน null ถ้ายังคำนวณไม่ได้ (ยังไม่มีภาพ หรือกล่องยังไม่มีขนาด)
   * =========================================================================
   */
  getTransform() {
    if (!this.frameSize) return null;

    // ขนาดของภาพที่ผลตรวจจับอ้างอิงอยู่
    const frameWidth = this.frameSize[0];
    const frameHeight = this.frameSize[1];
    if (!frameWidth || !frameHeight) return null;

    // ขนาดจริงของสตรีม (อาจใหญ่กว่าภาพที่ส่งไปตรวจ)
    const stream = this.getStreamSize();
    if (!stream || !stream.width || !stream.height) return null;

    // พื้นที่ทั้งหมดที่ภาพมีสิทธิ์ใช้ (ของจอนี้เท่านั้น)
    const rect = this.box.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return null;

    // ---- 1. สเกลแบบ contain: ย่อตามด้านที่ "คับ" กว่า เพื่อให้เห็นทั้งเฟรม ----
    // ใช้ min() ไม่ใช่ max() ตรงนี้คือหัวใจของ contain
    // ถ้าใช้ max() จะกลายเป็น cover ซึ่งจะตัดขอบภาพทิ้ง
    const scale = Math.min(rect.width / stream.width, rect.height / stream.height);

    const drawnWidth = stream.width * scale;
    const drawnHeight = stream.height * scale;

    // ---- 2. แถบว่างจาก letterbox (แบ่งครึ่งสองข้างเพราะ contain จัดภาพไว้กลาง) ----
    const offsetX = (rect.width - drawnWidth) / 2;
    const offsetY = (rect.height - drawnHeight) / 2;

    // ---- ตัวคูณรวม: จากพิกัดของภาพที่ใช้ตรวจ ไปเป็นพิกเซลบนจอ ----
    const scaleX = drawnWidth / frameWidth;
    const scaleY = drawnHeight / frameHeight;

    const mirrored = Boolean(this.isMirrored());

    return {
      scaleX: scaleX,
      scaleY: scaleY,
      offsetX: offsetX,
      offsetY: offsetY,
      drawnWidth: drawnWidth,
      drawnHeight: drawnHeight,
      mirrored: mirrored,

      /** แปลงกรอบสี่เหลี่ยมจากพิกัดเฟรมต้นฉบับ ไปเป็นพิกัดบน canvas */
      rect: function (x, y, w, h) {
        const width = w * scaleX;
        const height = h * scaleY;
        const top = offsetY + y * scaleY;

        // ภาพถูกพลิกกระจกด้วย CSS transform: scaleX(-1) แต่ canvas ไม่ได้ถูกพลิกตาม
        // จึงต้องกลับพิกัดแกน X เอง โดยกลับ "ภายในพื้นที่ที่ภาพถูกวาด" เท่านั้น
        // ไม่ใช่กลับทั้งกล่อง ไม่งั้นแถบว่างจะทำให้กรอบเลื่อนไปอีก
        const left = mirrored
          ? offsetX + drawnWidth - (x + w) * scaleX
          : offsetX + x * scaleX;

        return { x: left, y: top, w: width, h: height };
      },
    };
  }

  // ==========================================================================
  // วงวาด
  // ==========================================================================

  /**
   * วงวาดหลัก - ทำงานทุกเฟรมของจอ (ปกติ 60 ครั้ง/วินาที)
   *
   * นี่คือหัวใจของเฟส 3: ของเดิมวาดเฉพาะตอนได้ผลใหม่จาก backend (~5 ครั้ง/วินาที)
   * ตาคนจึงเห็นกรอบกระโดดเป็นจังหวะ ๆ
   * พอแยกการวาดออกมาเดินตามจอแล้วไหลตำแหน่งทีละนิด กรอบจะเกาะใบหน้าลื่นขึ้นมาก
   */
  _tick(now) {
    this.rafId = requestAnimationFrame(this._tick);

    const dt = this.lastTickAt ? (now - this.lastTickAt) : 16.667;
    this.lastTickAt = now;

    // วัดอัตราการวาดจริงจากช่วง 1 วินาทีล่าสุด
    this.tickTimestamps.push(now);
    while (this.tickTimestamps.length && now - this.tickTimestamps[0] > 1000) {
      this.tickTimestamps.shift();
    }

    const k = this._smoothingFactor(dt);

    // ไหลตัวคูณความจาง (ผลตรวจเก่า) เข้าหาเป้าหมายอย่างนุ่มนวลเช่นกัน
    this.staleFactor = lerp(this.staleFactor, this.staleTarget, k);

    // ไหลทุกกรอบเข้าหาเป้าหมาย แล้วเก็บกวาดตัวที่จางจนมองไม่เห็นแล้ว
    this.boxes.forEach((entry, trackId) => {
      entry.current.x = lerp(entry.current.x, entry.target.x, k);
      entry.current.y = lerp(entry.current.y, entry.target.y, k);
      entry.current.w = lerp(entry.current.w, entry.target.w, k);
      entry.current.h = lerp(entry.current.h, entry.target.h, k);
      entry.alpha = lerp(entry.alpha, entry.targetAlpha, k);

      if (entry.dead && entry.alpha < 0.02) {
        this.boxes.delete(trackId);
      }
    });

    this.draw();
  }

  /**
   * ค่าที่ใช้ไหลเข้าหาเป้าหมาย โดยไม่ขึ้นกับว่าจอรีเฟรชเร็วแค่ไหน
   *
   * ถ้าใช้ค่าคงที่ตรง ๆ (current += (target-current) * 0.35) ความลื่นจะเปลี่ยนไป
   * ตามอัตรารีเฟรชของจอ คือจอ 144Hz จะไหลเร็วกว่าจอ 60Hz กว่าสองเท่า
   * สูตรนี้ปรับให้ได้ผลเท่ากันทุกจอ โดยอิงจากเวลาจริงที่ผ่านไป
   */
  _smoothingFactor(dtMs) {
    const s = Math.min(0.999, Math.max(0.001, this.getSmoothing()));
    return 1 - Math.pow(1 - s, dtMs / 16.667); // 16.667 ms = หนึ่งเฟรมที่ 60Hz
  }

  /** วาดกรอบทั้งหมดตามตำแหน่งที่ไหลมาถึงตอนนี้ */
  draw() {
    const ctx = this.canvas.getContext('2d');
    this.clear();

    const tf = this.getTransform();
    if (!tf) return;

    // ขอบเขตของ "พื้นที่ที่ภาพถูกวาดจริง" ใช้ดันป้ายให้อยู่ในภาพ ไม่ไปลอยบนแถบว่าง
    const imageLeft = tf.offsetX;
    const imageTop = tf.offsetY;
    const imageRight = tf.offsetX + tf.drawnWidth;
    const imageBottom = tf.offsetY + tf.drawnHeight;

    ctx.lineWidth = BOX_LINE_WIDTH;
    ctx.font = '600 14px "Sarabun", "Segoe UI", sans-serif';
    ctx.textBaseline = 'top';
    ctx.lineJoin = 'round';

    this.boxes.forEach((entry) => {
      // ความทึบสุดท้าย = ความทึบของกรอบนั้น คูณด้วยตัวคูณรวมตอนผลตรวจเก่า
      const alpha = entry.alpha * this.staleFactor;
      if (alpha < 0.02) return;

      const c = entry.current;

      // แปลงพิกัดที่เดียวผ่านฟังก์ชันกลาง (คิดสเกล แถบว่าง และ mirror ให้ครบแล้ว)
      const box = tf.rect(c.x, c.y, c.w, c.h);
      const x = box.x;
      const y = box.y;
      const w = box.w;
      const h = box.h;

      const color = BOX_COLORS[entry.identityState] || BOX_COLORS.pending;

      ctx.globalAlpha = alpha;
      ctx.strokeStyle = color;
      ctx.strokeRect(x, y, w, h);

      // ---- ป้ายกำกับ ----
      const label = buildLabel(entry);
      const padding = 7;
      const labelHeight = 23;
      const textWidth = ctx.measureText(label).width;
      const labelWidth = textWidth + padding * 2;

      // ถ้าป้ายล้นขอบบนของ "ภาพ" ให้ย้ายไปไว้ใต้กรอบแทน
      // เทียบกับขอบภาพ ไม่ใช่ขอบกล่อง เพราะแถบว่างไม่ใช่ที่ที่ควรมีป้ายไปลอยอยู่
      let labelY = y - labelHeight - 2;
      if (labelY < imageTop) {
        labelY = y + h + 2;
      }

      // ถ้าป้ายยาวจนล้นขอบขวาของภาพ ให้ดันกลับเข้ามาให้อ่านครบ
      // (ชื่อไทยเต็ม ๆ กับรหัสนักศึกษารวมกันยาวกว่ากรอบใบหน้าเสมอ
      //  และจอแต่ละจอแคบลงกว่าเดิมมากเพราะวางข้างกันสองคอลัมน์)
      let labelX = x;
      if (labelX + labelWidth > imageRight) {
        labelX = Math.max(imageLeft, imageRight - labelWidth);
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

        // ล้นขอบล่างของภาพ ให้ย้ายขึ้นมาไว้ในกรอบแทน
        if (dirY + dirHeight > imageBottom) {
          dirY = Math.max(imageTop, y + h - dirHeight - 2);
        }

        let dirX = x;
        if (dirX + dirWidth > imageRight) {
          dirX = Math.max(imageLeft, imageRight - dirWidth);
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
}


/** ไหลค่าหนึ่งค่าเข้าหาเป้าหมาย */
function lerp(from, to, k) {
  return from + (to - from) * k;
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
