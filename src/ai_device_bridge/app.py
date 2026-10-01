"""Desktop application entry point."""

import os
import platform
import socket
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ai_device_bridge import __version__
from ai_device_bridge.api.health import HealthResponse
from ai_device_bridge.domain.models import (
    AIPlanEvidence,
    DeviceProfile,
    PlanStatus,
    SourceMode,
    TransferPlan,
    TransferStatus,
)
from ai_device_bridge.infrastructure.node_server import NodeServer
from ai_device_bridge.infrastructure.sqlite_repository import SQLiteRepository
from ai_device_bridge.services.diagnostics import DiagnosticLog
from ai_device_bridge.services.file_catalog import (
    FileCandidate,
    FileCatalogError,
)
from ai_device_bridge.services.file_inspection import FileInspection, inspect_file
from ai_device_bridge.services.file_transfer import FileTransferError, TransferCancelled, send_file
from ai_device_bridge.services.intent_planning import (
    IntentSearchResult,
    is_safe_relative_directory,
    search_intent,
)
from ai_device_bridge.services.peer_health import PeerHealthError, check_peer_health
from ai_device_bridge.services.transfer_journal import TransferJournal


class HealthCheckWorker(QThread):
    """Run a peer health check without blocking the Qt event loop."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self, address: str, expected_fingerprint: str | None = None, certificate_pem: str = ""
    ) -> None:
        super().__init__()
        self.address = address
        self.expected_fingerprint = expected_fingerprint
        self.certificate_pem = certificate_pem

    def run(self) -> None:
        try:
            self.succeeded.emit(
                check_peer_health(
                    self.address,
                    expected_fingerprint=self.expected_fingerprint,
                    trusted_certificate_pem=self.certificate_pem or None,
                )
            )
        except PeerHealthError as error:
            self.failed.emit(str(error))


class FileInspectionWorker(QThread):
    """Calculate a file's SHA-256 without blocking the desktop interface."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def run(self) -> None:
        try:
            self.succeeded.emit(inspect_file(self.path))
        except ValueError as error:
            self.failed.emit(str(error))


class NodeServerStartWorker(QThread):
    """Start TLS service without freezing the desktop interface."""

    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, node_server: NodeServer) -> None:
        super().__init__()
        self.node_server = node_server

    def run(self) -> None:
        try:
            self.node_server.start()
        except (RuntimeError, OSError, ValueError) as error:
            self.failed.emit(str(error))
        else:
            self.succeeded.emit()


class FileTransferWorker(QThread):
    """Send a confirmed plan without blocking the Qt event loop."""

    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    phase_changed = Signal(str)
    progress_changed = Signal(int)

    def __init__(
        self,
        plan: TransferPlan,
        address: str,
        cert_pem: str,
        token: str,
        repository: SQLiteRepository,
        require_receiver_approval: bool = True,
    ) -> None:
        super().__init__()
        self.plan = plan
        self.address = address
        self.cert_pem = cert_pem
        self.token = token
        self.repository = repository
        self.require_receiver_approval = require_receiver_approval
        self.cancel_event = Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        journal = None
        try:
            journal = TransferJournal.begin_sent(self.repository, self.plan)
            last_saved_bytes = 0

            def phase_changed(phase: str) -> None:
                if phase == "waiting_receiver":
                    journal.transition(TransferStatus.WAITING_RECEIVER)
                elif phase == "transferring":
                    journal.transition(TransferStatus.TRANSFERRING)
                elif phase == "verifying":
                    journal.transition(TransferStatus.VERIFYING)
                self.phase_changed.emit(phase)

            def progress_changed(sent: int) -> None:
                nonlocal last_saved_bytes
                persist = (
                    sent == self.plan.file_size_bytes or sent - last_saved_bytes >= 4 * 1024 * 1024
                )
                journal.progress(sent, persist=persist)
                if persist:
                    last_saved_bytes = sent
                self.progress_changed.emit(sent)

            result = send_file(
                self.plan.source_path,
                self.address,
                self.cert_pem,
                self.token,
                self.plan.file_name,
                self.plan.target_directory,
                self.plan.file_size_bytes,
                self.plan.expected_sha256,
                on_phase=phase_changed,
                on_progress=progress_changed,
                cancel_event=self.cancel_event,
                require_receiver_approval=self.require_receiver_approval,
            )
            journal.transition(TransferStatus.COMPLETED)
            self.succeeded.emit(result)
        except TransferCancelled as error:
            if journal is not None:
                try:
                    journal.transition(TransferStatus.CANCELLED)
                except sqlite3.Error:
                    self.failed.emit("传输已中止，但本机历史保存失败；请检查接收端文件。")
                    return
            self.cancelled.emit(str(error))
        except (FileTransferError, sqlite3.Error, OSError, ValueError) as error:
            if journal is not None and journal.task.status not in {
                TransferStatus.COMPLETED,
                TransferStatus.FAILED,
            }:
                try:
                    if getattr(error, "code", "") == "receiver_rejected":
                        journal.transition(TransferStatus.REJECTED)
                    else:
                        journal.fail(getattr(error, "code", "local_error"), str(error))
                except (sqlite3.Error, ValueError):
                    pass
            self.failed.emit(str(error))


class FileSearchWorker(QThread):
    """Build a constrained intent draft without blocking the desktop interface."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self, root: str, query: str, peer_names: tuple[str, ...], ollama_url: str, model: str
    ) -> None:
        super().__init__()
        self.root = root
        self.query = query
        self.peer_names = peer_names
        self.ollama_url = ollama_url
        self.model = model

    def run(self) -> None:
        try:
            self.succeeded.emit(
                search_intent(
                    self.root, self.query, self.peer_names, self.ollama_url, self.model
                )
            )
        except FileCatalogError as error:
            self.failed.emit(str(error))
        except (OSError, ValueError) as error:
            self.failed.emit(f"读取授权目录失败：{error}")


class MainWindow(QMainWindow):
    """Desktop workflows for pairing, file plans, receiving and history."""

    def __init__(
        self,
        node_server: NodeServer,
        repository: SQLiteRepository,
        local_device_id: UUID,
        local_fingerprint: str,
        receive_token: str,
        diagnostics: DiagnosticLog | None = None,
    ) -> None:
        super().__init__()
        self.node_server = node_server
        self.repository = repository
        self.local_device_id = local_device_id
        self.local_fingerprint = local_fingerprint
        self.receive_token = receive_token
        self.selected_receive_grant = ""
        self.reviewing_receive_request = False
        self.diagnostics = diagnostics
        self.last_issue = ""
        self.health_worker: HealthCheckWorker | None = None
        self.start_worker: NodeServerStartWorker | None = None
        self.file_worker: FileInspectionWorker | None = None
        self.transfer_worker: FileTransferWorker | None = None
        self.search_worker: FileSearchWorker | None = None
        self.active_transfer_plan: TransferPlan | None = None
        self.pending_ai_candidate_plan = False
        self.pending_ai_candidate: tuple[IntentSearchResult, FileCandidate] | None = None
        self.search_result: IntentSearchResult | None = None
        self.last_checked_peer: HealthResponse | None = None
        self.inspected_file: FileInspection | None = None
        self.selected_peer_id: UUID | None = None
        self.setWindowTitle(f"AI Device Bridge v{__version__}")
        self.resize(760, 760)

        title = QLabel("AI Device Bridge")
        title.setObjectName("projectTitle")
        intro = QLabel("两端确认配对与逐次接收后，通过 HTTPS 传输并校验 SHA-256。")
        intro.setWordWrap(True)
        self.guide_label = QLabel(
            "首次使用：① 两台电脑在“设备与配对”页互相检查地址、核对 TLS 指纹并保存配对；"
            "② 接收电脑选中发送设备，复制该设备专用授权码到发送电脑；"
            "③ 在“发送文件”页确认计划并发送，接收电脑同意后才会上传。"
            "结果在“历史与诊断”页查看，AI 候选在“AI 查找”页审核。"
        )
        self.guide_label.setWordWrap(True)
        self.dismiss_guide_button = QPushButton("已了解，隐藏指引")
        show_guide = repository.get_setting("onboarding_done") != "1"
        self.guide_label.setVisible(show_guide)
        self.dismiss_guide_button.setVisible(show_guide)
        self.dismiss_guide_button.clicked.connect(self.dismiss_guide)

        self.local_status = QLabel("本机节点服务未启动")
        self.local_fingerprint_label = QLabel(
            f"本机 TLS 指纹（首次配对请与对端屏幕核对）：\n{local_fingerprint}"
        )
        self.local_fingerprint_label.setWordWrap(True)
        self.receive_token_field = QLineEdit()
        self.receive_token_field.setReadOnly(True)
        self.receive_token_field.setPlaceholderText("选中已配对的发送设备后显示专用接收授权码")
        self.copy_token_button = QPushButton("复制所选设备授权码")
        self.copy_token_button.setEnabled(False)
        self.copy_token_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.selected_receive_grant)
        )
        self.rotate_grant_button = QPushButton("轮换授权码")
        self.rotate_grant_button.setEnabled(False)
        self.rotate_grant_button.clicked.connect(self.rotate_selected_receive_grant)
        self.start_button = QPushButton("启动本机服务")
        self.stop_button = QPushButton("停止本机服务")
        self.stop_button.setEnabled(False)
        local_controls = QHBoxLayout()
        local_controls.addWidget(self.start_button)
        local_controls.addWidget(self.stop_button)
        local_controls.addStretch()

        self.peer_address = QLineEdit()
        self.peer_address.setPlaceholderText("例如 192.168.1.20:8765")
        self.check_button = QPushButton("检查设备")
        self.pair_button = QPushButton("确认并保存配对")
        self.pair_button.setEnabled(False)
        peer_controls = QHBoxLayout()
        peer_controls.addWidget(self.peer_address)
        peer_controls.addWidget(self.check_button)
        peer_controls.addWidget(self.pair_button)
        self.peer_status = QLabel("尚未检查目标设备")
        self.peer_status.setWordWrap(True)

        self.peer_list = QListWidget()
        self.peer_list.setMaximumHeight(200)
        self.empty_peer_label = QLabel("暂无已配对设备。检查设备后，点击“确认并保存配对”添加。")
        self.paired_devices_label = QLabel("已配对设备（0）")
        self.remove_peer_button = QPushButton("解除配对")
        peer_list_controls = QHBoxLayout()
        peer_list_controls.addWidget(self.remove_peer_button)
        peer_list_controls.addStretch()

        note = QLabel(
            "首次配对时请通过可信渠道核对双方显示的 TLS 指纹。"
            "两台电脑都要保存对方为已配对设备。接收电脑选中发送设备，"
            "将该设备专用授权码复制到发送电脑。每次发送还需接收电脑确认。"
        )
        note.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addWidget(self.guide_label)
        layout.addWidget(self.dismiss_guide_button)
        layout.addWidget(self.local_status)
        layout.addWidget(self.local_fingerprint_label)
        token_controls = QHBoxLayout()
        token_controls.addWidget(QLabel("所选设备接收授权码"))
        token_controls.addWidget(self.receive_token_field, 1)
        token_controls.addWidget(self.copy_token_button)
        token_controls.addWidget(self.rotate_grant_button)
        layout.addLayout(token_controls)
        token_hint = QLabel(
            "若当前使用设备为接收端，则该码需填写在发送端的(发送文件页面-接收设备授权码处)"
        )
        token_hint.setWordWrap(True)
        hint_font = token_hint.font()
        hint_font.setPointSizeF(max(8.0, hint_font.pointSizeF() - 1.0))
        token_hint.setFont(hint_font)
        layout.addWidget(token_hint)
        layout.addLayout(local_controls)
        layout.addSpacing(20)
        layout.addWidget(QLabel("目标设备地址"))
        layout.addLayout(peer_controls)
        layout.addWidget(self.peer_status)
        layout.addSpacing(10)
        layout.addWidget(self.paired_devices_label)
        layout.addWidget(self.empty_peer_label)
        layout.addWidget(self.peer_list)
        layout.addLayout(peer_list_controls)
        layout.addWidget(note)
        layout.addStretch()

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)
        self._add_page("设备与配对", layout)

        self.start_button.clicked.connect(self.start_local_service)
        self.stop_button.clicked.connect(self.stop_local_service)
        self.check_button.clicked.connect(self.check_peer)
        self.pair_button.clicked.connect(self.save_current_peer)
        self.remove_peer_button.clicked.connect(self.remove_selected_peer)
        self.peer_list.itemClicked.connect(self.select_peer)
        self.peer_address.textChanged.connect(self.invalidate_checked_peer)
        self.refresh_peer_list()

        layout = QVBoxLayout()
        self.choose_file_button = QPushButton("选择文件并计算校验值")
        self.file_status = QLabel("尚未选择文件")
        self.file_status.setWordWrap(True)
        self.file_hash = QLabel("SHA-256：—")
        self.file_hash.setWordWrap(True)
        self.target_directory = QLineEdit("AI Device Bridge Inbox")
        self.create_plan_button = QPushButton("预览并确认传输计划")
        self.create_plan_button.setEnabled(False)
        self.preparation_hint = QLabel()
        self.preparation_hint.setWordWrap(True)
        self.selected_peer_label = QLabel("接收设备：未选择")
        self.choose_peer_tab_button = QPushButton("前往“设备与配对”选择接收设备")
        self.choose_peer_tab_button.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        self.plan_list = QListWidget()
        self.plan_list.setMaximumHeight(230)
        self.plan_filter = QLineEdit()
        self.plan_filter.setPlaceholderText("搜索最近 500 条计划：文件名、设备或状态")
        self.plan_status = QLabel(
            "选择已确认计划后发送。接收文件保存在本机应用数据目录的 Received 文件夹。"
        )
        self.plan_status.setWordWrap(True)
        self.peer_token = QLineEdit()
        self.peer_token.setPlaceholderText("粘贴所选接收设备显示的授权码")
        self.peer_token.textChanged.connect(
            lambda value: self.diagnostics.add_secret(value)
            if self.diagnostics is not None and len(value) >= 20 else None
        )
        self.save_peer_token_button = QPushButton("保存授权码")
        self.save_peer_token_button.setEnabled(False)
        self.send_plan_button = QPushButton("发送所选计划")
        self.send_plan_button.setEnabled(False)
        self.cancel_transfer_button = QPushButton("取消传输")
        self.cancel_transfer_button.setEnabled(False)
        self.transfer_progress = QProgressBar()
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_progress.setFormat("尚未开始传输")
        self.transfer_history = QListWidget()
        self.history_filter = QLineEdit()
        self.history_filter.setPlaceholderText("搜索最近 2000 条传输历史：文件名、方向或状态")
        self.transfer_history.setMaximumHeight(160)
        self.prune_history_button = QPushButton("清理 30 天前的已结束历史")
        self.delete_plan_button = QPushButton("删除所选计划")
        self.delete_plan_button.setEnabled(False)

        layout.addWidget(QLabel("选择文件、确认计划并发送"))
        layout.addWidget(self.selected_peer_label)
        layout.addWidget(self.choose_peer_tab_button)
        file_controls = QHBoxLayout()
        file_controls.addWidget(self.choose_file_button)
        file_controls.addWidget(self.file_status, 1)
        layout.addLayout(file_controls)
        layout.addWidget(self.file_hash)
        layout.addWidget(QLabel("接收端目标目录"))
        layout.addWidget(self.target_directory)
        layout.addWidget(self.create_plan_button)
        layout.addWidget(self.preparation_hint)
        layout.addWidget(QLabel("最近传输计划"))
        layout.addWidget(self.plan_filter)
        layout.addWidget(self.plan_list)
        peer_token_controls = QHBoxLayout()
        peer_token_controls.addWidget(QLabel("接收设备授权码"))
        peer_token_controls.addWidget(self.peer_token, 1)
        peer_token_controls.addWidget(self.save_peer_token_button)
        layout.addLayout(peer_token_controls)
        plan_action_controls = QHBoxLayout()
        plan_action_controls.addWidget(self.send_plan_button)
        plan_action_controls.addWidget(self.cancel_transfer_button)
        plan_action_controls.addWidget(self.delete_plan_button)
        layout.addLayout(plan_action_controls)
        layout.addWidget(self.transfer_progress)
        layout.addWidget(self.plan_status)

        self.choose_file_button.clicked.connect(self.choose_file)
        self.create_plan_button.clicked.connect(self.create_transfer_plan)
        self.plan_filter.textChanged.connect(self.refresh_plan_list)
        self.refresh_plan_list()
        self.save_peer_token_button.clicked.connect(self.save_selected_peer_token)
        self.send_plan_button.clicked.connect(self.send_selected_plan)
        self.cancel_transfer_button.clicked.connect(self.cancel_active_transfer)
        self.delete_plan_button.clicked.connect(self.delete_selected_plan)
        self.plan_list.itemSelectionChanged.connect(self.update_send_button_state)
        self.history_filter.textChanged.connect(self.refresh_transfer_history)
        self.prune_history_button.clicked.connect(self.prune_old_history)
        self.refresh_transfer_history()
        self.history_timer = QTimer(self)
        self.history_timer.setInterval(3000)
        self.history_timer.timeout.connect(self.refresh_transfer_history)
        self.history_timer.start()
        self.receive_timer = QTimer(self)
        self.receive_timer.setInterval(1000)
        self.receive_timer.timeout.connect(self.review_pending_receives)
        self.receive_timer.start()
        layout.addStretch()
        self.send_page = self._add_page("发送文件", layout)

        layout = QVBoxLayout()
        self.authorized_root = QLineEdit(repository.get_setting("authorized_root"))
        self.authorized_root.setReadOnly(True)
        self.choose_root_button = QPushButton("选择授权目录")
        self.ollama_url = QLineEdit(repository.get_setting("ollama_url", "http://127.0.0.1:11434"))
        self.ollama_model = QLineEdit(repository.get_setting("ollama_model", "qwen2.5:3b"))
        self.save_ai_settings_button = QPushButton("保存 AI 设置")
        self.file_query = QLineEdit()
        self.file_query.setPlaceholderText("例如：找最近修改的深度学习论文并准备发给台式机")
        self.search_files_button = QPushButton("AI 查找候选文件")
        self.search_files_button.setEnabled(False)
        self.search_hint = QLabel()
        self.search_hint.setWordWrap(True)
        self.candidate_list = QListWidget()
        self.candidate_list.setMaximumHeight(150)
        self.candidate_status = QLabel(
            "AI 只查看授权目录中的文件名和元数据。选中候选后仍需人工确认传输计划。"
        )
        self.candidate_status.setWordWrap(True)
        self.use_candidate_button = QPushButton("载入候选文件并审核计划")
        self.use_candidate_button.setEnabled(False)
        root_controls = QHBoxLayout()
        root_controls.addWidget(self.authorized_root, 1)
        root_controls.addWidget(self.choose_root_button)
        model_controls = QHBoxLayout()
        model_controls.addWidget(QLabel("Ollama 地址"))
        model_controls.addWidget(self.ollama_url, 1)
        model_controls.addWidget(QLabel("模型"))
        model_controls.addWidget(self.ollama_model)
        model_controls.addWidget(self.save_ai_settings_button)
        layout.addWidget(QLabel("查找候选文件并审核计划"))
        layout.addLayout(root_controls)
        layout.addLayout(model_controls)
        layout.addWidget(self.file_query)
        layout.addWidget(self.search_files_button)
        layout.addWidget(self.search_hint)
        layout.addWidget(self.candidate_list)
        layout.addWidget(self.use_candidate_button)
        layout.addWidget(self.candidate_status)
        self.choose_root_button.clicked.connect(self.choose_authorized_root)
        self.save_ai_settings_button.clicked.connect(self.save_ai_settings)
        self.search_files_button.clicked.connect(self.search_authorized_files)
        self.use_candidate_button.clicked.connect(self.use_selected_candidate)
        self.candidate_list.itemSelectionChanged.connect(
            lambda: self.use_candidate_button.setEnabled(
                self.candidate_list.currentItem() is not None
            )
        )
        self.file_query.textChanged.connect(self.invalidate_search_results)
        self.update_search_button_state()
        layout.addStretch()
        self._add_page("AI 查找", layout)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("传输历史"))
        layout.addWidget(self.history_filter)
        layout.addWidget(self.transfer_history)
        layout.addWidget(self.prune_history_button)
        self.diagnostic_button = QPushButton("复制诊断信息")
        self.diagnostic_button.clicked.connect(self.copy_diagnostics)
        self.diagnostic_status = QLabel("诊断信息包含版本、数据目录、数据库版本及最近错误。")
        self.diagnostic_status.setWordWrap(True)
        layout.addWidget(QLabel("诊断与支持"))
        layout.addWidget(self.diagnostic_button)
        layout.addWidget(self.diagnostic_status)
        layout.addStretch()
        self._add_page("历史与诊断", layout)
        self.update_plan_button_state()

    def _add_page(self, title: str, layout: QVBoxLayout) -> QScrollArea:
        layout.setContentsMargins(16, 16, 16, 16)
        container = QWidget()
        container.setLayout(layout)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(container)
        self.tabs.addTab(scroll_area, title)
        return scroll_area

    def dismiss_guide(self) -> None:
        self.repository.save_setting("onboarding_done", "1")
        self.guide_label.hide()
        self.dismiss_guide_button.hide()

    def save_ai_settings(self) -> None:
        from urllib.parse import urlsplit

        url = self.ollama_url.text().strip()
        model = self.ollama_model.text().strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not model:
            self.candidate_status.setText("请填写有效的 Ollama HTTP(S) 地址和模型名称。")
            return
        self.repository.save_setting("ollama_url", url)
        self.repository.save_setting("ollama_model", model)
        self.candidate_status.setText("AI 设置已保存。")

    def _record_issue(self, code: str, message: str) -> None:
        self.last_issue = message
        if self.diagnostics is not None:
            self.diagnostics.event(code, message)

    def copy_diagnostics(self) -> None:
        if self.diagnostics is None:
            self.diagnostic_status.setText("此运行环境未配置诊断日志。")
            return
        report = self.diagnostics.report(
            version=__version__,
            schema_version=self.repository.schema_version(),
            service="运行中" if self.stop_button.isEnabled() else "未启动",
            issue=self.last_issue,
        )
        QApplication.clipboard().setText(report)
        self.diagnostic_status.setText("诊断信息已复制，可粘贴反馈；授权码和私钥已脱敏。")

    def prune_old_history(self) -> None:
        answer = QMessageBox.question(
            self,
            "清理旧历史",
            "清理 30 天前已结束的发送与接收历史？\n"
            "计划、配对信息和磁盘上的文件都会保留。此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            sent, received = self.repository.prune_terminal_history(
                datetime.now(UTC) - timedelta(days=30)
            )
        except sqlite3.Error as error:
            self._record_issue("HISTORY_CLEANUP_FAILED", str(error))
            self.diagnostic_status.setText(f"清理失败：{error}")
            return
        self.refresh_transfer_history()
        self.diagnostic_status.setText(
            f"已清理 {sent} 条发送记录、{received} 条接收记录；计划与文件保留。"
        )

    def choose_authorized_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择本次允许 AI 检索的文件夹")
        if not selected:
            return
        root = Path(selected).expanduser().resolve()
        self.authorized_root.setText(str(root))
        self.repository.save_setting("authorized_root", str(root))
        self.candidate_list.clear()
        self.search_result = None
        self.candidate_status.setText(f"本次授权目录：{root}。仅检索此文件夹及其子文件夹。")
        self.update_search_button_state()

    def invalidate_search_results(self, _text: str) -> None:
        self.search_result = None
        self.candidate_list.clear()
        self.use_candidate_button.setEnabled(False)
        self.candidate_status.setText("描述已更改，请重新检索并审核候选。")
        self.update_search_button_state()

    def update_search_button_state(self) -> None:
        worker_busy = bool(self.search_worker and self.search_worker.isRunning())
        self.search_files_button.setEnabled(
            bool(
                self.authorized_root.text().strip()
                and self.file_query.text().strip()
                and not worker_busy
            )
        )
        if worker_busy:
            reason = "正在检索，请等待完成。"
        elif not self.authorized_root.text().strip():
            reason = "请先选择授权目录。"
        elif not self.file_query.text().strip():
            reason = "请描述要查找的文件。"
        else:
            reason = "可以开始检索。"
        self.search_files_button.setToolTip(reason)
        self.search_hint.setText(reason)

    def search_authorized_files(self) -> None:
        if self.search_worker and self.search_worker.isRunning():
            return
        root = self.authorized_root.text().strip()
        query = self.file_query.text().strip()
        ollama_url = self.ollama_url.text().strip()
        model = self.ollama_model.text().strip()
        if not root or not query:
            self.candidate_status.setText("请选择授权目录并描述要查找的文件。")
            return
        try:
            self.repository.save_setting("authorized_root", str(Path(root).resolve()))
            self.repository.save_setting("ollama_url", ollama_url)
            self.repository.save_setting("ollama_model", model)
        except (OSError, sqlite3.Error, ValueError) as error:
            self.candidate_status.setText(f"保存检索设置失败：{error}")
            return
        self.candidate_list.clear()
        self.search_result = None
        self.use_candidate_button.setEnabled(False)
        self.search_files_button.setEnabled(False)
        self.candidate_status.setText(
            "正在扫描授权目录并请求 Ollama 匹配候选文件……不会读取文件内容。"
        )
        peer_names = tuple(
            peer.device_name for peer in self.repository.list_paired_devices()
            if peer.device_id != self.local_device_id
        )
        self.search_worker = FileSearchWorker(root, query, peer_names, ollama_url, model)
        self.search_worker.succeeded.connect(self.on_file_search_succeeded)
        self.search_worker.failed.connect(self.on_file_search_failed)
        self.search_worker.finished.connect(self.update_search_button_state)
        self.search_worker.start()

    def on_file_search_succeeded(self, result: IntentSearchResult) -> None:
        if (
            result.request_text != self.file_query.text().strip()
            or result.authorized_root != str(Path(self.authorized_root.text()).resolve())
        ):
            self.candidate_status.setText("检索期间描述或授权目录已更改，请重新检索。")
            return
        self.search_result = result
        self.candidate_list.clear()
        self.selected_peer_id = None
        self.selected_receive_grant = ""
        self.receive_token_field.clear()
        self.copy_token_button.setEnabled(False)
        self.rotate_grant_button.setEnabled(False)
        self.selected_peer_label.setText("接收设备：未选择")
        self.peer_list.setCurrentRow(-1)
        self.peer_token.clear()
        self.save_peer_token_button.setEnabled(False)
        if result.intent.target_device_name:
            for index in range(self.peer_list.count()):
                peer_item = self.peer_list.item(index)
                device = self.repository.get_device(
                    UUID(peer_item.data(Qt.ItemDataRole.UserRole))
                )
                if device and device.device_name == result.intent.target_device_name:
                    self.peer_list.setCurrentItem(peer_item)
                    self.select_peer(peer_item)
                    break
        for candidate in result.candidates:
            use_created = result.recent_by == "created" and candidate.created_at is not None
            shown_at = candidate.created_at if use_created else candidate.modified_at
            date_label = "创建" if use_created else "修改"
            timestamp = shown_at.astimezone().strftime("%Y-%m-%d %H:%M")
            item = QListWidgetItem(
                f"{candidate.relative_path} · {candidate.file_size_bytes} B · "
                f"{date_label} {timestamp}"
            )
            item.setData(Qt.ItemDataRole.UserRole, candidate.candidate_id)
            self.candidate_list.addItem(item)
        count = self.candidate_list.count()
        source = "Ollama 排序" if result.ranking_source == "ollama" else "本地匹配"
        status = (
            f"{source}返回 {count} 个候选（授权目录已扫描 {result.scanned_count} 个文件）。"
            "请选择文件，再审核设备、目录和计划。"
        )
        if result.intent.target_device_name:
            status += f"\n描述中的设备：{result.intent.target_device_name}（需从已配对列表核对）。"
        if result.intent.target_directory_name:
            self.target_directory.setText(result.intent.target_directory_name)
            status += (
                f"\n建议接收子目录：Received/{result.intent.target_directory_name}，"
                "请确认或修改。"
            )
        if result.clarification:
            status += f"\n需要确认：{result.clarification}"
        self.candidate_status.setText(status)
        self.use_candidate_button.setEnabled(False)

    def on_file_search_failed(self, message: str) -> None:
        self._record_issue("AI_SEARCH_FAILED", message)
        self.search_result = None
        self.candidate_status.setText(f"AI 文件检索失败：{message}")
        self.use_candidate_button.setEnabled(False)

    def use_selected_candidate(self) -> None:
        item = self.candidate_list.currentItem()
        result = self.search_result
        if item is None or result is None:
            self.candidate_status.setText("请先选择一个候选文件。")
            return
        if (
            result.request_text != self.file_query.text().strip()
            or result.authorized_root != str(Path(self.authorized_root.text()).resolve())
        ):
            self.candidate_status.setText("描述或授权目录已变化，请重新检索。")
            return
        candidate = next(
            (value for value in result.candidates
             if value.candidate_id == item.data(Qt.ItemDataRole.UserRole)), None
        )
        if candidate is None:
            self.candidate_status.setText("候选已过期，请重新检索。")
            return
        root = Path(result.authorized_root)
        try:
            candidate_path = Path(candidate.path).resolve(strict=True)
            stat = candidate_path.stat()
        except (OSError, RuntimeError):
            self.candidate_status.setText("候选文件已不存在，请重新检索。")
            return
        if not candidate_path.is_relative_to(root) or not candidate_path.is_file():
            self.candidate_status.setText("候选文件已移出授权目录或不存在，请重新检索。")
            return
        if stat.st_size != candidate.file_size_bytes or abs(
            stat.st_mtime - candidate.modified_at.timestamp()
        ) > 0.001:
            self.candidate_status.setText("候选文件在检索后发生变化，请重新检索。")
            return
        if self.selected_peer_id is None:
            QMessageBox.information(
                self,
                "尚未选择接收设备",
                "请先在已配对设备列表中选择接收设备，再载入候选文件。",
            )
            return
        peer = next(
            (value for value in self.repository.list_paired_devices()
             if value.device_id == self.selected_peer_id), None
        )
        if peer is None:
            self.candidate_status.setText("所选接收设备已解除配对，请重新选择设备。")
            return
        expected_peer = result.intent.target_device_name
        if expected_peer and peer.device_name != expected_peer:
            self.candidate_status.setText(
                f"描述指定“{expected_peer}”，当前选中“{peer.device_name}”；"
                "请重新选择设备或修改描述并检索。"
            )
            return
        if self.file_worker and self.file_worker.isRunning():
            return
        self.pending_ai_candidate_plan = True
        self.pending_ai_candidate = (result, candidate)
        self.inspected_file = None
        self.file_status.setText("正在读取候选文件并计算 SHA-256……")
        self.file_hash.setText("SHA-256：计算中")
        self.candidate_status.setText("正在计算 SHA-256；完成后会弹窗询问是否加入最近传输计划。")
        self.choose_file_button.setEnabled(False)
        self.use_candidate_button.setEnabled(False)
        self.file_worker = FileInspectionWorker(str(candidate_path))
        self.file_worker.succeeded.connect(self.on_file_inspected)
        self.file_worker.failed.connect(self.on_file_inspection_failed)
        self.file_worker.finished.connect(self.on_file_worker_finished)
        self.file_worker.start()

    def start_local_service(self) -> None:
        if self.start_worker is not None and self.start_worker.isRunning():
            return
        self.local_status.setText("正在启动本机服务……")
        self.start_button.setEnabled(False)
        self.start_worker = NodeServerStartWorker(self.node_server)
        self.start_worker.succeeded.connect(self.on_local_service_started)
        self.start_worker.failed.connect(self.on_local_service_start_failed)
        self.start_worker.start()

    def on_local_service_started(self) -> None:
        self.local_status.setText("本机服务已启动，监听端口 8765。")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def on_local_service_start_failed(self, message: str) -> None:
        self._record_issue("LOCAL_SERVICE_START_FAILED", message)
        self.local_status.setText(f"本机服务启动失败：{message}")
        self.start_button.setEnabled(True)

    def stop_local_service(self) -> None:
        try:
            self.node_server.stop()
        except RuntimeError as error:
            self.local_status.setText(f"本机服务停止中：{error}")
            return
        self.local_status.setText("本机节点服务已停止。")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def check_peer(self) -> None:
        if self.health_worker and self.health_worker.isRunning():
            return
        self.last_checked_peer = None
        self.pair_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.peer_status.setText("正在检查目标设备……")
        expected_fingerprint = None
        certificate_pem = ""
        if self.selected_peer_id is not None:
            device = self.repository.get_device(self.selected_peer_id)
            _token, certificate_pem = self.repository.get_peer_security(self.selected_peer_id)
            if device is not None and certificate_pem:
                expected_fingerprint = device.public_key_fingerprint
        self.health_worker = HealthCheckWorker(
            self.peer_address.text(), expected_fingerprint, certificate_pem
        )
        self.health_worker.succeeded.connect(self.on_health_check_succeeded)
        self.health_worker.failed.connect(self.on_health_check_failed)
        self.health_worker.finished.connect(lambda: self.check_button.setEnabled(True))
        self.health_worker.start()

    def invalidate_checked_peer(self, _text: str) -> None:
        self.last_checked_peer = None
        self.pair_button.setEnabled(False)
        self.selected_peer_id = None
        self.selected_receive_grant = ""
        self.receive_token_field.clear()
        self.copy_token_button.setEnabled(False)
        self.rotate_grant_button.setEnabled(False)
        self.selected_peer_label.setText("接收设备：未选择")
        self.update_plan_button_state()

    def on_health_check_succeeded(self, response: HealthResponse) -> None:
        if response.device_id == self.local_device_id:
            self.peer_status.setText("目标地址指向本机，不能与自己配对。")
            return
        self.last_checked_peer = response
        self.pair_button.setEnabled(True)
        already_paired = self.repository.get_device(response.device_id) is not None
        action_text = (
            "已配对；可更新保存的地址。" if already_paired else "请点击“确认并保存配对”完成添加。"
        )
        self.peer_status.setText(
            f"设备在线：{response.device_name}（{response.platform}），"
            f"AI Device Bridge v{response.app_version}。TLS 指纹："
            f"{response.certificate_fingerprint}。{action_text}"
        )

    def save_current_peer(self) -> None:
        response = self.last_checked_peer
        if response is None:
            self.peer_status.setText("请先检查目标设备，再保存配对。")
            return
        answer = QMessageBox.question(
            self,
            "核对设备 TLS 指纹",
            "请通过可信渠道与接收电脑屏幕显示的 TLS 指纹逐字符核对后再继续。\n\n"
            f"{response.certificate_fingerprint}\n\n指纹一致，确认保存配对？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.save_peer(
                DeviceProfile(
                    device_id=response.device_id,
                    device_name=response.device_name,
                    public_key_fingerprint=response.certificate_fingerprint,
                    platform=response.platform,
                    created_at=datetime.now(UTC),
                ),
                self.peer_address.text(),
                certificate_pem=response.certificate_pem,
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, "配对保存失败", str(error))
            return
        self.refresh_peer_list()
        self.peer_status.setText(
            f"配对已保存：{response.device_name}，列表现有 {self.peer_list.count()} 台设备。"
        )
        if self.peer_list.count() > 0:
            self.peer_list.setCurrentRow(self.peer_list.count() - 1)
            self.select_peer(self.peer_list.currentItem())

    def refresh_peer_list(self) -> None:
        self.peer_list.clear()
        for device in self.repository.list_paired_devices():
            if device.device_id == self.local_device_id:
                continue
            address = self.repository.get_peer_address(device.device_id)
            address_label = address or "未保存地址"
            item_text = f"{device.device_name} · {device.platform} · {address_label}"
            self.peer_list.addItem(item_text)
            self.peer_list.item(self.peer_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole, str(device.device_id)
            )
        self.paired_devices_label.setText(f"已配对设备（{self.peer_list.count()}）")
        self.empty_peer_label.setVisible(self.peer_list.count() == 0)

    def select_peer(self, item: QListWidgetItem) -> None:
        device_id = UUID(item.data(Qt.ItemDataRole.UserRole))
        address = self.repository.get_peer_address(device_id)
        self.selected_peer_id = device_id
        token, _certificate = self.repository.get_peer_security(device_id)
        self.peer_token.setText(token)
        self.save_peer_token_button.setEnabled(True)
        if address:
            self.peer_address.setText(address)
            self.selected_peer_id = device_id
            self.peer_status.setText("已载入该设备保存的地址；点击“检查设备”刷新在线状态。")
        self.selected_receive_grant = self.repository.get_or_create_receive_grant(device_id)
        if self.diagnostics is not None:
            self.diagnostics.add_secret(self.selected_receive_grant)
        self.receive_token_field.setText(self.selected_receive_grant)
        self.copy_token_button.setEnabled(True)
        self.rotate_grant_button.setEnabled(True)
        device = self.repository.get_device(device_id)
        self.selected_peer_label.setText(
            f"接收设备：{device.device_name}" if device else "接收设备：未选择"
        )
        self.update_plan_button_state()

    def rotate_selected_receive_grant(self) -> None:
        if self.selected_peer_id is None:
            return
        answer = QMessageBox.question(
            self, "轮换设备授权码",
            "旧授权码会立即失效，正在等待的接收请求会被拒绝。"
            "需要把新授权码复制到发送电脑。确定轮换吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.selected_receive_grant = self.repository.rotate_receive_grant(
            self.selected_peer_id
        )
        if self.diagnostics is not None:
            self.diagnostics.add_secret(self.selected_receive_grant)
        self.receive_token_field.setText(self.selected_receive_grant)
        self.peer_status.setText("设备接收授权码已轮换。请将新授权码复制到发送电脑。")

    def review_pending_receives(self) -> None:
        if self.reviewing_receive_request or not getattr(self.node_server, "is_running", False):
            return
        pending = self.repository.list_pending_receive_requests()
        if not pending:
            return
        request = pending[0]
        sender = self.repository.get_device(request.sender_device_id)
        sender_name = sender.device_name if sender else str(request.sender_device_id)
        self.reviewing_receive_request = True
        try:
            self.raise_()
            self.activateWindow()
            answer = QMessageBox.question(
                self, "确认接收文件",
                f"发送设备：{sender_name}\n文件：{request.file_name}\n"
                f"大小：{request.file_size_bytes:,} 字节\n"
                f"保存至：Received/{request.target_directory}\n"
                f"SHA-256：{request.expected_sha256}\n\n允许这一次传输吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            accepted = self.repository.decide_receive_request(
                request.request_id, approve=answer == QMessageBox.StandardButton.Yes
            )
            if accepted:
                self.local_status.setText(
                    "已同意此次接收，正在等待文件。" if answer == QMessageBox.StandardButton.Yes
                    else "已拒绝此次接收，文件不会上传。"
                )
            else:
                self.local_status.setText("接收请求已过期或授权已撤销，文件不会上传。")
        finally:
            self.reviewing_receive_request = False

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择要准备发送的文件")
        if not path:
            return
        if self.file_worker and self.file_worker.isRunning():
            return
        self.pending_ai_candidate_plan = False
        self.pending_ai_candidate = None
        self.inspected_file = None
        self.file_status.setText("正在计算 SHA-256……")
        self.file_hash.setText("SHA-256：计算中")
        self.choose_file_button.setEnabled(False)
        self.update_plan_button_state()
        self.file_worker = FileInspectionWorker(path)
        self.file_worker.succeeded.connect(self.on_file_inspected)
        self.file_worker.failed.connect(self.on_file_inspection_failed)
        self.file_worker.finished.connect(self.on_file_worker_finished)
        self.file_worker.start()

    def on_file_worker_finished(self) -> None:
        self.choose_file_button.setEnabled(True)
        self.update_plan_button_state()

    def on_file_inspected(self, result: FileInspection) -> None:
        self.inspected_file = result
        size_mib = result.file_size_bytes / (1024 * 1024)
        self.file_status.setText(f"{result.file_name} · {size_mib:.2f} MiB")
        self.file_hash.setText(f"SHA-256：{result.sha256}")
        self.update_plan_button_state()
        if self.pending_ai_candidate_plan:
            self.pending_ai_candidate_plan = False
            self.create_transfer_plan(from_ai_candidate=True)
            self.pending_ai_candidate = None

    def on_file_inspection_failed(self, message: str) -> None:
        self._record_issue("FILE_INSPECTION_FAILED", message)
        self.pending_ai_candidate_plan = False
        self.pending_ai_candidate = None
        self.inspected_file = None
        self.file_status.setText(f"文件读取失败：{message}")
        self.file_hash.setText("SHA-256：—")
        self.update_plan_button_state()

    def update_plan_button_state(self) -> None:
        peer_ready = (
            self.selected_peer_id is not None
            and self.repository.get_device(self.selected_peer_id) is not None
        )
        file_ready = self.inspected_file is not None
        worker_busy = bool(self.file_worker and self.file_worker.isRunning())
        self.create_plan_button.setEnabled(peer_ready and file_ready and not worker_busy)
        if worker_busy:
            reason = "正在计算文件校验值。"
        elif not peer_ready:
            reason = "请先在已配对设备列表中选择接收设备。"
        elif not file_ready:
            reason = "请先选择文件并等待 SHA-256 计算完成。"
        else:
            reason = "可以预览并确认计划。"
        self.create_plan_button.setToolTip(reason)
        self.preparation_hint.setText(reason)

    def create_transfer_plan(
        self, _checked: bool = False, *, from_ai_candidate: bool = False
    ) -> None:
        source = self.inspected_file
        target_id = self.selected_peer_id
        target_directory = self.target_directory.text().strip()
        ai_choice = self.pending_ai_candidate if from_ai_candidate else None
        if source is None or target_id is None:
            self.plan_status.setText("请先选择文件，并在已配对设备列表中选择接收设备。")
            return
        if not is_safe_relative_directory(target_directory):
            self.plan_status.setText("请填写 Received 下有效的相对目标目录。")
            return
        target = next(
            (value for value in self.repository.list_paired_devices()
             if value.device_id == target_id), None
        )
        if target is None:
            self.plan_status.setText("所选设备已解除配对，请重新选择设备。")
            self.refresh_peer_list()
            return
        if from_ai_candidate:
            if ai_choice is None:
                self.plan_status.setText("AI 候选已失效，请重新检索。")
                return
            result, candidate = ai_choice
            try:
                current_source_path = Path(source.path).resolve(strict=True)
            except (OSError, RuntimeError):
                self.plan_status.setText("AI 候选文件已不存在，请重新检索。")
                return
            if (
                result.request_text != self.file_query.text().strip()
                or result.authorized_root != str(Path(self.authorized_root.text()).resolve())
                or source.path != candidate.path
                or not current_source_path.is_relative_to(result.authorized_root)
            ):
                self.plan_status.setText("检索条件或候选文件已变化，请重新检索。")
                return
            if (
                result.intent.target_device_name
                and target.device_name != result.intent.target_device_name
            ):
                self.plan_status.setText("所选设备与描述不符，请重新选择或重新检索。")
                return
        prompt_text = (
            "将此文件加入最近传输计划？确认后计划会显示在列表中，但不会自动发送。"
            if from_ai_candidate
            else "确认保存计划？当前不会发送文件。"
        )
        answer = QMessageBox.question(
            self,
            "载入最近传输计划" if from_ai_candidate else "确认传输计划",
            f"文件：{source.file_name}\n大小：{source.file_size_bytes} 字节\n"
            f"SHA-256：{source.sha256}\n接收设备：{target.device_name}\n"
            f"接收位置：Received/{target_directory}\n"
            + (
                f"原始描述：{ai_choice[0].request_text}\n"
                f"授权目录：{ai_choice[0].authorized_root}\n"
                f"人工选择：{ai_choice[1].candidate_id} · {ai_choice[1].relative_path}\n"
                if ai_choice else ""
            )
            + f"\n{prompt_text}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            if from_ai_candidate:
                self.plan_status.setText("已取消载入计划；文件仍保留在本机，未加入最近计划。")
            return
        now = datetime.now(UTC)
        plan = TransferPlan(
            plan_id=uuid4(),
            source_device_id=self.local_device_id,
            source_path=source.path,
            target_device_id=target_id,
            target_directory=target_directory,
            source_mode=(
                SourceMode.NATURAL_LANGUAGE if from_ai_candidate else SourceMode.MANUAL
            ),
            file_name=source.file_name,
            file_size_bytes=source.file_size_bytes,
            status=PlanStatus.CONFIRMED,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
            expected_sha256=source.sha256,
        )
        try:
            if ai_choice:
                result, candidate = ai_choice
                self.repository.save_ai_plan(
                    plan,
                    AIPlanEvidence(
                        plan_id=plan.plan_id,
                        request_text=result.request_text,
                        authorized_root=result.authorized_root,
                        candidates=tuple(
                            (item.candidate_id, item.relative_path) for item in result.considered
                        ),
                        selected_candidate_id=candidate.candidate_id,
                        created_at=now,
                    ),
                )
            else:
                self.repository.save_plan(plan)
        except (sqlite3.Error, ValueError) as error:
            self.plan_status.setText(f"计划保存失败：{error}")
            return
        if from_ai_candidate:
            self.plan_filter.clear()
        self.refresh_plan_list()
        self.plan_status.setText(f"传输计划已保存（{plan.plan_id}），文件仍保留在本机，尚未发送。")
        if from_ai_candidate:
            for index in range(self.plan_list.count()):
                item = self.plan_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == str(plan.plan_id):
                    self.plan_list.setCurrentItem(item)
                    break
            self.tabs.setCurrentWidget(self.send_page)

    def refresh_plan_list(self) -> None:
        selected = self.plan_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        query = self.plan_filter.text().strip().casefold()
        self.plan_list.clear()
        for plan in self.repository.list_plans(limit=500):
            target = self.repository.get_device(plan.target_device_id)
            target_name = target.device_name if target else str(plan.target_device_id)
            source_label = "AI" if plan.source_mode is SourceMode.NATURAL_LANGUAGE else "手动"
            searchable = f"{plan.file_name} {target_name} {plan.status.value} {source_label}"
            if query and query not in searchable.casefold():
                continue
            if self.plan_list.count() >= 100:
                break
            self.plan_list.addItem(
                f"[{source_label}] {plan.file_name} → {target_name} · {plan.status.value} · "
                f"{plan.file_size_bytes} B · SHA-256 {plan.expected_sha256[:12]}…"
            )
            plan_item = self.plan_list.item(self.plan_list.count() - 1)
            plan_item.setData(Qt.ItemDataRole.UserRole, str(plan.plan_id))
            if selected_id == str(plan.plan_id):
                self.plan_list.setCurrentItem(plan_item)
            if plan.source_mode is SourceMode.NATURAL_LANGUAGE:
                evidence = self.repository.get_ai_plan_evidence(plan.plan_id)
                if evidence:
                    selected_path = next(
                        (path for candidate_id, path in evidence.candidates
                         if candidate_id == evidence.selected_candidate_id),
                        "候选记录缺失",
                    )
                    plan_item.setToolTip(
                        f"原始描述：{evidence.request_text}\n"
                        f"人工选择：{evidence.selected_candidate_id} · {selected_path}"
                    )
        self.update_send_button_state()

    def delete_selected_plan(self) -> None:
        item = self.plan_list.currentItem()
        if item is None:
            self.plan_status.setText("请先选择一个传输计划。")
            return
        plan_id = UUID(item.data(Qt.ItemDataRole.UserRole))
        if self.active_transfer_plan and self.active_transfer_plan.plan_id == plan_id:
            self.plan_status.setText("该计划正在发送，不能删除。")
            return
        plan = self.repository.get_plan(plan_id)
        if plan is None:
            self.plan_status.setText("所选计划已不存在，请刷新列表。")
            self.refresh_plan_list()
            return
        answer = QMessageBox.question(
            self,
            "删除传输计划",
            f"从最近传输计划列表中删除“{plan.file_name}”？\n\n"
            "这只会隐藏计划记录，不会删除本地源文件或接收端文件；相关传输历史会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = self.repository.delete_plan(plan_id)
        except sqlite3.Error as error:
            QMessageBox.warning(self, "删除计划失败", str(error))
            return
        self.refresh_plan_list()
        self.plan_status.setText(
            "计划已从最近列表删除。源文件与传输历史均保留。"
            if deleted
            else "所选计划已删除或不存在。"
        )

    def save_selected_peer_token(self) -> None:
        if self.selected_peer_id is None:
            self.plan_status.setText("请先在已配对设备列表中选择接收设备。")
            return
        token = self.peer_token.text().strip()
        if len(token) < 20:
            self.plan_status.setText("授权码长度无效，请从接收电脑复制完整授权码。")
            return
        try:
            self.repository.save_peer_token(self.selected_peer_id, token)
        except (ValueError, sqlite3.Error) as error:
            self.plan_status.setText(f"授权码保存失败：{error}")
            return
        if self.diagnostics is not None:
            self.diagnostics.add_secret(token)
        self.plan_status.setText("接收设备授权码已保存。")
        self.update_send_button_state()

    def update_send_button_state(self) -> None:
        item = self.plan_list.currentItem()
        plan = (
            self.repository.get_plan(UUID(item.data(Qt.ItemDataRole.UserRole)))
            if item is not None
            else None
        )
        transfer_busy = bool(self.transfer_worker and self.transfer_worker.isRunning())
        credentials_ready = False
        disabled_reason = "先选择一个已确认的传输计划。"
        if plan is not None:
            token, certificate = self.repository.get_peer_security(plan.target_device_id)
            device = self.repository.get_device(plan.target_device_id)
            address = self.repository.get_peer_address(plan.target_device_id)
            if plan.status is not PlanStatus.CONFIRMED:
                disabled_reason = f"此计划状态为“{plan.status.value}”，不能发送。"
            elif not device or not address:
                disabled_reason = "接收设备或地址未保存，请重新配对。"
            elif not certificate:
                disabled_reason = "请先检查设备并确认保存配对，以保存接收设备 TLS 证书。"
            elif not token:
                disabled_reason = "请粘贴接收电脑的授权码，并点击“保存授权码”。"
            elif transfer_busy:
                disabled_reason = "文件正在发送，请等待完成。"
            else:
                credentials_ready = True
        self.send_plan_button.setEnabled(
            bool(
                plan
                and plan.status is PlanStatus.CONFIRMED
                and not transfer_busy
                and credentials_ready
            )
        )
        self.send_plan_button.setToolTip(
            "发送条件已满足。" if self.send_plan_button.isEnabled() else disabled_reason
        )
        active_plan_id = self.active_transfer_plan.plan_id if self.active_transfer_plan else None
        self.delete_plan_button.setEnabled(plan is not None and plan.plan_id != active_plan_id)
        if (
            plan is not None
            and plan.status is PlanStatus.CONFIRMED
            and not self.send_plan_button.isEnabled()
            and not transfer_busy
        ):
            self.plan_status.setText(f"暂不能发送：{disabled_reason}")

    def send_selected_plan(self) -> None:
        item = self.plan_list.currentItem()
        if item is None:
            self.plan_status.setText("请先选择已确认的传输计划。")
            return
        plan = self.repository.get_plan(UUID(item.data(Qt.ItemDataRole.UserRole)))
        if plan is None or plan.status is not PlanStatus.CONFIRMED:
            self.plan_status.setText("所选计划不可发送。")
            return
        if not plan.expected_sha256:
            self.plan_status.setText("该计划没有文件校验值，请重新选择文件并生成计划。")
            return
        if plan.expires_at <= datetime.now(UTC):
            plan.status = PlanStatus.EXPIRED
            self.repository.save_plan(plan)
            self.refresh_plan_list()
            self.plan_status.setText("该计划已过期，请重新选择文件并确认计划。")
            return
        if plan.source_mode is SourceMode.NATURAL_LANGUAGE:
            evidence = self.repository.get_ai_plan_evidence(plan.plan_id)
            if evidence is None:
                self.plan_status.setText("AI 计划缺少候选审核记录，请重新检索并建立计划。")
                return
            try:
                source_path = Path(plan.source_path).resolve(strict=True)
                root_path = Path(evidence.authorized_root).resolve(strict=True)
            except (OSError, RuntimeError):
                self.plan_status.setText("AI 计划的授权目录或源文件已不存在，请重新检索。")
                return
            if not source_path.is_relative_to(root_path) or not source_path.is_file():
                self.plan_status.setText("AI 计划的源文件已离开授权目录，禁止发送。")
                return
        address = self.repository.get_peer_address(plan.target_device_id)
        token, certificate_pem = self.repository.get_peer_security(plan.target_device_id)
        if not address or not token or not certificate_pem:
            self.plan_status.setText("请先为目标设备保存 HTTPS 配对信息和接收授权码。")
            return
        answer = QMessageBox.question(
            self,
            "发送文件",
            f"将发送 {plan.file_name}（{plan.file_size_bytes} 字节）到目标设备。\n"
            f"接收目录：{plan.target_directory}\nSHA-256：{plan.expected_sha256}\n\n现在发送？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.send_plan_button.setEnabled(False)
        self.plan_status.setText("正在检查源文件……")
        self.transfer_progress.setValue(0)
        self.transfer_progress.setFormat("正在检查源文件")
        self.cancel_transfer_button.setEnabled(True)
        self.active_transfer_plan = plan
        self.transfer_worker = FileTransferWorker(
            plan, address, certificate_pem, token, self.repository
        )
        self.transfer_worker.succeeded.connect(self.on_transfer_succeeded)
        self.transfer_worker.failed.connect(self.on_transfer_failed)
        self.transfer_worker.cancelled.connect(self.on_transfer_cancelled)
        self.transfer_worker.phase_changed.connect(self.on_transfer_phase_changed)
        self.transfer_worker.progress_changed.connect(self.on_transfer_progress_changed)
        self.transfer_worker.finished.connect(self.update_send_button_state)
        self.transfer_worker.start()
        self.update_send_button_state()

    def on_transfer_phase_changed(self, phase: str) -> None:
        if phase == "checking":
            self.plan_status.setText("正在重新校验源文件……")
        elif phase == "waiting_receiver":
            self.plan_status.setText("已请求接收端确认；对方同意后才会上传文件……")
            self.transfer_progress.setFormat("等待接收端确认")
        elif phase == "transferring":
            self.plan_status.setText("正在上传文件……")
            self.transfer_progress.setFormat("已上传 %p%")
        elif phase == "verifying":
            self.plan_status.setText("上传完成，等待接收端校验和落盘……")
            self.transfer_progress.setFormat("已上传 100%，等待接收确认")
            self.cancel_transfer_button.setEnabled(False)

    def on_transfer_progress_changed(self, sent: int) -> None:
        plan = self.active_transfer_plan
        if plan is None:
            return
        percent = 100 if plan.file_size_bytes == 0 else sent * 100 // plan.file_size_bytes
        self.transfer_progress.setValue(min(percent, 100))
        self.transfer_progress.setFormat(f"已上传 {sent:,} / {plan.file_size_bytes:,} 字节（%p%）")

    def cancel_active_transfer(self) -> None:
        if self.transfer_worker is not None and self.transfer_worker.isRunning():
            self.transfer_worker.cancel()
            self.cancel_transfer_button.setEnabled(False)
            self.plan_status.setText("正在取消传输，等待当前网络操作结束……")

    def refresh_transfer_history(self) -> None:
        status_labels = {
            TransferStatus.PREPARING: "检查中",
            TransferStatus.WAITING_RECEIVER: "等待接收",
            TransferStatus.TRANSFERRING: "传输中",
            TransferStatus.VERIFYING: "校验中",
            TransferStatus.COMPLETED: "完成",
            TransferStatus.FAILED: "失败或结果未知",
            TransferStatus.CANCELLED: "已取消",
            TransferStatus.REJECTED: "已拒绝",
        }
        query = self.history_filter.text().strip().casefold()
        self.transfer_history.clear()
        for entry in self.repository.list_history_entries(limit=2000):
            direction = "发出" if entry.direction.value == "sent" else "接收"
            label = status_labels[entry.status]
            detail = f"；{entry.error_message}" if entry.error_message else ""
            text = f"{direction} · {entry.file_name}：{label}{detail}"
            if query and query not in text.casefold():
                continue
            if self.transfer_history.count() >= 100:
                break
            self.transfer_history.addItem(text)

    def on_transfer_succeeded(self, result: dict[str, object]) -> None:
        self.cancel_transfer_button.setEnabled(False)
        self.transfer_progress.setValue(100)
        self.transfer_progress.setFormat("接收端已校验并保存")
        if self.active_transfer_plan is not None:
            self.active_transfer_plan.status = PlanStatus.COMPLETED
            try:
                self.repository.save_plan(self.active_transfer_plan)
            except sqlite3.Error:
                self.plan_status.setText(
                    "接收端已确认文件完整，但本机计划状态保存失败；请检查历史后再操作。"
                )
                self.active_transfer_plan = None
                self.refresh_transfer_history()
                return
            self.active_transfer_plan = None
            self.refresh_plan_list()
        self.refresh_transfer_history()
        self.plan_status.setText(
            f"发送成功，接收端已校验 SHA-256。文件：{result['file_name']}，"
            f"大小：{result['file_size_bytes']} 字节，SHA-256：{result['sha256']}"
        )

    def on_transfer_failed(self, message: str) -> None:
        self._record_issue("TRANSFER_FAILED", message)
        self.cancel_transfer_button.setEnabled(False)
        self.refresh_transfer_history()
        self.plan_status.setText(f"发送失败：{message}")
        self.active_transfer_plan = None

    def on_transfer_cancelled(self, message: str) -> None:
        self.cancel_transfer_button.setEnabled(False)
        self.refresh_transfer_history()
        self.plan_status.setText(message)
        self.active_transfer_plan = None

    def remove_selected_peer(self) -> None:
        item = self.peer_list.currentItem()
        if item is None:
            return
        device_id = UUID(item.data(Qt.ItemDataRole.UserRole))
        answer = QMessageBox.question(
            self,
            "解除配对",
            f"确定从本机移除 {item.text()} 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                removed = self.repository.remove_device(device_id)
            except sqlite3.Error as error:
                QMessageBox.warning(self, "解除配对失败", str(error))
                return
            if self.selected_peer_id == device_id:
                self.selected_peer_id = None
                self.selected_receive_grant = ""
                self.receive_token_field.clear()
                self.copy_token_button.setEnabled(False)
                self.rotate_grant_button.setEnabled(False)
                self.selected_peer_label.setText("接收设备：未选择")
                self.save_peer_token_button.setEnabled(False)
                self.peer_token.clear()
                self.peer_address.clear()
            self.refresh_peer_list()
            self.update_send_button_state()
            if removed:
                self.peer_status.setText("配对已解除；已保存的传输计划和历史设备资料仍保留。")

    def on_health_check_failed(self, message: str) -> None:
        self._record_issue("HEALTH_CHECK_FAILED", message)
        self.peer_status.setText(f"连接失败：{message}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override name
        if self.start_worker and self.start_worker.isRunning():
            QMessageBox.information(self, "本机服务正在启动", "请稍候再关闭应用。")
            event.ignore()
            return
        if self.transfer_worker and self.transfer_worker.isRunning():
            QMessageBox.information(
                self,
                "文件正在传输",
                "请等待当前文件传输完成后再关闭应用。",
            )
            event.ignore()
            return
        if self.search_worker and self.search_worker.isRunning():
            QMessageBox.information(
                self,
                "AI 文件检索正在运行",
                "请等待文件检索完成后再关闭应用。",
            )
            event.ignore()
            return
        if self.health_worker and self.health_worker.isRunning():
            self.health_worker.wait(3500)
        if self.file_worker and self.file_worker.isRunning():
            self.file_worker.wait()
        try:
            self.node_server.stop()
        except RuntimeError as error:
            self.local_status.setText(f"本机服务仍在停止：{error}")
            event.ignore()
            return
        super().closeEvent(event)


def main() -> int:
    """Start the Qt event loop."""
    app = QApplication(sys.argv)
    app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    data_dir = app_data / "AI Device Bridge"
    diagnostics = DiagnosticLog(data_dir)
    try:
        repository = SQLiteRepository(data_dir / "bridge.sqlite3")
        receive_token = repository.get_setting("receive_token")
        diagnostics.add_secret(receive_token)
        for peer in repository.list_paired_devices():
            peer_token, _certificate = repository.get_peer_security(peer.device_id)
            diagnostics.add_secret(peer_token)
        for grant in repository.list_receive_grants():
            diagnostics.add_secret(grant)
        TransferJournal.reconcile_interrupted(repository)
        repository.reconcile_incomplete_incoming()
        repository.expire_receive_requests(all_pending=True)
        from ai_device_bridge.infrastructure.tls_identity import load_or_create_tls_identity

        identity = load_or_create_tls_identity(data_dir)
        local_device = repository.get_or_create_local_device(
            socket.gethostname(), platform.system()
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        message = diagnostics.redact(str(error))
        diagnostics.event("APP_START_FAILED", message)
        QMessageBox.critical(
            None, "AI Device Bridge 启动失败",
            f"{message}\n\n诊断日志：{diagnostics.path}",
        )
        return 1
    diagnostics.event("APP_STARTED", f"version={__version__} schema={repository.schema_version()}")
    window = MainWindow(
        NodeServer(
            device_id=local_device.device_id,
            certificate_path=identity.certificate_path,
            private_key_path=identity.private_key_path,
            certificate_fingerprint=identity.fingerprint,
            certificate_pem=identity.certificate_pem,
            receive_token=receive_token,
            receive_directory=data_dir / "Received",
            repository=repository,
        ),
        repository,
        local_device.device_id,
        identity.fingerprint,
        receive_token,
        diagnostics,
    )
    window.show()
    return app.exec()
