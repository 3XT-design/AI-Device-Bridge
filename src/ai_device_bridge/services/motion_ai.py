"""Bounded squat-session statistics and local Ollama coaching."""

import json
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass
from statistics import median
from urllib.parse import urlsplit

import httpx

from ai_device_bridge.services.squat_analysis import SquatResult


class MotionAIError(Exception):
    """A user-facing local analysis error."""


@dataclass(frozen=True)
class SquatSessionSummary:
    duration_seconds: float
    processed_frames: int
    valid_frames: int
    repetitions: int
    knee_angle_min: float | None
    knee_angle_median: float | None
    trunk_lean_median: float | None
    trunk_lean_max: float | None
    depth_observed_frames: int
    phase_frames: dict[str, int]
    warning_frames: dict[str, int]


class SquatSessionCollector:
    """Collect small numerical statistics; never retain images or landmarks."""

    def __init__(self) -> None:
        self.first_ns: int | None = None
        self.last_ns: int | None = None
        self.processed_frames = 0
        self.valid_frames = 0
        self.repetitions = 0
        self.random = random.Random(0)
        self.knee_seen = 0
        self.trunk_seen = 0
        self.knee_angles: list[float] = []
        self.trunk_angles: list[float] = []
        self.knee_min: float | None = None
        self.trunk_max: float | None = None
        self.depth_observed_frames = 0
        self.phases: Counter[str] = Counter()
        self.warnings: Counter[str] = Counter()

    def add(self, result: SquatResult, read_ns: int) -> None:
        if self.first_ns is None:
            self.first_ns = read_ns
        self.last_ns = read_ns
        self.processed_frames += 1
        self.repetitions = max(self.repetitions, result.repetitions)
        self.phases[result.phase] += 1
        if result.confidence < 0.60 or result.knee_angle is None:
            return
        self.valid_frames += 1
        self.knee_min = (
            min(self.knee_min, result.knee_angle)
            if self.knee_min is not None else result.knee_angle
        )
        self.knee_seen += 1
        self._sample(self.knee_angles, result.knee_angle, self.knee_seen)
        if result.trunk_lean is not None:
            self.trunk_max = (
                max(self.trunk_max, result.trunk_lean)
                if self.trunk_max is not None else result.trunk_lean
            )
            self.trunk_seen += 1
            self._sample(self.trunk_angles, result.trunk_lean, self.trunk_seen)
        if result.hip_below_knee:
            self.depth_observed_frames += 1
        if result.feedback in {
            "躯干前倾较大，请留意姿态", "侧面观察蹲幅偏浅，下次可适当加深"
        }:
            self.warnings[result.feedback] += 1

    def _sample(self, values: list[float], value: float, seen: int) -> None:
        if len(values) < 3600:
            values.append(value)
        else:
            position = self.random.randrange(seen)
            if position < len(values):
                values[position] = value

    def summary(self) -> SquatSessionSummary:
        duration = (
            (self.last_ns - self.first_ns) / 1_000_000_000
            if self.first_ns is not None and self.last_ns is not None else 0.0
        )
        return SquatSessionSummary(
            duration_seconds=round(duration, 1),
            processed_frames=self.processed_frames,
            valid_frames=self.valid_frames,
            repetitions=self.repetitions,
            knee_angle_min=round(self.knee_min, 1) if self.knee_min is not None else None,
            knee_angle_median=round(median(self.knee_angles), 1) if self.knee_angles else None,
            trunk_lean_median=round(median(self.trunk_angles), 1) if self.trunk_angles else None,
            trunk_lean_max=round(self.trunk_max, 1) if self.trunk_max is not None else None,
            depth_observed_frames=self.depth_observed_frames,
            phase_frames=dict(self.phases),
            warning_frames=dict(self.warnings),
        )


def analyze_squat_with_ollama(
    summary: SquatSessionSummary, base_url: str, model: str,
) -> tuple[str, float]:
    """Ask only the local Ollama service for a concise interpretation."""
    if summary.valid_frames < 10:
        raise MotionAIError("有效姿态帧不足 10 帧，请延长录制并保持全身可见。")
    try:
        parsed = urlsplit(base_url.strip())
        port = parsed.port
    except ValueError as error:
        raise MotionAIError("Ollama 地址无效，请检查本机地址和端口。") from error
    if (
        parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or (port is not None and port < 1)
        or parsed.username or parsed.password or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment
    ):
        raise MotionAIError("动作数据只发送到本机 Ollama，请使用 http://127.0.0.1:11434。")
    if not model.strip():
        raise MotionAIError("请填写本机已安装的 Ollama 模型名称。")
    prompt = (
        "你是谨慎的中文深蹲动作分析助手。只根据提供的统计数据写简短建议；"
        "不得编造视频内容、左右差异、受伤风险或诊断。"
        "这些数据来自单侧 2D 姿态估计，深度帧数不是完成深蹲次数。"
        "若完成次数为 0 或有效帧较少，明确说明证据不足。"
        '只返回 JSON 对象：{"观察":"一句话","下一组建议":"一项具体可执行建议","局限":"一句话"}。'
    )
    started = time.monotonic()
    try:
        response = httpx.post(
            f"{base_url.strip().rstrip('/')}/api/chat",
            json={
                "model": model.strip(), "stream": False, "format": "json",
                "options": {"temperature": 0.2, "num_predict": 220},
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(asdict(summary), ensure_ascii=False)},
                ],
            },
            timeout=httpx.Timeout(60.0, connect=3.0),
            trust_env=False,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        answer = json.loads(content)
    except httpx.TimeoutException as error:
        raise MotionAIError("Ollama 分析超时，请尝试更小的本机模型。") from error
    except httpx.HTTPStatusError as error:
        raise MotionAIError(
            f"Ollama 返回 HTTP {error.response.status_code}；请检查模型名称与服务状态。"
        ) from error
    except httpx.RequestError as error:
        raise MotionAIError("无法连接本机 Ollama，请确认它已启动。") from error
    except (ValueError, KeyError, TypeError) as error:
        raise MotionAIError("Ollama 返回格式无效，请重试或更换模型。") from error
    if not isinstance(answer, dict) or any(
        not isinstance(answer.get(key), str) or not answer[key].strip()
        for key in ("观察", "下一组建议", "局限")
    ):
        raise MotionAIError("Ollama 未给出完整分析，请重试或更换模型。")
    text = "\n".join(
        f"{key}：{answer[key].strip()[:180]}" for key in ("观察", "下一组建议", "局限")
    )
    return text, time.monotonic() - started
