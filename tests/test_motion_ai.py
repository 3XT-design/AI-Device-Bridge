import json
import time

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from ai_device_bridge.motion_capture import MotionCapturePage
from ai_device_bridge.services.motion_ai import (
    MotionAIError,
    SquatSessionCollector,
    analyze_squat_with_ollama,
)
from ai_device_bridge.services.squat_analysis import SquatResult


def result(*, reps: int = 0, confidence: float = 0.9) -> SquatResult:
    return SquatResult(
        "ascending", reps, "left", confidence, 92.0, 80.0, 34.0, True,
        "起身中",
    )


def populated_summary():
    collector = SquatSessionCollector()
    for index in range(12):
        collector.add(result(reps=1 if index == 11 else 0), index * 100_000_000)
    return collector.summary()


def test_collector_counts_all_processed_frames_and_valid_measurements() -> None:
    collector = SquatSessionCollector()
    collector.add(result(), 1_000_000_000)
    collector.add(result(confidence=0), 2_000_000_000)
    collector.add(result(reps=1), 3_000_000_000)
    summary = collector.summary()
    assert summary.processed_frames == 3
    assert summary.valid_frames == 2
    assert summary.repetitions == 1
    assert summary.duration_seconds == 2
    assert summary.depth_observed_frames == 2


def test_ollama_receives_only_statistics_and_validates_reply(monkeypatch) -> None:
    sent = []

    def fake_post(url, **kwargs):
        sent.append((url, kwargs))
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps({
                "观察": "完成一次动作", "下一组建议": "继续保持侧面拍摄", "局限": "二维估计"
            }, ensure_ascii=False)}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    report, elapsed = analyze_squat_with_ollama(
        populated_summary(), "http://127.0.0.1:11434", "qwen2.5:7b"
    )
    assert "观察：完成一次动作" in report
    assert elapsed >= 0
    url, options = sent[0]
    assert url == "http://127.0.0.1:11434/api/chat"
    assert options["trust_env"] is False
    assert options["json"]["model"] == "qwen2.5:7b"
    payload = json.loads(options["json"]["messages"][1]["content"])
    assert payload["repetitions"] == 1
    assert "video" not in payload and "landmarks" not in payload


def test_motion_ai_refuses_remote_destination_and_insufficient_pose(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: pytest.fail("network called"))
    with pytest.raises(MotionAIError, match="本机"):
        analyze_squat_with_ollama(populated_summary(), "http://192.168.1.3:11434", "model")
    with pytest.raises(MotionAIError, match="地址无效"):
        analyze_squat_with_ollama(populated_summary(), "http://127.0.0.1:bad", "model")
    with pytest.raises(MotionAIError, match="不足"):
        analyze_squat_with_ollama(
            SquatSessionCollector().summary(), "http://127.0.0.1:11434", "model"
        )


def test_motion_page_runs_ai_without_blocking_gui(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])

    class Settings:
        values = {"ollama_model": "installed:latest"}

        def get_setting(self, key, default=""):
            return self.values.get(key, default)

        def save_setting(self, key, value):
            self.values[key] = value

    monkeypatch.setattr(
        "ai_device_bridge.motion_capture.analyze_squat_with_ollama",
        lambda _summary, _url, _model: ("观察：测试报告", 0.1),
    )
    page = MotionCapturePage(Settings())
    page._on_summary(populated_summary())
    assert page.ai_button.isEnabled()
    page.analyze_session()
    deadline = time.monotonic() + 3
    while page.ai_worker.isRunning() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    application.processEvents()
    assert page.ai_worker.wait(1000)
    assert "观察：测试报告" in page.ai_result.toPlainText()
    assert page.ai_button.isEnabled()
