"""Local video squat capture and replay UI for the M7 baseline."""

import csv
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Condition, Event, Lock, Thread

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai_device_bridge.services.squat_analysis import SquatAnalyzer, SquatResult


def percentile(values: deque[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percentage))]


class LatestCamera:
    """Read continuously so slower inference never queues old camera frames."""

    def __init__(self, capture, writer, stop_requested: Event) -> None:
        self.capture = capture
        self.writer = writer
        self.stop_requested = stop_requested
        self.condition = Condition()
        self.latest: tuple[int, object, int] | None = None
        self.done = False
        self.error: Exception | None = None
        self.frames_read = 0
        self.first_read_ns = 0
        self.last_read_ns = 0
        self.thread = Thread(target=self._read, name="squat-camera", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _read(self) -> None:
        index = 0
        try:
            while not self.stop_requested.is_set():
                ok, frame = self.capture.read()
                if not ok:
                    break
                read_ns = time.perf_counter_ns()
                if not self.first_read_ns:
                    self.first_read_ns = read_ns
                self.last_read_ns = read_ns
                self.frames_read += 1
                if self.writer is not None:
                    self.writer.write(frame)
                with self.condition:
                    self.latest = (index, frame, read_ns)
                    self.condition.notify_all()
                index += 1
        except Exception as error:
            self.error = error
        finally:
            with self.condition:
                self.done = True
                self.condition.notify_all()

    def next_after(self, previous: int) -> tuple[int, object, int] | None:
        with self.condition:
            self.condition.wait_for(
                lambda: self.done or self.stop_requested.is_set()
                or (self.latest is not None and self.latest[0] > previous),
                timeout=0.25,
            )
            if self.latest is not None and self.latest[0] > previous:
                return self.latest
            return None


class MotionCaptureWorker(QThread):
    frame_ready = Signal(object, object, object, float)
    status_changed = Signal(str)
    failed = Signal(str)
    completed = Signal(str)

    def __init__(
        self, source: int | str, output_dir: Path | None = None, requested_fps: int = 30
    ) -> None:
        super().__init__()
        self.source = source
        self.output_dir = output_dir
        self.requested_fps = requested_fps
        self.stop_requested = Event()
        self.frame_lock = Lock()
        self.frame_pending = False

    def stop(self) -> None:
        self.stop_requested.set()

    def ack_frame(self) -> None:
        with self.frame_lock:
            self.frame_pending = False

    def reserve_frame(self) -> bool:
        with self.frame_lock:
            if self.frame_pending:
                return False
            self.frame_pending = True
            return True

    def run(self) -> None:
        try:
            self._capture()
        except Exception as error:
            self.failed.emit(f"视频分析启动或运行失败：{error}")

    def _capture(self) -> None:
        import cv2
        import mediapipe as mp

        camera = isinstance(self.source, int)
        if camera and sys.platform == "win32":
            capture = cv2.VideoCapture(self.source, cv2.CAP_DSHOW)
            if not capture.isOpened():
                capture.release()
                capture = cv2.VideoCapture(self.source)
        else:
            capture = cv2.VideoCapture(self.source)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError("无法打开摄像头或视频，请检查设备编号、权限或文件格式。")

        writer = None
        csv_file = None
        csv_writer = None
        camera_reader = None
        completion_message = ""
        try:
            if camera:
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                capture.set(cv2.CAP_PROP_FPS, self.requested_fps)
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            fps = capture.get(cv2.CAP_PROP_FPS)
            if not 1 <= fps <= 120:
                fps = 30.0
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if width < 1 or height < 1:
                raise RuntimeError("视频设备没有提供有效的画面尺寸。")
            self.status_changed.emit(
                f"已打开 {'摄像头' if camera else '视频'}：{width}×{height}，"
                f"设备报告 {fps:.1f} FPS；正在分析。"
            )

            if self.output_dir is not None:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                stem = datetime.now().strftime("squat-%Y%m%d-%H%M%S-%f")
                csv_path = self.output_dir / f"{stem}.csv"
                csv_file = csv_path.open("x", newline="", encoding="utf-8-sig")
                csv_writer = csv.writer(csv_file)
                csv_writer.writerow([
                    "frame", "source_time_ms", "read_time_ns", "inference_ms", "phase",
                    "repetitions", "side", "confidence", "knee_angle_deg", "hip_angle_deg",
                    "trunk_lean_deg", "hip_below_knee", "feedback",
                    "shoulder_x", "shoulder_y", "shoulder_visibility",
                    "hip_x", "hip_y", "hip_visibility",
                    "knee_x", "knee_y", "knee_visibility",
                    "ankle_x", "ankle_y", "ankle_visibility",
                ])
                if camera:
                    video_path = self.output_dir / f"{stem}.mp4"
                    writer = cv2.VideoWriter(
                        str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
                    )
                    if not writer.isOpened():
                        raise RuntimeError("无法创建 MP4 录像；请选择可写入的目录。")

            analyzer = SquatAnalyzer()
            frame_index = 0
            processed_count = 0
            skipped = 0
            first_read_ns = 0
            with mp.solutions.pose.Pose(
                model_complexity=1,
                min_detection_confidence=0.6,
                min_tracking_confidence=0.6,
            ) as pose:
                replay_started = time.perf_counter()
                if camera:
                    camera_reader = LatestCamera(capture, writer, self.stop_requested)
                    camera_reader.start()
                while not self.stop_requested.is_set():
                    if camera:
                        sample = camera_reader.next_after(frame_index - 1)
                        if sample is None:
                            if camera_reader.done:
                                if camera_reader.error is not None:
                                    raise RuntimeError(
                                        f"摄像头读取失败：{camera_reader.error}"
                                    ) from camera_reader.error
                                if processed_count == 0:
                                    raise RuntimeError("摄像头没有返回画面。")
                                break
                            continue
                        camera_index, frame, read_ns = sample
                        skipped += max(0, camera_index - frame_index)
                        frame_index = camera_index
                    else:
                        due = replay_started + frame_index / fps
                        wait = due - time.perf_counter()
                        if wait > 0 and self.stop_requested.wait(wait):
                            break
                        while time.perf_counter() - replay_started > (frame_index + 2) / fps:
                            if not capture.grab():
                                break
                            frame_index += 1
                            skipped += 1
                        ok, frame = capture.read()
                        if not ok:
                            break
                        read_ns = time.perf_counter_ns()
                    if not first_read_ns:
                        first_read_ns = read_ns
                    source_time_ms = (
                        (read_ns - first_read_ns) / 1_000_000 if camera
                        else capture.get(cv2.CAP_PROP_POS_MSEC)
                    )
                    if not camera and source_time_ms <= 0:
                        source_time_ms = frame_index * 1000 / fps
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pose_result = pose.process(rgb)
                    landmarks = (
                        pose_result.pose_landmarks.landmark
                        if pose_result.pose_landmarks is not None else None
                    )
                    result = analyzer.update(landmarks, read_ns // 1_000_000)
                    inference_ms = (time.perf_counter_ns() - read_ns) / 1_000_000
                    if csv_writer is not None:
                        side_indices = SquatAnalyzer._SIDES.get(result.side, ())
                        selected_points = (
                            [landmarks[index] for index in side_indices]
                            if landmarks is not None else []
                        )
                        point_values = [
                            value
                            for point in selected_points
                            for value in (point.x, point.y, point.visibility)
                        ]
                        point_values.extend([""] * (12 - len(point_values)))
                        csv_writer.writerow([
                            frame_index, round(source_time_ms, 1), read_ns,
                            round(inference_ms, 2), result.phase, result.repetitions, result.side,
                            round(result.confidence, 3), result.knee_angle, result.hip_angle,
                            result.trunk_lean, result.hip_below_knee, result.feedback,
                            *point_values,
                        ])
                    if self.reserve_frame():
                        if pose_result.pose_landmarks is not None:
                            mp.solutions.drawing_utils.draw_landmarks(
                                frame, pose_result.pose_landmarks,
                                mp.solutions.pose.POSE_CONNECTIONS,
                            )
                        preview = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        image = QImage(
                            preview.data, width, height, preview.strides[0],
                            QImage.Format.Format_RGB888,
                        ).copy()
                        self.frame_ready.emit(image, result, read_ns, inference_ms)
                    processed_count += 1
                    frame_index += 1
            if csv_file is not None:
                completion_message = (
                    f"已处理 {processed_count} 帧，跳过 {skipped} 帧；分析记录：{csv_path}"
                )
            else:
                completion_message = f"已处理 {processed_count} 帧，跳过 {skipped} 帧。"
        finally:
            if camera_reader is not None:
                self.stop_requested.set()
                camera_reader.thread.join(timeout=2)
            capture.release()
            if camera_reader is not None and camera_reader.thread.is_alive():
                camera_reader.thread.join(timeout=2)
            if camera_reader is not None and camera_reader.frames_read > 1:
                elapsed = camera_reader.last_read_ns - camera_reader.first_read_ns
                actual_fps = (camera_reader.frames_read - 1) * 1_000_000_000 / max(1, elapsed)
                completion_message += f"；设备实测 {actual_fps:.1f} FPS"
            if writer is not None:
                writer.release()
            if csv_file is not None:
                csv_file.close()
        self.completed.emit(completion_message)


class MotionCapturePage(QWidget):
    """Single-computer squat baseline with live, on-screen feedback."""

    def __init__(self) -> None:
        super().__init__()
        self.worker: MotionCaptureWorker | None = None
        self.latencies: deque[float] = deque(maxlen=1800)
        self.arrivals: deque[int] = deque(maxlen=1800)
        self.valid_frames = 0
        self.total_frames = 0
        self.last_image: QImage | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        description = QLabel(
            "M7 · 单机视频深蹲分析。请将摄像头固定在侧面，让全身和脚部进入画面。"
            "反馈显示在画面下方；当前使用屏幕文字提示。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        controls = QHBoxLayout()
        self.camera_index = QSpinBox()
        self.camera_index.setRange(0, 9)
        self.camera_index.setToolTip("摄像头编号；内置摄像头通常为 0")
        self.camera_fps = QSpinBox()
        self.camera_fps.setRange(15, 60)
        self.camera_fps.setSingleStep(15)
        self.camera_fps.setValue(30)
        self.camera_fps.setToolTip("请求的采集帧率；实际帧率取决于摄像头")
        self.camera_button = QPushButton("启动摄像头")
        self.video_button = QPushButton("打开视频回放")
        self.stop_button = QPushButton("停止分析")
        self.stop_button.setEnabled(False)
        controls.addWidget(QLabel("摄像头编号"))
        controls.addWidget(self.camera_index)
        controls.addWidget(QLabel("请求 FPS"))
        controls.addWidget(self.camera_fps)
        controls.addWidget(self.camera_button)
        controls.addWidget(self.video_button)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        layout.addLayout(controls)

        self.save_checkbox = QCheckBox("保存本次分析记录（摄像头还会保存 MP4）")
        layout.addWidget(self.save_checkbox)
        self.preview = QLabel("选择摄像头或视频开始分析")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(480, 320)
        self.preview.setStyleSheet("background: #20242a; color: white;")
        layout.addWidget(self.preview, 1)
        self.feedback = QLabel("准备深蹲")
        self.feedback.setWordWrap(True)
        self.feedback.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self.feedback)
        self.measurements = QLabel("动作：—  次数：0  膝角：—  躯干倾角：—")
        layout.addWidget(self.measurements)
        self.performance = QLabel("帧处理延迟：—  有效关键点：—")
        self.performance.setWordWrap(True)
        layout.addWidget(self.performance)
        self.status = QLabel("等待视频输入。角度与深度均为单一侧面视角的估计值。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.camera_button.clicked.connect(lambda: self._start(self.camera_index.value()))
        self.video_button.clicked.connect(self.open_video)
        self.stop_button.clicked.connect(self.stop)

    def open_video(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择深蹲视频", "", "视频文件 (*.mp4 *.mov *.avi *.mkv);;所有文件 (*)"
        )
        if path:
            self._start(path)

    def _start(self, source: int | str) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        output_dir = None
        if self.save_checkbox.isChecked():
            selected = QFileDialog.getExistingDirectory(self, "选择分析记录保存目录")
            if not selected:
                return
            output_dir = Path(selected)
        self.latencies.clear()
        self.arrivals.clear()
        self.valid_frames = 0
        self.total_frames = 0
        self.camera_button.setEnabled(False)
        self.video_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setText("正在加载姿态模型并打开视频……")
        self.worker = MotionCaptureWorker(source, output_dir, self.camera_fps.value())
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status_changed.connect(self.status.setText)
        self.worker.failed.connect(self.status.setText)
        self.worker.completed.connect(self.status.setText)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.status.setText("正在停止视频分析……")

    def _on_finished(self) -> None:
        self.camera_button.setEnabled(True)
        self.video_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def on_frame(
        self, image: QImage, result: SquatResult, read_ns: int, inference_ms: float
    ) -> None:
        self.last_image = image
        self._show_image()
        self.total_frames += 1
        if result.confidence >= 0.60:
            self.valid_frames += 1
        arrival_ns = time.perf_counter_ns()
        self.arrivals.append(arrival_ns)
        self.latencies.append((arrival_ns - read_ns) / 1_000_000)
        phase_name = {
            "ready": "准备", "descending": "下蹲", "ascending": "起身",
            "tracking_lost": "未识别",
        }[result.phase]
        knee = f"{result.knee_angle:.0f}°" if result.knee_angle is not None else "—"
        trunk = f"{result.trunk_lean:.0f}°" if result.trunk_lean is not None else "—"
        self.feedback.setText(result.feedback)
        self.measurements.setText(
            f"动作：{phase_name}  次数：{result.repetitions}  "
            f"膝角：{knee}  躯干倾角：{trunk}  侧面：{result.side or '—'}"
        )
        valid = self.valid_frames * 100 / self.total_frames
        processed_fps = (
            (len(self.arrivals) - 1) * 1_000_000_000
            / max(1, self.arrivals[-1] - self.arrivals[0])
            if len(self.arrivals) > 1 else 0.0
        )
        self.performance.setText(
            f"读帧至界面 p50/p95/p99：{percentile(self.latencies, .50):.0f}/"
            f"{percentile(self.latencies, .95):.0f}/"
            f"{percentile(self.latencies, .99):.0f} ms；"
            f"本帧推理：{inference_ms:.0f} ms；处理：{processed_fps:.1f} FPS；"
            f"有效关键点：{valid:.1f}%"
        )
        sender = self.sender()
        if isinstance(sender, MotionCaptureWorker):
            sender.ack_frame()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().resizeEvent(event)
        self._show_image()

    def _show_image(self) -> None:
        if self.last_image is not None:
            self.preview.setPixmap(
                QPixmap.fromImage(self.last_image).scaled(
                    self.preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def shutdown(self) -> bool:
        if self.worker is None or not self.worker.isRunning():
            return True
        self.worker.stop()
        return self.worker.wait(4000)
