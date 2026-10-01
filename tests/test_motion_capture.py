import time
from threading import Event

import cv2
import numpy as np
from PySide6.QtWidgets import QApplication

from ai_device_bridge.motion_capture import LatestCamera, MotionCaptureWorker


def test_camera_reader_keeps_newest_frame_when_analysis_is_slow() -> None:
    class Camera:
        index = 0

        def read(self):
            if self.index >= 10:
                return False, None
            self.index += 1
            return True, self.index

    reader = LatestCamera(Camera(), None, Event())
    reader.start()
    reader.thread.join(timeout=2)
    assert reader.done
    assert reader.next_after(-1)[1] == 10


def test_preview_has_only_one_queued_frame() -> None:
    worker = MotionCaptureWorker(0)
    assert worker.reserve_frame()
    assert not worker.reserve_frame()
    worker.ack_frame()
    assert worker.reserve_frame()


def test_replay_runs_pose_model_and_writes_timestamped_csv(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    application = QApplication.instance() or QApplication([])
    video_path = tmp_path / "short.mp4"
    video = cv2.VideoWriter(
        str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 15, (320, 240)
    )
    assert video.isOpened()
    for _ in range(6):
        video.write(np.zeros((240, 320, 3), dtype=np.uint8))
    video.release()

    worker = MotionCaptureWorker(str(video_path), tmp_path)
    frames = []
    completed = []
    failed = []
    worker.frame_ready.connect(lambda _image, result, _time, _cost: frames.append(result))
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.start()
    until = time.monotonic() + 20
    while worker.isRunning() and time.monotonic() < until:
        application.processEvents()
        time.sleep(.01)
    assert worker.wait(1000)
    application.processEvents()

    assert not failed
    assert completed and "已处理" in completed[0]
    assert len(frames) > 0
    assert all(frame.phase == "tracking_lost" for frame in frames)
    csv_files = list(tmp_path.glob("squat-*.csv"))
    assert len(csv_files) == 1
    assert "read_time_ns" in csv_files[0].read_text(encoding="utf-8-sig")
