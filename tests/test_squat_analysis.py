from dataclasses import dataclass
from math import cos, radians, sin

from ai_device_bridge.services.squat_analysis import SquatAnalyzer


@dataclass
class Point:
    x: float = 0.0
    y: float = 0.0
    visibility: float = 0.0


def pose(knee_angle: float, hip_y: float, shoulder_x: float = 0.5) -> list[Point]:
    landmarks = [Point() for _ in range(33)]
    bend = radians(180 - knee_angle)
    left = {
        11: Point(shoulder_x, hip_y - 0.25, 0.99),
        23: Point(0.5, hip_y, 0.99),
        25: Point(0.5, 0.75, 0.99),
        27: Point(0.5 + 0.2 * sin(bend), 0.75 + 0.2 * cos(bend), 0.99),
    }
    for index, point in left.items():
        landmarks[index] = point
    return landmarks


def test_squat_counts_one_complete_movement_and_warns_about_depth() -> None:
    analyzer = SquatAnalyzer()
    movements = [
        (175, .45), (145, .49), (115, .55), (100, .62), (95, .66),
        (95, .67), (95, .67), (125, .64), (145, .60), (165, .54),
        (175, .48), (175, .45),
    ]
    results = [
        analyzer.update(pose(knee, hip), index * 120)
        for index, (knee, hip) in enumerate(movements)
    ]
    assert any(result.phase == "descending" for result in results)
    assert any(result.phase == "ascending" for result in results)
    assert any("蹲幅偏浅" in result.feedback for result in results)
    assert results[-1].repetitions == 1


def test_tracking_loss_resets_partial_action_without_incrementing() -> None:
    analyzer = SquatAnalyzer()
    analyzer.update(pose(140, .5), 100)
    missing = analyzer.update(None, 1300)
    assert missing.phase == "tracking_lost"
    assert "全身" in missing.feedback
    standing = analyzer.update(pose(175, .45), 1400)
    assert standing.phase == "ready"
    assert standing.repetitions == 0


def test_lean_feedback_waits_for_three_visible_frames() -> None:
    analyzer = SquatAnalyzer()
    results = [
        analyzer.update(pose(115, .55, shoulder_x=.87), index * 100)
        for index in range(4)
    ]
    assert "前倾" not in results[1].feedback
    assert "前倾" in results[-1].feedback


def test_aborted_shallow_motion_returns_to_ready() -> None:
    analyzer = SquatAnalyzer()
    results = [
        analyzer.update(pose(knee, .5), index * 100)
        for index, knee in enumerate((175, 145, 140, 150, 175, 175))
    ]
    assert results[-1].phase == "ready"
    assert results[-1].repetitions == 0
