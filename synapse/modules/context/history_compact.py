"""Conversation-history compaction used by ReAct, /compact, and overflow retry."""

from __future__ import annotations

from dataclasses import dataclass

from synapse.protocols.llm import Message

# Keep in sync with PlanningConfig defaults.
DEFAULT_SOFT_CHARS = 120_000
DEFAULT_KEEP_RECENT_TOOLS = 6
DEFAULT_KEEP_RECENT_TURNS = 4
DEFAULT_ROTATE_AFTER = 3
MIN_TOOL_CHARS = 200
PROTECTED_TOOLS = {"read", "grep", "glob"}

_COMPACT_PHRASES = frozenset({
    "/compact", "compact",
    "压缩", "压缩一下", "请压缩", "请压缩一下",
    "压缩上下文", "请压缩上下文", "压缩对话", "请压缩对话",
})


@dataclass
class CompactReport:
    changed: bool
    level: str  # none | l1 | l2
    chars_before: int
    chars_after: int
    compact_count: int
    rotate_hint: bool
    summary: str

    def to_dict(self) -> dict:
        return {
            "changed": self.changed,
            "level": self.level,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "compact_count": self.compact_count,
            "rotate_hint": self.rotate_hint,
            "summary": self.summary,
        }


def is_compact_request(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    head = raw.split(None, 1)[0].lower()
    if head == "/compact":
        return True
    return raw.lower() in _COMPACT_PHRASES


def message_chars(messages: list[Message]) -> int:
    return sum(len(getattr(m, "content", "") or "") for m in messages)


def _tool_name(tc: dict) -> str:
    if not isinstance(tc, dict):
        return ""
    name = tc.get("name")
    if name:
        return str(name)
    fn = tc.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return ""


def _protected_tool_indices(messages: list[Message], keep_recent_tools: int) -> set[int]:
    tool_idx = [i for i, m in enumerate(messages) if getattr(m, "role", None) == "tool"]
    protected = set(tool_idx[-keep_recent_tools:]) if keep_recent_tools else set()
    id_to_name: dict[str, str] = {}
    for msg in messages:
        if getattr(msg, "role", None) != "assistant":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            tc_id = (tc or {}).get("id") if isinstance(tc, dict) else None
            if tc_id:
                id_to_name[str(tc_id)] = _tool_name(tc)
    read_idx = [
        i for i in tool_idx
        if id_to_name.get(getattr(messages[i], "tool_call_id", "") or "") in PROTECTED_TOOLS
    ]
    if keep_recent_tools:
        protected.update(read_idx[-keep_recent_tools:])
    return protected


def _elidable_tool_indices(messages: list[Message], keep_recent_tools: int) -> list[int]:
    protected = _protected_tool_indices(messages, keep_recent_tools)
    victims: list[int] = []
    for i, msg in enumerate(messages):
        if i in protected or getattr(msg, "role", None) != "tool":
            continue
        content = getattr(msg, "content", "") or ""
        if len(content) <= MIN_TOOL_CHARS:
            continue
        if "[elided" in content or "[compacted" in content:
            continue
        victims.append(i)
    return victims


async def _llm_summary(llm, prompt: str) -> str:
    from synapse.modules.context.llm_compactor import MAX_INPUT_CHARS, MAX_SUMMARY_CHARS
    if llm is None:
        return ""
    try:
        response = await llm.chat([
            Message(role="system",
                    content="You are a concise context summarizer for a coding agent."),
            Message(role="user", content=prompt[:MAX_INPUT_CHARS]),
        ], tools=None)
        return (response.content or "").strip()[:MAX_SUMMARY_CHARS]
    except Exception:
        return ""


_TOOL_SUMMARY_CACHE: dict[str, str] = {}


async def _summarize_tool_victims(messages: list[Message], victims: list[int], llm) -> str:
    import hashlib
    from synapse.modules.context.llm_compactor import MAX_INPUT_CHARS
    joined = "\n\n".join(
        f"--- tool result #{n} ---\n{(messages[i].content or '')}"
        for n, i in enumerate(victims, start=1)
    )[:MAX_INPUT_CHARS]
    key = hashlib.sha1(f"{id(llm)}:{joined}".encode("utf-8", errors="ignore")).hexdigest()
    cached = _TOOL_SUMMARY_CACHE.get(key)
    if cached is not None:
        return cached
    summary = await _llm_summary(
        llm,
        "Summarize these past tool results into one dense reference. "
        "Preserve file paths, symbol names, error messages, key "
        "findings and decisions. Drop prose.\n\n" + joined,
    )
    _TOOL_SUMMARY_CACHE[key] = summary
    return summary


async def compact_l1(messages: list[Message], llm, strategy: str,
                     keep_recent_tools: int) -> bool:
    victims = _elidable_tool_indices(messages, keep_recent_tools)
    if not victims:
        return False
    if strategy == "llm" and llm is not None:
        summary = await _summarize_tool_victims(messages, victims, llm)
        if summary:
            first = victims[0]
            messages[first].content = (
                f"[compacted summary of {len(victims)} older tool results]\n{summary}"
            )
            for i in victims[1:]:
                messages[i].content = "[elided → summarized above]"
            return True
    for i in victims:
        messages[i].content = "[elided older tool result to save context]"
    return True


def _turn_excerpt(messages: list[Message]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", "")
        content = (getattr(msg, "content", "") or "").strip()
        if not content:
            continue
        if role in {"user", "system"}:
            parts.append(f"{role}: {content}")
        elif role == "assistant":
            parts.append(f"assistant: {content}")
        elif role == "tool" and "[elided" not in content:
            parts.append(f"tool: {content[:400]}")
    return "\n\n".join(parts)


async def _summarize_turns(old: list[Message], llm) -> str:
    from synapse.modules.context.llm_compactor import MAX_INPUT_CHARS, MAX_SUMMARY_CHARS
    joined = _turn_excerpt(old)
    if not joined:
        return ""
    if llm is None:
        return joined[:MAX_SUMMARY_CHARS]
    chunks = [joined[i:i + MAX_INPUT_CHARS] for i in range(0, len(joined), MAX_INPUT_CHARS)][:4]
    partials: list[str] = []
    for chunk in chunks:
        piece = await _llm_summary(
            llm,
            "Summarize earlier conversation for a coding agent. Preserve file "
            "paths, decisions, errors, and current task state. Drop chatter.\n\n" + chunk,
        )
        if piece:
            partials.append(piece)
    if not partials:
        return joined[:MAX_SUMMARY_CHARS]
    if len(partials) == 1:
        return partials[0]
    merged = await _llm_summary(llm, "Merge these partial summaries. Keep facts, drop repeats.\n\n"
                                + "\n\n".join(partials))
    return (merged or "\n".join(partials))[:MAX_SUMMARY_CHARS]


async def compact_l2(messages: list[Message], llm, keep_recent_turns: int) -> bool:
    user_idx = [i for i, m in enumerate(messages) if getattr(m, "role", None) == "user"]
    if len(user_idx) <= keep_recent_turns:
        return False
    cut = user_idx[-keep_recent_turns]
    head_end = 1 if messages and getattr(messages[0], "role", None) == "system" else 0
    if cut <= head_end:
        return False
    old = messages[head_end:cut]
    if not old:
        return False
    summary = await _summarize_turns(old, llm)
    if not summary:
        summary = f"{len(old)} older turns folded to save context."
    note = Message(
        role="system",
        content=f"[compacted earlier conversation]\n{summary}",
    )
    messages[head_end:cut] = [note]
    return True


def _human_summary(level: str, before: int, after: int, count: int, rotate: bool) -> str:
    if level == "none":
        return "没有需要压缩的内容。"
    text = f"已做 {level.upper()} 压缩：{before} → {after} 字符（第 {count} 次）"
    if rotate:
        text += "。已多次压缩，建议开新会话继续，以免摘要失真。"
    return text


async def compact_history(
    messages: list[Message],
    *,
    llm=None,
    session_meta: dict | None = None,
    force: bool = False,
    soft_chars: int = DEFAULT_SOFT_CHARS,
    keep_recent_tools: int = DEFAULT_KEEP_RECENT_TOOLS,
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS,
    rotate_after: int = DEFAULT_ROTATE_AFTER,
    strategy: str = "elide",
) -> CompactReport:
    meta = session_meta if session_meta is not None else {}
    before = message_chars(messages)
    level = "none"
    changed = False
    if force or before > soft_chars:
        if await compact_l1(messages, llm, strategy, keep_recent_tools):
            changed = True
            level = "l1"
        after_l1 = message_chars(messages)
        if force or after_l1 > soft_chars:
            if await compact_l2(messages, llm, keep_recent_turns):
                changed = True
                level = "l2"
    after = message_chars(messages)
    count = int(meta.get("compact_count") or 0)
    if changed:
        count += 1
        meta["compact_count"] = count
        meta["last_compact"] = {
            "level": level,
            "chars_before": before,
            "chars_after": after,
        }
    rotate = count >= rotate_after
    return CompactReport(
        changed=changed,
        level=level,
        chars_before=before,
        chars_after=after,
        compact_count=count,
        rotate_hint=rotate and changed,
        summary=_human_summary(level, before, after, count, rotate and changed),
    )
