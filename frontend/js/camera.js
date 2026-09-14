/* =============================================================================
   camera.js - จัดการเว็บแคมฝั่งเบราว์เซอร์อย่างเดียว
   ไม่ยุ่งกับ WebSocket และไม่ยุ่งกับการวาดกรอบ (อยู่ใน app.js)

   ข้อควรรู้ที่ทำให้พังบ่อย รวมไว้ตรงนี้:

   1. getUserMedia ใช้ได้เฉพาะหน้าเว็บที่เป็น "secure context" เท่านั้น
      คือ http://localhost หรือเว็บที่เป็น HTTPS
      ถ้าเปิดด้วย http://127.0.0.1 หรือ http://192.168.x.x จะถูกบล็อก
      -> ตอนย้ายขึ้น Pi แล้วเปิดจากเครื่องอื่นต้องทำ HTTPS หรือใช้กล้อง RTSP แทน (เฟส 6)

   2. enumerateDevices() จะไม่บอก "ชื่อ" กล้อง (label เป็นสตริงว่าง)
      จนกว่าผู้ใช้จะกดอนุญาตให้ใช้กล้องก่อน
      -> ต้องขอสิทธิ์ก่อน แล้วค่อยไปดึงรายชื่อมาเติม dropdown

   3. การปิดกล้องต้องเรียก track.stop() ให้ครบทุก track
      ถ้าลืม ไฟสัญญาณกล้องจะยังติดค้างและกล้องจะถูกจองไว้ไม่ให้โปรแกรมอื่นใช้
   ============================================================================= */

'use strict';

/**
 * แปลง error จาก getUserMedia เป็นข้อความภาษาไทยที่บอกวิธีแก้ได้จริง
 * ไม่ปล่อยให้ผู้ใช้เห็นแค่ "NotReadableError" ซึ่งไม่มีใครรู้ว่าต้องทำอะไรต่อ
 */
function describeCameraError(err) {
  const name = err && err.name ? err.name : 'UnknownError';

  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
      return 'เบราว์เซอร์ไม่อนุญาตให้ใช้กล้อง — กดไอคอนรูปกล้องบนแถบที่อยู่เว็บ แล้วเลือกอนุญาต จากนั้นโหลดหน้าใหม่';

    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return 'ไม่พบกล้องบนเครื่องนี้ — ตรวจว่าเสียบกล้องแล้ว และไม่ได้ถูกปิดไว้ใน Device Manager';

    case 'NotReadableError':
    case 'TrackStartError':
      return 'เปิดกล้องไม่ได้เพราะมีโปรแกรมอื่นใช้อยู่ — ปิด Zoom / Teams / Discord / OBS หรือแท็บอื่นที่เปิดกล้องค้างไว้ แล้วลองใหม่';

    case 'OverconstrainedError':
    case 'ConstraintNotSatisfiedError':
      return 'กล้องตัวนี้ไม่รองรับความละเอียดที่ขอ — ลองเลือกกล้องตัวอื่นจากรายการ';

    case 'SecurityError':
      return 'ถูกบล็อกด้วยเหตุผลด้านความปลอดภัย — ต้องเปิดหน้าเว็บผ่าน http://localhost:3000 หรือผ่าน HTTPS เท่านั้น';

    case 'AbortError':
      return 'ระบบปฏิบัติการยกเลิกการเปิดกล้อง — ลองถอดแล้วเสียบกล้องใหม่';

    default:
      return 'เปิดกล้องไม่สำเร็จ (' + name + '): ' + (err && err.message ? err.message : 'ไม่ทราบสาเหตุ');
  }
}


/**
 * ควบคุมเว็บแคมหนึ่งตัว ผูกกับ <video> หนึ่งอัน
 */
class CameraController {

  /**
   * @param {HTMLVideoElement} videoEl  element ที่จะใช้แสดงภาพสด
   */
  constructor(videoEl) {
    this.video = videoEl;
    this.stream = null;

    // canvas ที่ใช้ย่อภาพก่อนส่งไป backend (ไม่ได้แสดงบนหน้าจอ)
    // สร้างครั้งเดียวแล้วใช้ซ้ำ ถ้าสร้างใหม่ทุกเฟรมจะสร้างขยะให้ GC เก็บไม่ทัน
    this.captureCanvas = document.createElement('canvas');
    this.captureCtx = this.captureCanvas.getContext('2d', { alpha: false });
  }

  /** กล้องกำลังเปิดอยู่หรือไม่ */
  get isRunning() {
    return this.stream !== null;
  }

  /** ความละเอียดจริงของสตรีม (ไม่ใช่ขนาดที่แสดงบนจอ) */
  get streamSize() {
    if (!this.video.videoWidth) return null;
    return { width: this.video.videoWidth, height: this.video.videoHeight };
  }

  // -------------------------------------------------------------------------
  // รายชื่อกล้อง
  // -------------------------------------------------------------------------

  /**
   * ดึงรายชื่อกล้องทั้งหมด
   *
   * หมายเหตุสำคัญ: ถ้ายังไม่เคยได้รับสิทธิ์ใช้กล้อง label จะเป็นสตริงว่าง
   * ฟังก์ชันนี้จึงคืน hasLabels มาด้วย ให้ผู้เรียกตัดสินใจว่าจะขอสิทธิ์ก่อนไหม
   */
  async listCameras() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      throw new Error('เบราว์เซอร์นี้ไม่รองรับการดูรายชื่ออุปกรณ์ (enumerateDevices)');
    }

    const devices = await navigator.mediaDevices.enumerateDevices();
    const cameras = devices.filter((d) => d.kind === 'videoinput');

    return {
      cameras: cameras,
      hasLabels: cameras.length > 0 && cameras.every((c) => c.label !== ''),
    };
  }

  /**
   * ขอสิทธิ์ใช้กล้องแบบชั่วคราวเพื่อให้ได้ชื่อกล้องมาเติม dropdown
   * เปิดแล้วปิดทันที ไฟกล้องจะติดแวบเดียว
   */
  async requestPermission() {
    const tmp = await navigator.mediaDevices.getUserMedia({ video: true });
    tmp.getTracks().forEach((t) => t.stop());
  }

  // -------------------------------------------------------------------------
  // เปิด / ปิดกล้อง
  // -------------------------------------------------------------------------

  /**
   * เปิดกล้อง
   * @param {string|null} deviceId  รหัสกล้องที่ต้องการ หรือ null = ให้เบราว์เซอร์เลือกเอง
   */
  async start(deviceId) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error(
        'เบราว์เซอร์นี้ไม่รองรับการใช้กล้อง (getUserMedia) — ' +
        'ต้องเปิดผ่าน http://localhost:3000 หรือผ่าน HTTPS เท่านั้น'
      );
    }

    // ปิดของเดิมก่อนเสมอ กันกรณีกดเปิดซ้อน หรือสลับกล้องแล้วตัวเก่าค้าง
    this.stop();

    // ขอ 1280x720 แบบ ideal ไม่ใช่ exact เพื่อให้กล้องที่ทำไม่ได้ยังเปิดผ่าน
    // แล้วเลือกความละเอียดใกล้เคียงที่สุดให้แทน (ถ้าใช้ exact จะ error ทันที)
    const constraints = {
      audio: false,
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
    };

    if (deviceId) {
      constraints.video.deviceId = { exact: deviceId };
    }

    try {
      this.stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (err) {
      this.stream = null;
      throw new Error(describeCameraError(err));
    }

    this.video.srcObject = this.stream;

    // รอจนรู้ความละเอียดจริงก่อน ไม่งั้น videoWidth จะยังเป็น 0
    // แล้วการคำนวณพิกัดกรอบทั้งหมดจะผิดในเฟรมแรก ๆ
    await this._waitForMetadata();
    await this.video.play();

    return this.streamSize;
  }

  /** รอ event loadedmetadata (หรือข้ามไปเลยถ้ารู้ขนาดอยู่แล้ว) */
  _waitForMetadata() {
    if (this.video.readyState >= 1 && this.video.videoWidth > 0) {
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error('กล้องเปิดแล้วแต่ไม่ส่งภาพมาภายใน 10 วินาที — ลองเลือกกล้องตัวอื่น'));
      }, 10000);

      this.video.addEventListener('loadedmetadata', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
  }

  /**
   * ปิดกล้อง
   *
   * ต้อง stop() ทุก track ไม่ใช่แค่ track แรก
   * ไม่งั้นไฟกล้องจะค้างติดและโปรแกรมอื่นจะเปิดกล้องไม่ได้
   */
  stop() {
    if (this.stream) {
      this.stream.getTracks().forEach((track) => track.stop());
      this.stream = null;
    }
    // ล้าง srcObject ด้วย ไม่งั้นเบราว์เซอร์บางตัวยังถือ reference ไว้
    this.video.srcObject = null;
  }

  // -------------------------------------------------------------------------
  // จับภาพหนึ่งเฟรมเพื่อส่งไปตรวจจับ
  // -------------------------------------------------------------------------

  /**
   * ย่อภาพจากสตรีมแล้วเข้ารหัสเป็น JPEG
   *
   * ย่อก่อนส่งเพราะ
   *   - ภาพ 1280x720 เต็ม ๆ ทำให้ AI ช้าลงมากโดยไม่ได้ความแม่นเพิ่มเท่าไร
   *   - กินแบนด์วิดท์ WebSocket โดยใช่เหตุ
   *
   * หมายเหตุ: ภาพที่ส่งไปจะ "ไม่กลับกระจก" เสมอ คือเป็นภาพตามที่กล้องเห็นจริง
   * การกลับกระจกเป็นเรื่องของการแสดงผลเท่านั้น (จัดการใน app.js ตอนวาดกรอบ)
   * ถ้าส่งภาพกลับกระจกไปให้ AI ด้วย พิกัดที่ได้กลับมาจะสับสนหนักขึ้นไปอีก
   *
   * @param {number} targetWidth  ความกว้างที่ต้องการ
   * @param {number} quality      คุณภาพ JPEG 0.0-1.0
   * @returns {Promise<{blob: Blob, width: number, height: number}|null>}
   */
  async captureJpeg(targetWidth, quality) {
    const size = this.streamSize;
    if (!size) return null;

    // รักษาอัตราส่วนภาพเดิมไว้ ถ้าบิดอัตราส่วนกรอบที่ได้กลับมาจะเพี้ยน
    const scale = targetWidth / size.width;
    const width = Math.round(targetWidth);
    const height = Math.round(size.height * scale);

    if (this.captureCanvas.width !== width || this.captureCanvas.height !== height) {
      this.captureCanvas.width = width;
      this.captureCanvas.height = height;
    }

    this.captureCtx.drawImage(this.video, 0, 0, width, height);

    const blob = await new Promise((resolve) => {
      this.captureCanvas.toBlob(resolve, 'image/jpeg', quality);
    });

    if (!blob) return null;

    return { blob: blob, width: width, height: height };
  }
}
