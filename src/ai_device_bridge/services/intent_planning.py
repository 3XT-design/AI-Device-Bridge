"""Conservative, review-only parsing of a transfer request."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_device_bridge.domain.models import FileQuery, ParsedTransferIntent
from ai_device_bridge.services.file_catalog import (
    FileCandidate,
    FileCatalogError,
    discover_files,
    rank_candidates_with_ollama,
)


@dataclass(frozen=True, slots=True)
class IntentSearchResult:
    intent: ParsedTransferIntent
    candidates: tuple[FileCandidate, ...]
    considered: tuple[FileCandidate, ...]
    clarification: str
    ranking_source: str
    request_text: str
    authorized_root: str
    scanned_count: int = 0


def parse_transfer_intent(
    request: str, paired_device_names: tuple[str, ...], now: datetime | None = None
) -> ParsedTransferIntent:
    """Extract only explicit hints; never infer an unpaired device or an absolute path."""
    text = request.strip()
    if not text:
        raise ValueError("请输入要查找的文件描述。")
    now = now or datetime.now().astimezone()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")

    matched_names = sorted({name for name in paired_device_names if name and name in text})
    target_device = matched_names[0] if len(matched_names) == 1 else None
    questions: list[str] = []
    if len(matched_names) > 1 or (
        target_device is not None and paired_device_names.count(target_device) > 1
    ):
        target_device = None
        questions.append("描述中出现多台已配对设备，请在设备列表中明确选择一台。")
    elif not matched_names and re.search(r"发给|发送给|传给|发到|发送到|传到", text):
        questions.append("描述中的接收设备未能与已配对设备唯一对应，请在设备列表中选择。")

    directory: str | None = None
    quoted_directory = re.search(r"(?:目录|文件夹)[：:\s]*[“\"「]([^”\"」]+)[”\"」]", text)
    if quoted_directory:
        proposed = quoted_directory.group(1).strip()
        if is_safe_relative_directory(proposed):
            directory = proposed
        else:
            questions.append("目标目录不是有效的相对子目录，请在界面中填写。")
    elif re.search(r"(?:到|至|放到|存到|发到|传到).{0,30}桌面", text):
        directory = "桌面"
        questions.append("“桌面”仅指接收端 Received 下的同名子目录，并非系统桌面，请审核。")

    after: datetime | None = None
    before: datetime | None = None
    if "上周" in text:
        local_monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        after = (local_monday - timedelta(days=7)).astimezone(UTC)
        before = local_monday.astimezone(UTC)
    elif "最近" in text:
        questions.append("“最近”按修改时间从新到旧排序，不限制日期。")

    extensions = re.findall(
        r"\.(pdf|docx?|xlsx?|pptx?|txt|md|zip|png|jpe?g)(?![A-Za-z0-9])", text, re.I
    )
    if not extensions:
        extensions = re.findall(
            r"(?<![A-Za-z0-9])(pdf|docx?|xlsx?|pptx?|txt|md|zip|png|jpe?g)\s*文件",
            text,
            re.I,
        )

    file_terms = re.split(r"发给|发送给|传给|发到|发送到|传到", text, maxsplit=1)[0]
    for value in matched_names:
        file_terms = file_terms.replace(value, " ")
    if directory:
        file_terms = file_terms.replace(directory, " ")
    if quoted_directory:
        file_terms = file_terms.replace(quoted_directory.group(0), " ")
    file_terms = re.sub(
        r"(?i)(?<![A-Za-z0-9])(?:pdf|docx?|xlsx?|pptx?|txt|md|zip|png|jpe?g)\s*文件",
        " ",
        file_terms,
    )
    file_terms = re.sub(r"\.[A-Za-z0-9]+\b", " ", file_terms)
    file_terms = re.sub(
        r"上周|最近|修改|请|帮我|把|查找|寻找|找|发送给|发给|传给|发送到|发到|传到|发送|传输|"
        r"准备|一份|那份|这份|文件夹|文件|目录|的|给|到|至|并|一下|那个|这个",
        " ",
        file_terms,
    )
    file_terms = re.sub(r"[，。,.!？?\s]+", " ", file_terms).strip()
    query = FileQuery(
        keywords=[file_terms] if file_terms else [],
        extensions=extensions,
        modified_after=after,
        modified_before=before,
    )
    if not (query.keywords or query.extensions or query.modified_after or "最近" in text):
        questions.append("文件条件不够明确，请补充文件名、类型或时间。")
    return ParsedTransferIntent(
        file_query=query,
        target_device_name=target_device,
        target_directory_name=directory,
        needs_clarification=bool(questions),
        clarification_question=" ".join(questions) if questions else None,
    )


def is_safe_relative_directory(value: str) -> bool:
    parts = value.replace("\\", "/").split("/")
    return bool(value) and all(
        part not in {"", ".", ".."}
        and ":" not in part
        and not any(ord(character) < 32 for character in part)
        for part in parts
    ) and parts[0].casefold() != ".bridge-staging"


def deterministic_candidates(
    query: FileQuery, candidates: list[FileCandidate], limit: int = 200
) -> tuple[FileCandidate, ...]:
    """Filter by explicit metadata first, then rank filename/path token overlap."""
    if limit < 1:
        raise ValueError("limit must be positive")
    from ai_device_bridge.services.file_catalog import _tokens

    query_tokens = set().union(*(_tokens(word) for word in query.keywords))
    scored: list[tuple[int, FileCandidate]] = []
    for candidate in candidates:
        if query.extensions and not any(
            candidate.file_name.casefold().endswith(extension) for extension in query.extensions
        ):
            continue
        if query.modified_after and candidate.modified_at < query.modified_after:
            continue
        if query.modified_before and candidate.modified_at >= query.modified_before:
            continue
        overlap = len(query_tokens & _tokens(candidate.relative_path))
        if query_tokens and not overlap:
            continue
        scored.append((overlap, candidate))
    scored.sort(
        key=lambda entry: (
            -entry[0],
            -entry[1].modified_at.timestamp(),
            entry[1].relative_path.casefold(),
        )
    )
    return tuple(candidate for _score, candidate in scored[:limit])


def search_intent(
    root: str,
    request: str,
    paired_device_names: tuple[str, ...],
    ollama_url: str,
    model: str,
) -> IntentSearchResult:
    """Only cataloged file IDs may become suggestions; Ollama failure stays reviewable."""
    intent = parse_transfer_intent(request, paired_device_names)
    authorized_root = str(Path(root).expanduser().resolve(strict=True))
    catalog = discover_files(authorized_root)
    considered = deterministic_candidates(intent.file_query, catalog)
    if not considered:
        message = (
            "授权目录中没有可检索的普通文件；请检查目录或手动选择文件。"
            if not catalog
            else f"已扫描 {len(catalog)} 个文件，但没有符合条件的候选；请调整描述。"
        )
        return IntentSearchResult(
            intent, (), (), message, "local", request, authorized_root, len(catalog),
        )
    if not (
        intent.file_query.keywords
        or intent.file_query.extensions
        or intent.file_query.modified_after
    ):
        selected = considered[:10]
        clarification = (
            "按修改时间展示最近的候选；如未看到目标文件，请补充文件名或类型。"
        )
        if intent.clarification_question:
            clarification = f"{clarification} {intent.clarification_question}"
        return IntentSearchResult(
            intent, selected, considered, clarification, "local", request, authorized_root,
            len(catalog),
        )
    try:
        ranked = rank_candidates_with_ollama(request, list(considered), ollama_url, model)
        selected = ranked.candidates
        source = "ollama"
        clarification = (
            "模型未选出有效候选，请修改描述或手动选择文件。"
            if not selected
            else "找到多份候选，请逐项核对并选择一份。"
            if len(selected) > 1
            else ""
        )
    except FileCatalogError:
        selected = considered[:10]
        source = "local"
        clarification = "Ollama 未完成排序，显示本地匹配结果；请人工核对。"
    if intent.clarification_question:
        clarification = f"{clarification} {intent.clarification_question}".strip()
    return IntentSearchResult(
        intent, selected, considered, clarification, source, request, authorized_root,
        len(catalog),
    )
