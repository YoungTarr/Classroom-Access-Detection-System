"""โปรแกรมอ่านกล้อง RTSP ที่รันแยกเป็นอีก process (เฟส 11)

ทำไมต้องแยก process:
เดิม thread อ่านกล้องอยู่ใน process เดียวกับงานตรวจจับใบหน้า ทั้งสองแย่งกันเข้าคิว
GIL ของ Python ผลคือกล้องที่ส่งมา 15 fps อ่านได้จริงแค่ 4-8 fps (วัดแล้ว: สคริปต์แยก
ดึงจากกล้องตัวเดียวกันได้ 17 fps ทั้งที่ระบบกำลังทำงานเต็มที่) การแยก process
ทำให้แต่ละ process มี GIL ของตัวเอง ไม่มีใครแย่งใคร

process นี้ทำหน้าที่เดียว: ต่อกล้อง -> อ่านเฟรม -> เขียนลงหน่วยความจำร่วม (shared memory)
ส่วน RTSPFrameSource (ฝั่ง backend) ไปหยิบเฟรมล่าสุดจากที่เดียวกัน

รันด้วย:  python -m app.core.capture_worker
ค่าตั้งต้นรับผ่านตัวแปรสภาพแวดล้อม (ไม่ใส่ใน argv เพราะ argv มองเห็นได้ด้วย ps
และ URL มีรหัสผ่านกล้อง):
    CADS_CAPTURE_CONFIG  JSON: url, safe_url, password, shm_path, capture_fps,
                         ffmpeg_options, watchdog_timeout, reconnect_initial,
                         reconnect_max, camera_id

**ห้าม import app.config หรือโมดูลหนัก ๆ ในไฟล์นี้** process นี้ต้องเบาและสตาร์ทเร็ว
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

import numpy as np

# ---------------------------------------------------------------------------
# รูปแบบหน่วยความจำร่วม (ใช้ร่วมกับ RTSPFrameSource - แก้ที่นี่ที่เดียว)
# ---------------------------------------------------------------------------
#
#   [0 .. HEADER_BYTES)            header: float64 x HEADER_FIELDS + ข้อความ error
#   [HEADER_BYTES .. )             ช่องเก็บเฟรม NUM_SLOTS ช่อง ช่องละ SLOT_BYTES
#
# เขียนด้วย "seqlock": ผู้เขียนเพิ่ม seq เป็นเลขคี่ก่อนเขียน แล้วเป็นเลขคู่หลังเขียนเสร็จ
# ผู้อ่านต้องอ่านซ้ำถ้าเจอ seq เป็นเลขคี่ หรือ seq เปลี่ยนระหว่างอ่าน

NUM_SLOTS = 3
MAX_WIDTH = 2560
MAX_HEIGHT = 1440
SLOT_BYTES = MAX_WIDTH * MAX_HEIGHT * 3

HEADER_FIELDS = 16
ERROR_OFFSET = 256
ERROR_MAX_BYTES = 512
HEADER_BYTES = 4096
TOTAL_BYTES = HEADER_BYTES + NUM_SLOTS * SLOT_BYTES

# ตำแหน่งของแต่ละช่องใน header
H_SEQ = 0
H_FRAME_ID = 1       # เลขลำดับเฟรมล่าสุดที่เขียนเสร็จ (เริ่มที่ 1)
H_WIDTH = 2
H_HEIGHT = 3
H_RECEIVED_AT = 4    # time.monotonic() ของตอนได้เฟรม (ระบบเดียวกัน ใช้ข้าม process ได้)
H_CONNECTED = 5
H_LAST_FRAME_AT = 6
H_RECONNECTS = 7
H_CONNECT_ATTEMPTS = 8
H_READ_FPS = 9
H_ERROR_LEN = 10
H_HEARTBEAT = 11     # เวลาล่าสุดที่ลูปของ worker ยังหมุนอยู่

logger = logging.getLogger("capture_worker")


class SharedHeader:
    """เข้าถึง header + ช่องเฟรมบน memory-mapped file (ใช้ทั้งสองฝั่ง)"""

    def __init__(self, path: str, create: bool) -> None:
        mode = "w+" if create else "r+"
        self.mem = np.memmap(path, dtype=np.uint8, mode=mode, shape=(TOTAL_BYTES,))
        self.fields = self.mem[: HEADER_FIELDS * 8].view(np.float64)
        self.error_buf = self.mem[ERROR_OFFSET: ERROR_OFFSET + ERROR_MAX_BYTES]

    def slot(self, index: int) -> np.ndarray:
        start = HEADER_BYTES + index * SLOT_BYTES
        return self.mem[start: start + SLOT_BYTES]

    def close(self) -> None:
        # ปล่อย view ทั้งหมดก่อน แล้วค่อยปิด mmap
        del self.fields, self.error_buf, self.mem


def read_header(shared: SharedHeader) -> dict:
    """อ่าน header แบบ seqlock - ได้ชุดค่าที่สอดคล้องกันเสมอ"""
    for _ in range(100):
        seq1 = shared.fields[H_SEQ]
        if seq1 % 2 == 1:
            time.sleep(0.0002)
            continue
        values = shared.fields.copy()
        error_len = int(values[H_ERROR_LEN])
        error = bytes(shared.error_buf[:error_len]).decode("utf-8", "replace") if error_len else None
        if shared.fields[H_SEQ] == seq1:
            return {"values": values, "error": error}
    # ผู้เขียนค้างอยู่กลางการเขียน ใช้ค่าที่อ่านได้ล่าสุดไปก่อน
    values = shared.fields.copy()
    return {"values": values, "error": None}


class _Writer:
    """ฝั่งเขียน (อยู่ใน worker เท่านั้น)"""

    def __init__(self, shared: SharedHeader) -> None:
        self.shared = shared
        self.f = shared.fields

    def update(self, **fields: float) -> None:
        """อัปเดตหลายช่องของ header พร้อมกันแบบ seqlock"""
        names = {
            "frame_id": H_FRAME_ID, "width": H_WIDTH, "height": H_HEIGHT,
            "received_at": H_RECEIVED_AT, "connected": H_CONNECTED,
            "last_frame_at": H_LAST_FRAME_AT, "reconnects": H_RECONNECTS,
            "connect_attempts": H_CONNECT_ATTEMPTS, "read_fps": H_READ_FPS,
            "heartbeat": H_HEARTBEAT,
        }
        self.f[H_SEQ] += 1  # เลขคี่ = กำลังเขียน
        for key, value in fields.items():
            self.f[names[key]] = value
        self.f[H_SEQ] += 1  # เลขคู่ = เขียนเสร็จ

    def set_error(self, message: str | None) -> None:
        data = (message or "").encode("utf-8")[:ERROR_MAX_BYTES]
        self.f[H_SEQ] += 1
        self.shared.error_buf[: len(data)] = np.frombuffer(data, dtype=np.uint8)
        self.f[H_ERROR_LEN] = len(data)
        self.f[H_SEQ] += 1


class Worker:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.shared = SharedHeader(cfg["shm_path"], create=False)
        self.out = _Writer(self.shared)
        self.stop = False
        self.parent_pid = os.getppid()

        self.frame_id = 0
        self.reconnects = 0
        self.connect_attempts = 0
        self.read_times: list[float] = []

    # ------------------------------------------------------------------
    def should_stop(self) -> bool:
        # parent ตายไปแล้ว (ถูกรับช่วงโดย process อื่น) ต้องไม่ทิ้งตัวเองไว้ค้างเป็นกำพร้า
        return self.stop or os.getppid() != self.parent_pid

    def scrub(self, text: str) -> str:
        text = text.replace(self.cfg["url"], self.cfg["safe_url"])
        if self.cfg.get("password"):
            text = text.replace(self.cfg["password"], "***")
        return text

    def sleep(self, seconds: float) -> None:
        """หลับแบบตื่นมาเช็กสัญญาณหยุดได้"""
        end = time.monotonic() + seconds
        while not self.should_stop() and time.monotonic() < end:
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))

    def fps(self, now: float) -> float:
        window = 3.0
        self.read_times = [t for t in self.read_times if now - t <= window]
        if len(self.read_times) < 2:
            return 0.0
        span = self.read_times[-1] - self.read_times[0]
        return (len(self.read_times) - 1) / span if span > 0 else 0.0

    # ------------------------------------------------------------------
    def run(self) -> None:
        delay = self.cfg["reconnect_initial"]

        while not self.should_stop():
            capture = self.open_capture()

            if capture is None:
                self.connect_attempts += 1
                self.out.update(connect_attempts=self.connect_attempts, connected=0, read_fps=0,
                                heartbeat=time.monotonic())
                logger.warning(
                    "กล้อง %s ต่อไม่ได้ (ครั้งที่ %d) จะลองใหม่ในอีก %.0f วินาที (%s)",
                    self.cfg["camera_id"], self.connect_attempts, delay, self.last_error,
                )
                self.sleep(delay)
                delay = min(delay * 2, self.cfg["reconnect_max"])
                continue

            delay = self.cfg["reconnect_initial"]
            self.read_loop(capture)
            capture.release()
            self.read_times.clear()
            self.out.update(connected=0, read_fps=0, heartbeat=time.monotonic())

            if not self.should_stop():
                self.reconnects += 1
                self.out.update(reconnects=self.reconnects)
                logger.warning(
                    "กล้อง %s สตรีมหลุด (%s) กำลังต่อใหม่ครั้งที่ %d",
                    self.cfg["camera_id"], self.last_error, self.reconnects,
                )

    last_error: str | None = None

    def open_capture(self):
        import cv2

        # FFmpeg อ่านค่านี้ตอนเปิดสตรีมเท่านั้น ต้องตั้งก่อนสร้าง VideoCapture
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = self.cfg["ffmpeg_options"]

        try:
            capture = cv2.VideoCapture(self.cfg["url"], cv2.CAP_FFMPEG)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"สร้าง VideoCapture ไม่สำเร็จ: {self.scrub(str(exc))}"
            self.out.set_error(self.last_error)
            return None

        if not capture.isOpened():
            capture.release()
            self.last_error = (
                f"เปิดสตรีมไม่ได้ที่ {self.cfg['safe_url']} - "
                "ตรวจว่ากล้องเปิดอยู่ ต่อเน็ตเวิร์กเดียวกัน "
                "และบัญชีผู้ใช้/รหัสผ่านถูกต้อง"
            )
            self.out.set_error(self.last_error)
            return None

        self.last_error = None
        self.out.set_error(None)
        now = time.monotonic()
        self.out.update(connected=1, last_frame_at=now, heartbeat=now)
        logger.info(
            "ต่อกล้อง %s สำเร็จ (%s) ความละเอียด %dx%d",
            self.cfg["camera_id"], self.cfg["safe_url"],
            int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        return capture

    def read_loop(self, capture) -> None:
        """ตรรกะเดียวกับ RTSPFrameSource._read_loop เดิมทุกประการ
        (grab ทุกเฟรม + token bucket + watchdog) แต่เขียนลง shared memory"""
        timeout = self.cfg["watchdog_timeout"]
        capture_fps = max(1, self.cfg["capture_fps"])
        # ถังต้องจุพอรับ "ก้อน" เฟรมที่ Wi-Fi ส่งมาติดกันหลังช่วงเงียบ (ครึ่งวินาที)
        # ถ้าจุแค่ 2 ตอนตั้ง CAPTURE_FPS เท่ากับอัตรากล้อง จะทิ้งเฟรมไปราว 40%
        # (วัดจริง: กล้อง 15 fps เหลือ 8-9) ทั้งที่อัตราเฉลี่ยไม่เกินที่ตั้งเลย
        burst_size = max(2.0, capture_fps * 0.5)
        tokens = burst_size
        last_token_at = time.monotonic()
        last_good_at = time.monotonic()

        while not self.should_stop():
            ok = capture.grab()
            now = time.monotonic()

            if now - last_good_at > timeout:
                self.last_error = (
                    f"ไม่ได้เฟรมใหม่เกิน {timeout:.0f} วินาที "
                    "(กล้องอาจถูกถอดปลั๊ก หรือหลุดจากเครือข่าย)"
                )
                self.out.set_error(self.last_error)
                return

            if not ok:
                self.out.update(heartbeat=now)
                time.sleep(0.05)
                continue

            last_good_at = now

            tokens = min(burst_size, tokens + (now - last_token_at) * capture_fps)
            last_token_at = now
            if tokens < 1.0:
                continue

            ok, image = capture.retrieve()
            if not ok or image is None:
                continue

            height, width = image.shape[:2]
            if width > MAX_WIDTH or height > MAX_HEIGHT or image.ndim != 3:
                self.last_error = (
                    f"ภาพจากกล้องใหญ่เกินที่รองรับ ({width}x{height} "
                    f"เกิน {MAX_WIDTH}x{MAX_HEIGHT})"
                )
                self.out.set_error(self.last_error)
                time.sleep(0.5)
                continue

            tokens -= 1.0
            self.frame_id += 1

            # เขียนภาพลงช่องถัดไปก่อน แล้วค่อยประกาศใน header
            # ผู้อ่านจึงไม่เห็นเฟรมที่เขียนไม่เสร็จ
            slot = self.shared.slot(self.frame_id % NUM_SLOTS)
            nbytes = height * width * 3
            slot[:nbytes] = np.ascontiguousarray(image).reshape(-1)

            self.read_times.append(now)
            self.out.update(
                frame_id=self.frame_id, width=width, height=height,
                received_at=now, last_frame_at=now, heartbeat=now,
                read_fps=self.fps(now),
            )
            if self.last_error is not None:
                self.last_error = None
                self.out.set_error(None)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:     [capture] %(message)s",
    )
    cfg = json.loads(os.environ.pop("CADS_CAPTURE_CONFIG"))
    worker = Worker(cfg)

    def on_signal(signum, frame):  # noqa: ARG001
        worker.stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
