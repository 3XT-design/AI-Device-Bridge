import time
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from PySide6.QtWidgets import QApplication, QMessageBox

from ai_device_bridge import __version__
from ai_device_bridge.app import MainWindow
from ai_device_bridge.domain.models import DeviceProfile, SourceMode
from ai_device_bridge.infrastructure.sqlite_repository import SCHEMA_VERSION, SQLiteRepository
from ai_device_bridge.services.diagnostics import DiagnosticLog
from ai_device_bridge.services.file_catalog import FileCandidate
from ai_device_bridge.services.file_inspection import inspect_file
from ai_device_bridge.services.intent_planning import IntentSearchResult, parse_transfer_intent


def test_reviewed_candidate_creates_traceable_ai_plan_without_sending(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    now = datetime.now(UTC)
    local = repository.get_or_create_local_device("Laptop", "Windows")
    peer = DeviceProfile(uuid4(), "Desktop", "fingerprint", "Windows", now)
    repository.save_peer(peer, "192.168.1.2:8765")
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "report.txt"
    source.write_text("body", encoding="utf-8")
    candidate = FileCandidate(
        "F00001", str(source), "report.txt", "report.txt",
        source.stat().st_size, datetime.fromtimestamp(source.stat().st_mtime, UTC),
    )
    request = "找报告发给Desktop"
    result = IntentSearchResult(
        parse_transfer_intent(request, ("Desktop",)),
        (candidate,), (candidate,), "", "local", request, str(root),
    )
    window = MainWindow(
        SimpleNamespace(stop=lambda: None), repository, local.device_id, "fingerprint", "token"
    )
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == [
        "设备与配对", "发送文件", "AI 查找", "动作分析", "历史与诊断",
    ]
    window.tabs.setCurrentIndex(2)
    window.file_query.setText(request)
    window.authorized_root.setText(str(root))
    window.on_file_search_succeeded(result)
    assert window.selected_peer_id == peer.device_id
    assert window.candidate_list.count() == 1
    window.inspected_file = inspect_file(source)
    window.pending_ai_candidate = (result, candidate)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window.create_transfer_plan(from_ai_candidate=True)
    assert window.tabs.currentWidget() is window.send_page
    assert window.plan_list.currentItem() is not None
    assert window.selected_peer_label.text() == "接收设备：Desktop"

    plans = repository.list_plans()
    assert len(plans) == 1
    assert plans[0].source_mode is SourceMode.NATURAL_LANGUAGE
    evidence = repository.get_ai_plan_evidence(plans[0].plan_id)
    assert evidence.request_text == request
    assert evidence.candidates == (("F00001", "report.txt"),)
    assert evidence.selected_candidate_id == "F00001"
    assert repository.list_tasks() == []
    outside = tmp_path / "outside.txt"
    outside.write_text("body", encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)
    window.plan_list.setCurrentRow(0)
    window.send_selected_plan()
    assert "离开授权目录" in window.plan_status.text()
    assert repository.list_tasks() == []
    window.history_timer.stop()
    window.close()
    assert application is not None


def test_m5_guidance_search_and_copied_diagnostics(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    local = repository.get_or_create_local_device("Laptop", "Windows")
    diagnostics = DiagnosticLog(tmp_path, ("sensitive-receive-token",))
    window = MainWindow(
        SimpleNamespace(stop=lambda: None), repository, local.device_id,
        "fingerprint", "sensitive-receive-token", diagnostics,
    )
    assert not window.guide_label.isHidden()
    window.dismiss_guide()
    assert repository.get_setting("onboarding_done") == "1"
    assert window.guide_label.isHidden()
    assert "授权目录" in window.search_files_button.toolTip()
    window._record_issue("CONNECTION_FAILED", "token=sensitive-receive-token")
    window.copy_diagnostics()
    copied = application.clipboard().text()
    assert __version__ in copied
    assert "sensitive-receive-token" not in copied
    assert f"数据库版本：{SCHEMA_VERSION}" in copied
    window.tabs.setCurrentIndex(1)
    window.plan_filter.setText("report")
    window.tabs.setCurrentIndex(4)
    window.tabs.setCurrentIndex(1)
    assert window.plan_filter.text() == "report"
    window.choose_peer_tab_button.click()
    assert window.tabs.currentIndex() == 0
    window.history_timer.stop()
    window.close()


def test_receiver_approval_dialog_decides_pending_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    local = repository.get_or_create_local_device("Receiver", "Windows")
    sender = DeviceProfile(uuid4(), "Sender", "fingerprint", "Windows", datetime.now(UTC))
    repository.save_peer(sender, "127.0.0.1:8765", certificate_pem="certificate")
    repository.get_or_create_receive_grant(sender.device_id)
    request = repository.create_receive_request(
        sender.device_id, "report.txt", "Inbox", 5, "a" * 64
    )
    window = MainWindow(
        SimpleNamespace(stop=lambda: None, is_running=True), repository,
        local.device_id, "fingerprint", "unused",
    )
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window.review_pending_receives()
    assert repository.get_receive_request(request.request_id).status == "approved"
    assert "已同意" in window.local_status.text()
    window.history_timer.stop()
    window.receive_timer.stop()
    window.close()
    assert application is not None


def test_local_service_start_reports_progress_without_blocking_ui(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    local = repository.get_or_create_local_device("Laptop", "Windows")
    entered = Event()
    release = Event()

    class SlowServer:
        is_running = False

        def start(self):
            entered.set()
            release.wait(timeout=2)
            self.is_running = True

        def stop(self):
            self.is_running = False

    window = MainWindow(SlowServer(), repository, local.device_id, "fingerprint", "")
    window.start_local_service()
    assert "正在启动" in window.local_status.text()
    assert not window.start_button.isEnabled()
    assert entered.wait(timeout=1)
    release.set()
    window.start_worker.wait(2000)
    for _ in range(30):
        application.processEvents()
        if window.stop_button.isEnabled():
            break
        time.sleep(0.01)
    assert window.stop_button.isEnabled()
    assert "已启动" in window.local_status.text()
    window.history_timer.stop()
    window.receive_timer.stop()
    window.close()
