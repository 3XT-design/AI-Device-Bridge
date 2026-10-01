"""TLS health checks for other AI Device Bridge nodes."""

import hashlib
import ssl
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ai_device_bridge.api.health import TRANSFER_PROTOCOL_VERSION, HealthResponse


class PeerHealthError(RuntimeError):
    """Raised when a peer health check fails or returns an invalid response."""


def normalize_base_url(address: str) -> str:
    candidate = address.strip()
    if not candidate:
        raise PeerHealthError("请输入设备地址。")
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PeerHealthError("地址格式无效，请输入主机名或 IP 地址及端口。")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise PeerHealthError("请只输入设备地址，不要附加路径或查询参数。")
    try:
        parsed.port
    except ValueError as error:
        raise PeerHealthError("端口号无效。") from error
    return f"{parsed.scheme}://{parsed.netloc}"


def check_peer_health(
    address: str,
    timeout_seconds: float = 3.0,
    expected_fingerprint: str | None = None,
    trusted_certificate_pem: str | None = None,
) -> HealthResponse:
    base_url = normalize_base_url(address)
    if not base_url.startswith("https://"):
        raise PeerHealthError("设备检查只允许使用 HTTPS 地址。")
    verify: bool | ssl.SSLContext = False
    if trusted_certificate_pem:
        context = ssl.create_default_context()
        context.check_hostname = False
        try:
            context.load_verify_locations(cadata=trusted_certificate_pem)
        except (ssl.SSLError, ValueError) as error:
            raise PeerHealthError("本机保存的设备证书无法读取，请解除配对后重新核对。") from error
        verify = context
    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            verify=verify,
            trust_env=False,
        ) as client:
            response = client.get(f"{base_url}/api/v1/health")
            response.raise_for_status()
            result = HealthResponse.model_validate(response.json())
            network_stream = response.extensions.get("network_stream")
            ssl_object = network_stream.get_extra_info("ssl_object") if network_stream else None
            certificate_der = ssl_object.getpeercert(True) if ssl_object else None
            if not certificate_der:
                raise PeerHealthError("无法读取设备 TLS 证书。")
            actual_fingerprint = hashlib.sha256(certificate_der).hexdigest()
    except httpx.RequestError as error:
        raise PeerHealthError("无法连接设备，请检查地址、网络和防火墙设置。") from error
    except httpx.HTTPStatusError as error:
        raise PeerHealthError(f"设备返回 HTTP {error.response.status_code}。") from error
    except (ValueError, ValidationError) as error:
        raise PeerHealthError("设备响应格式不正确。") from error

    if result.status != "ok":
        raise PeerHealthError("设备节点当前状态异常。")
    if result.transfer_protocol_version != TRANSFER_PROTOCOL_VERSION:
        raise PeerHealthError("目标设备不支持逐次接收确认，请将两台电脑都更新到 M6 版本。")
    if actual_fingerprint != result.certificate_fingerprint.lower():
        raise PeerHealthError("设备报告的 TLS 指纹与实际连接证书不一致。")
    if expected_fingerprint and actual_fingerprint != expected_fingerprint.lower():
        raise PeerHealthError("设备 TLS 指纹已变化；为保护文件，请解除配对并重新核对指纹。")
    return result
