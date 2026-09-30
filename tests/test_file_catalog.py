import json as json_lib
from datetime import UTC, datetime

import httpx

from ai_device_bridge.services.file_catalog import (
    FileCandidate,
    discover_files,
    rank_candidates_with_ollama,
)


def test_discover_files_stays_inside_directory_and_skips_symlinks(tmp_path) -> None:
    root = tmp_path / "allowed"
    nested = root / "reports"
    nested.mkdir(parents=True)
    included = nested / "annual-report.txt"
    included.write_text("metadata scan must not read this content", encoding="utf-8")
    outside = tmp_path / "private.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "linked.txt").symlink_to(outside)
    except OSError:
        pass

    results = discover_files(root)

    assert [item.relative_path for item in results] == ["reports/annual-report.txt"]
    assert results[0].file_size_bytes == included.stat().st_size


def test_ollama_candidate_ids_are_filtered_to_catalog(monkeypatch) -> None:
    now = datetime.now(UTC)
    candidates = [
        FileCandidate("F00001", "/allowed/report.docx", "report.docx", "report.docx", 12, now),
        FileCandidate("F00002", "/allowed/notes.txt", "notes.txt", "notes.txt", 10, now),
    ]
    captured = {}

    def fake_post(url, json, **_kwargs):
        captured["url"] = url
        captured["body"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json_lib.dumps(
                        {
                            "candidate_ids": ["F00002", "F99999", "F00002"],
                            "clarification": "请确认是否为这份笔记。",
                        }
                    )
                }
            },
            request=request,
        )

    monkeypatch.setattr("ai_device_bridge.services.file_catalog.httpx.post", fake_post)
    result = rank_candidates_with_ollama(
        "找笔记", candidates, "http://127.0.0.1:11434", "qwen2.5:3b"
    )

    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert [item.candidate_id for item in result.candidates] == ["F00002"]
    assert result.clarification == "请确认是否为这份笔记。"
    prompt = captured["body"]["messages"][1]["content"]
    assert "/allowed/report.docx" not in prompt
    assert "report.docx" in prompt
