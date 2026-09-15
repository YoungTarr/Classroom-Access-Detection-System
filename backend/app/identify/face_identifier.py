"""ระบุตัวตนจากใบหน้า - ประกอบ detector + recognizer + FAISS + ทะเบียนสมาชิก เข้าด้วยกัน

การทำงานแบ่งเป็นสองจังหวะ

    ตอนสร้าง/reload (ช้า ทำนาน ๆ ครั้ง)
        ดึงรายชื่อจากฐานข้อมูล -> อ่านรูป 3 มุมของแต่ละคน -> หาใบหน้าในรูป
        -> สกัด embedding -> normalize -> ใส่คลัง FAISS

    ตอน identify (เร็ว ทำทุกเฟรม)
        สกัด embedding ของใบหน้าที่เห็น -> ค้นในคลัง -> ถ้าคะแนนถึงเกณฑ์ = รู้ว่าเป็นใคร

เรื่องการรายงานปัญหา: ไฟล์นี้ตั้งใจให้ "จู้จี้" เรื่องการรายงาน
ทุกกรณีที่ข้อมูลไม่พร้อมต้องบอกได้ว่า "ไฟล์ไหน ของรหัสอะไร เป็นอะไร"
เพราะเวลาระบบจำคนไม่ได้ สาเหตุมักอยู่ที่ข้อมูลตั้งต้น ไม่ใช่ที่โมเดล
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

from app.config import settings
from app.face.detector import DetectedFace, FaceDetector, face_detector
from app.face.recognizer import FaceRecognizer, face_recognizer
from app.identify.base import Identifier, IdentityResult, IndexBuildReport
from app.identify.members import Member, MemberDirectory
from app.vector_index.faiss_index import FaceIndex

logger = logging.getLogger(__name__)


class FaceIdentifier(Identifier):
    """ระบุตัวตนด้วยการเทียบใบหน้ากับคลังเวกเตอร์"""

    name = "face"

    def __init__(
        self,
        detector: FaceDetector | None = None,
        recognizer: FaceRecognizer | None = None,
    ) -> None:
        self.detector = detector or face_detector
        self.recognizer = recognizer or face_recognizer
        self.directory = MemberDirectory()

        self._index: FaceIndex | None = None
        self._members: dict[str, Member] = {}
        self._report = IndexBuildReport()

    # ------------------------------------------------------------------
    # สร้างคลังเวกเตอร์
    # ------------------------------------------------------------------
    def reload(self) -> IndexBuildReport:
        """สร้างคลังเวกเตอร์ใหม่ทั้งหมดจากฐานข้อมูลและไฟล์รูป

        เมธอดนี้โยน exception ได้ถ้าโครงสร้างพื้นฐานมีปัญหา (เช่นโมเดลยังไม่โหลด
        หรือต่อฐานข้อมูลไม่ได้) เพราะผู้เรียกคือ endpoint ที่รอผลอยู่และควรเห็นสาเหตุเต็ม ๆ
        แต่ปัญหา "รายไฟล์" จะไม่โยน - เก็บไว้ในรายงานแล้วทำไฟล์ที่เหลือต่อ
        เพราะรูปเสียหนึ่งไฟล์ไม่ควรทำให้ทั้งระบบใช้ไม่ได้
        """
        import cv2

        started = time.monotonic()

        if not self.recognizer.is_loaded:
            self.recognizer.load()
        if not self.detector.is_loaded:
            self.detector.load()

        members = self.directory.load()
        index = FaceIndex(dimension=self.recognizer.dimension)

        report = IndexBuildReport(member_count=len(members))

        for member in members:
            vectors_added = 0

            if not member.photos:
                report.problems.append(
                    f"{member.student_id} ({member.full_name}): "
                    f"ไม่ได้กำหนด path รูปไว้ในฐานข้อมูลเลยสักมุม"
                )

            for photo in member.photos:
                # ---- กรณีที่ 1: ไม่มีไฟล์ ----
                if not photo.resolved.exists():
                    report.problems.append(
                        f"{member.student_id} {photo.angle}: ไม่พบไฟล์ {photo.resolved} "
                        f"(ฐานข้อมูลระบุไว้ว่า {photo.raw_path})"
                    )
                    continue

                # ---- กรณีที่ 2: มีไฟล์แต่เปิดไม่ได้ ----
                image = cv2.imread(str(photo.resolved))
                if image is None:
                    report.problems.append(
                        f"{member.student_id} {photo.angle}: เปิดไฟล์ {photo.label} ไม่ได้ "
                        f"(ไฟล์อาจเสีย หรือไม่ใช่ไฟล์รูปภาพ)"
                    )
                    continue

                # ---- กรณีที่ 3: เปิดได้แต่หาใบหน้าไม่เจอ ----
                try:
                    faces = self.detector.detect(image)
                except Exception as exc:  # noqa: BLE001
                    report.problems.append(
                        f"{member.student_id} {photo.angle}: ตรวจจับใบหน้าใน {photo.label} "
                        f"ไม่สำเร็จ: {exc}"
                    )
                    continue

                if not faces:
                    report.problems.append(
                        f"{member.student_id} {photo.angle}: เปิดไฟล์ {photo.label} ได้ "
                        f"แต่หาใบหน้าในรูปไม่เจอ (ลองใช้รูปที่หน้าชัดและใหญ่กว่านี้)"
                    )
                    continue

                # ---- กรณีที่ 4: เจอหลายหน้าในรูปลงทะเบียน ----
                if len(faces) > 1:
                    # ไม่ถือเป็น error แต่ต้องเตือน เพราะถ้าเลือกผิดคนจะจำผิดไปทั้งระบบ
                    # เลือกใบที่ใหญ่ที่สุด เพราะรูปลงทะเบียนเจ้าตัวย่อมอยู่ใกล้กล้องที่สุด
                    report.problems.append(
                        f"{member.student_id} {photo.angle}: พบ {len(faces)} ใบหน้าใน "
                        f"{photo.label} เลือกใบที่ใหญ่ที่สุด "
                        f"(ควรใช้รูปที่มีคนเดียวเพื่อความแน่นอน)"
                    )

                face = max(faces, key=lambda f: f.w * f.h)

                if face.keypoints is None:
                    report.problems.append(
                        f"{member.student_id} {photo.angle}: ไม่ได้จุดสังเกตบนใบหน้าจาก "
                        f"{photo.label} จึงจัดหน้าให้ตรงก่อนสกัดไม่ได้"
                    )
                    continue

                # ---- สกัด embedding ----
                try:
                    vector = self.recognizer.embed(image, face.keypoints)
                    index.add(vector, student_id=member.student_id, source=photo.label)
                    vectors_added += 1
                except Exception as exc:  # noqa: BLE001
                    report.problems.append(
                        f"{member.student_id} {photo.angle}: สกัดลายเซ็นใบหน้าจาก "
                        f"{photo.label} ไม่สำเร็จ: {exc}"
                    )

            # ---- กรณีที่ 5: สมาชิกที่ไม่ได้เวกเตอร์เลยสักตัว ----
            if vectors_added == 0:
                report.members_without_vectors.append(
                    f"{member.student_id} ({member.full_name})"
                )
            else:
                report.enrolled_count += 1

        report.vector_count = index.size
        report.build_ms = (time.monotonic() - started) * 1000.0

        self._index = index
        self._members = self.directory.build_name_map(members)
        self._report = report

        self._log_summary(report)
        return report

    @staticmethod
    def _log_summary(report: IndexBuildReport) -> None:
        """พิมพ์สรุปผลการสร้างคลังให้เห็นใน docker compose logs"""
        logger.info("-" * 70)
        logger.info(
            "สร้างคลังใบหน้าเสร็จใน %.0f ms: สมาชิก %d คน, ลงทะเบียนสำเร็จ %d คน, เวกเตอร์ %d ตัว",
            report.build_ms,
            report.member_count,
            report.enrolled_count,
            report.vector_count,
        )

        if report.members_without_vectors:
            logger.warning(
                "สมาชิก %d คนที่ระบบจะจำไม่ได้ (ไม่มีเวกเตอร์เลย): %s",
                len(report.members_without_vectors),
                ", ".join(report.members_without_vectors),
            )

        if report.problems:
            logger.warning("พบปัญหากับไฟล์รูป %d รายการ:", len(report.problems))
            for problem in report.problems:
                logger.warning("   - %s", problem)

        # กรณีที่ 6: คลังว่างทั้งระบบ - เตือนให้ดังที่สุด เพราะระบบจะขึ้น Unknown ทุกคน
        if report.vector_count == 0:
            logger.error(
                "คลังใบหน้าว่างเปล่า! ระบบจะแสดง Unknown กับทุกคนจนกว่าจะมีรูปที่ใช้ได้ "
                "วางไฟล์ไว้ที่ data/faces/<รหัสนักศึกษา>/ แล้วเรียก GET /api/faces/reload"
            )

        logger.info("-" * 70)

    # ------------------------------------------------------------------
    # ระบุตัวตน
    # ------------------------------------------------------------------
    def identify(self, image: np.ndarray, face: DetectedFace) -> IdentityResult:
        """ระบุว่าใบหน้านี้เป็นใคร - ห้ามโยน exception ออกไปไม่ว่ากรณีใด"""
        try:
            return self._identify_inner(image, face)
        except Exception as exc:  # noqa: BLE001
            # ตาข่ายกันพลาดชั้นสุดท้าย: อะไรที่คาดไม่ถึงต้องกลายเป็น unknown
            # พร้อมเหตุผล ไม่ใช่ทำให้ WebSocket ขาดกลางคัน
            logger.exception("identify() เกิดข้อผิดพลาดที่ไม่คาดคิด: %s", exc)
            return IdentityResult.unknown(
                identified_by=self.name,
                detail=f"เกิดข้อผิดพลาดที่ไม่คาดคิดระหว่างระบุตัวตน: {exc}",
            )

    def _identify_inner(self, image: np.ndarray, face: DetectedFace) -> IdentityResult:
        threshold = settings.identify.similarity_threshold

        # ---- คลังยังไม่ถูกสร้าง ----
        if self._index is None:
            return IdentityResult.unknown(
                identified_by=self.name,
                detail="ยังไม่ได้สร้างคลังใบหน้า (เรียก GET /api/faces/reload เพื่อสร้าง)",
            )

        # ---- คลังว่าง: คนละเรื่องกับ "ไม่รู้จักคนนี้" ต้องแยกให้ชัด ----
        if self._index.is_empty:
            return IdentityResult.unknown(
                identified_by=self.name,
                detail=(
                    "ยังไม่มีเวกเตอร์ใบหน้าในระบบเลย "
                    f"(สมาชิก {self._report.member_count} คน แต่ไม่มีรูปที่ใช้ได้) "
                    "วางไฟล์ที่ data/faces/<รหัสนักศึกษา>/ แล้วเรียก /api/faces/reload"
                ),
            )

        # ---- ไม่มีจุดสังเกต จัดหน้าให้ตรงไม่ได้ ----
        if face.keypoints is None:
            return IdentityResult.unknown(
                identified_by=self.name,
                detail="ไม่ได้จุดสังเกตบนใบหน้าจากตัวตรวจจับ จึงจัดหน้าให้ตรงก่อนเทียบไม่ได้",
            )

        # ---- สกัด embedding แล้วค้นในคลัง ----
        vector = self.recognizer.embed(image, face.keypoints)
        hits = self._index.search(vector, top_k=3)

        if not hits:
            return IdentityResult.unknown(
                identified_by=self.name,
                detail="ค้นในคลังแล้วไม่ได้ผลลัพธ์กลับมาเลย",
            )

        best = hits[0]

        # ---- คะแนนไม่ถึงเกณฑ์ = ไม่ใช่คนในระบบ ----
        if best.score < threshold:
            return IdentityResult.unknown(
                identified_by=self.name,
                confidence=best.score,
                detail=(
                    f"คะแนนความคล้ายสูงสุด {best.score:.3f} ต่ำกว่าเกณฑ์ {threshold:.2f} "
                    f"(ใกล้เคียง {best.student_id} มากที่สุด)"
                ),
            )

        # ---- ถึงเกณฑ์แล้ว ต้องหาชื่อจากฐานข้อมูล ----
        member = self._members.get(best.student_id)
        if member is None:
            # เกิดได้ถ้ามีคนลบสมาชิกออกจากฐานข้อมูลหลังสร้างคลังไปแล้ว
            return IdentityResult.unknown(
                identified_by=self.name,
                confidence=best.score,
                detail=(
                    f"เจอเวกเตอร์ที่ตรงกับรหัส {best.student_id} แต่หาข้อมูลคนนี้ "
                    f"ในฐานข้อมูลไม่เจอแล้ว (อาจถูกลบไป) ลองเรียก /api/faces/reload"
                ),
            )

        # ระยะห่างจากอันดับสอง ยิ่งห่างยิ่งมั่นใจว่าไม่ได้สับสนกับคนอื่น
        margin = best.score - hits[1].score if len(hits) > 1 else best.score

        return IdentityResult.recognized(
            identified_by=self.name,
            student_id=member.student_id,
            first_name=member.first_name,
            last_name=member.last_name,
            confidence=best.score,
            detail=(
                f"ตรงกับ {best.source} ที่คะแนน {best.score:.3f} "
                f"(ห่างจากอันดับสอง {margin:.3f})"
            ),
        )

    # ------------------------------------------------------------------
    # สถานะ
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """ข้อมูลสรุปสำหรับ /api/health"""
        cfg = settings.identify
        data: dict[str, Any] = {
            "mode": self.name,
            "ready": self._index is not None and not self._index.is_empty,
            "similarity_threshold": cfg.similarity_threshold,
            "recognition_model": self.recognizer.model_name,
            "vote_window": cfg.vote_window,
            "min_votes": cfg.min_votes,
        }
        data.update(self._report.as_dict())
        return data
