"""มาตรวัดอัตราที่เกิดขึ้น "จริง" ของขั้นตอนหนึ่ง (เฟรมต่อวินาที)

แยกออกมาเป็นไฟล์ของตัวเองตั้งแต่เฟส 8 เพราะตอนนี้มีคนใช้สองฝั่ง:

    frame_source.py   วัดอัตราที่อ่านเฟรมจากกล้องได้จริง (ทำงานใน thread ของกล้อง)
    stream_runner.py  วัดอัตราตรวจจับและอัตราส่งภาพ (ทำงานใน event loop)

ถ้าปล่อยให้คลาสนี้อยู่ใน stream_runner.py เหมือนเดิม frame_source จะต้อง import
stream_runner ซึ่งย้อนกลับมา import frame_source อีกที กลายเป็น import วนลูป

**ต้องวัดของจริง ไม่ใช่รายงานค่าที่ตั้งไว้ใน config**
เพราะถ้าเครื่องทำไม่ทันค่าที่ตั้ง ผู้ใช้ต้องเห็นตัวเลขที่ทำได้จริง
ไม่งั้นจะจูนอะไรไม่ถูกเลย โดยเฉพาะตอนมีสองกล้องแย่ง CPU กันอยู่
"""

from __future__ import annotations

import threading
import time


class RateMeter:
    """นับจำนวนครั้งที่เกิดเหตุการณ์ในช่วงเวลาหนึ่ง แล้วคิดเป็นอัตราต่อวินาที

    ใส่ล็อกไว้เพราะ tick() กับ fps อาจถูกเรียกจากคนละ thread
    (thread อ่านกล้องเป็นคน tick แต่ event loop เป็นคนอ่านค่าไปรายงาน)
    ต้นทุนของล็อกไม่มีนัยสำคัญที่อัตราหลักสิบครั้งต่อวินาที
    """

    def __init__(self, window_seconds: float = 3.0) -> None:
        self.window = window_seconds
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._timestamps.append(now)
            cutoff = now - self.window
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.pop(0)

    @property
    def fps(self) -> float:
        with self._lock:
            if len(self._timestamps) < 2:
                return 0.0
            span = self._timestamps[-1] - self._timestamps[0]
            count = len(self._timestamps)

        if span <= 0:
            return 0.0
        return (count - 1) / span

    def reset(self) -> None:
        """ล้างค่าที่วัดไว้ (ใช้ตอนกล้องต่อใหม่ เพื่อไม่ให้ค่าเก่าค้างมาปนกับของใหม่)"""
        with self._lock:
            self._timestamps.clear()
