"""Side-view squat feedback from timestamped 2D pose landmarks.

The thresholds are a first measurement baseline, not an individual training prescription.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from math import acos, atan2, degrees, hypot
from typing import Protocol


class Landmark(Protocol):
    x: float
    y: float
    visibility: float


@dataclass(frozen=True)
class SquatResult:
    phase: str
    repetitions: int
    side: str
    confidence: float
    knee_angle: float | None
    hip_angle: float | None
    trunk_lean: float | None
    hip_below_knee: bool | None
    feedback: str


def angle(a: Landmark, vertex: Landmark, b: Landmark) -> float:
    first = (a.x - vertex.x, a.y - vertex.y)
    second = (b.x - vertex.x, b.y - vertex.y)
    length = hypot(*first) * hypot(*second)
    if length < 1e-8:
        raise ValueError("Two pose landmarks overlap")
    cosine = max(-1.0, min(1.0, (first[0] * second[0] + first[1] * second[1]) / length))
    return degrees(acos(cosine))


class SquatAnalyzer:
    """Track a squat with hysteresis and report cues while the person moves."""

    _SIDES = {"left": (11, 23, 25, 27), "right": (12, 24, 26, 28)}

    def __init__(self) -> None:
        self.phase = "ready"
        self.repetitions = 0
        self.side = ""
        self.smoothed_knee: float | None = None
        self.lowest_knee = 180.0
        self.start_hip_y = 0.0
        self.max_hip_drop = 0.0
        self.started_ms = 0
        self.last_seen_ms = 0
        self.lean_frames = 0
        self.depth_warning = False
        self.reached_depth = False

    def update(self, landmarks: Sequence[Landmark] | None, captured_ms: int) -> SquatResult:
        if not landmarks or len(landmarks) < 29:
            return self._missing(captured_ms)
        side, confidence = self._visible_side(landmarks)
        if confidence < 0.60:
            return self._missing(captured_ms)
        shoulder, hip, knee, ankle = (landmarks[index] for index in self._SIDES[side])
        try:
            knee_angle = angle(hip, knee, ankle)
            hip_angle = angle(shoulder, hip, knee)
        except ValueError:
            return self._missing(captured_ms)
        trunk_lean = degrees(atan2(abs(shoulder.x - hip.x), abs(shoulder.y - hip.y)))
        hip_below_knee = hip.y >= knee.y - 0.02
        self.last_seen_ms = captured_ms
        self.smoothed_knee = (
            knee_angle if self.smoothed_knee is None
            else 0.35 * knee_angle + 0.65 * self.smoothed_knee
        )
        bent = self.smoothed_knee

        if self.phase == "ready" and bent < 150:
            self.phase = "descending"
            self.started_ms = captured_ms
            self.start_hip_y = hip.y
            self.lowest_knee = bent
            self.max_hip_drop = 0.0
            self.depth_warning = False
            self.reached_depth = hip_below_knee
        elif self.phase == "descending":
            self.lowest_knee = min(self.lowest_knee, bent)
            self.max_hip_drop = max(self.max_hip_drop, hip.y - self.start_hip_y)
            self.reached_depth = self.reached_depth or hip_below_knee
            if bent > self.lowest_knee + 9 and self.lowest_knee < 135:
                self.phase = "ascending"
                self.depth_warning = not self.reached_depth
            elif bent >= 165 and self.lowest_knee >= 135:
                self.phase = "ready"
        elif self.phase == "ascending" and bent >= 158:
            if (
                self.lowest_knee <= 125
                and self.max_hip_drop >= 0.05
                and captured_ms - self.started_ms >= 600
            ):
                self.repetitions += 1
            self.phase = "ready"
            self.depth_warning = False

        self.lean_frames = self.lean_frames + 1 if self.phase != "ready" and trunk_lean > 55 else 0
        if self.lean_frames >= 3:
            feedback = "躯干前倾较大，请留意姿态"
        elif self.depth_warning and self.phase == "ascending":
            feedback = "侧面观察蹲幅偏浅，下次可适当加深"
        elif self.phase == "descending":
            feedback = "下蹲中"
        elif self.phase == "ascending":
            feedback = "起身中"
        else:
            feedback = "准备深蹲"

        return SquatResult(
            self.phase, self.repetitions, side, confidence, knee_angle, hip_angle,
            trunk_lean, hip_below_knee, feedback,
        )

    def _visible_side(self, landmarks: Sequence[Landmark]) -> tuple[str, float]:
        scores = {
            side: min(float(landmarks[index].visibility) for index in indices)
            for side, indices in self._SIDES.items()
        }
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        if self.side and scores[self.side] >= scores[best] - 0.15:
            best = self.side
        self.side = best
        return best, scores[best]

    def _missing(self, captured_ms: int) -> SquatResult:
        if self.last_seen_ms and captured_ms - self.last_seen_ms > 1000:
            self.phase = "ready"
            self.smoothed_knee = None
            self.lean_frames = 0
            self.depth_warning = False
            self.reached_depth = False
        return SquatResult(
            "tracking_lost", self.repetitions, self.side, 0.0,
            None, None, None, None, "请让全身进入侧面画面",
        )
