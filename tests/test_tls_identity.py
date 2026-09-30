from ai_device_bridge.infrastructure.tls_identity import load_or_create_tls_identity


def test_tls_identity_is_persisted_and_fingerprint_matches_certificate(tmp_path) -> None:
    first = load_or_create_tls_identity(tmp_path)
    second = load_or_create_tls_identity(tmp_path)

    assert first.fingerprint == second.fingerprint
    assert first.certificate_pem == second.certificate_pem
    assert first.certificate_path.is_file()
    assert first.private_key_path.is_file()
