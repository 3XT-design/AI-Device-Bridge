from ai_device_bridge.services.diagnostics import DiagnosticLog


def test_diagnostic_log_and_report_redact_credentials_and_private_key(tmp_path) -> None:
    token = "a-secret-receive-token"
    peer = "a-secret-peer-token"
    private_key = "-----BEGIN PRIVATE KEY-----\nsecret-key-body\n-----END PRIVATE KEY-----"
    log = DiagnosticLog(tmp_path, (token,))
    log.add_secret(peer)
    detail = f"receive={token} peer={peer} Bearer othersecret {private_key}"
    log.event("startup-failed", detail)
    report = log.report(version="0.5.0rc1", schema_version=3, service="stopped", issue=detail)
    saved = log.path.read_text(encoding="utf-8")
    for secret in (token, peer, "othersecret", "secret-key-body"):
        assert secret not in saved
        assert secret not in report
    assert "STARTUP_FAILED" in saved
    assert "数据库版本：3" in report
