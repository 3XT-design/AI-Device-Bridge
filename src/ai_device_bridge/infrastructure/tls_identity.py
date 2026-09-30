"""Create and persist a self-signed TLS identity for this device installation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True, slots=True)
class TLSIdentity:
    certificate_path: Path
    private_key_path: Path
    certificate_pem: str
    fingerprint: str


def load_or_create_tls_identity(data_directory: str | Path) -> TLSIdentity:
    directory = Path(data_directory)
    directory.mkdir(parents=True, exist_ok=True)
    certificate_path = directory / "device-cert.pem"
    private_key_path = directory / "device-key.pem"

    if certificate_path.exists() and private_key_path.exists():
        certificate_pem = certificate_path.read_text(encoding="ascii")
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, f"AI Device Bridge {uuid4()}")]
        )
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(private_key, hashes.SHA256())
        )
        certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        private_key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        certificate_path.write_bytes(certificate_pem.encode("ascii"))
        private_key_path.write_bytes(private_key_pem)

    fingerprint = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    return TLSIdentity(
        certificate_path=certificate_path,
        private_key_path=private_key_path,
        certificate_pem=certificate_pem,
        fingerprint=fingerprint,
    )
