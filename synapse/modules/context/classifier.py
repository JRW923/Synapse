"""Task type classifier — rule-based, no LLM cost.

Phase 3 (E): classifies a task string into a TaskType so the budget
allocator can pick the right four-zone profile. Deliberately simple —
keyword matching only. Verified effective before considering LLM
classification (which costs tokens).
"""

import re
from enum import Enum


class TaskType(str, Enum):
    TEST = "test"
    REFACTOR = "refactor"
    DEBUG = "debug"
    FEATURE = "feature"
    DOC = "doc"
    UNKNOWN = "unknown"


# Keyword sets — order matters: more specific patterns first.
# Each entry: (TaskType, list-of-patterns). First match wins.
# DEBUG before TEST: 'fix failing test' should be DEBUG (the user is
# debugging a test failure, not writing new tests).
_RULES: list[tuple[TaskType, list[str]]] = [
    (
        TaskType.DEBUG,
        [
            r"\bdebug\b", r"\bfix\b", r"\bbug\b", r"\berror\b", r"\bcrash\b",
            r"\bstack[- ]?trace\b", r"\btraceback\b", r"\bfail(?:ed|ure)?\b",
            r"\bexception\b", r"修复", r"调试", r"报错",
        ],
    ),
    (
        TaskType.TEST,
        [
            r"\btest\b", r"\btests\b", r"\btesting\b", r"\bunit[- ]?test",
            r"\bspec\b", r"\bpytest\b", r"\bunittest\b", r"\bjest\b",
            r"\bcoverage\b", r"测试",
        ],
    ),
    (
        TaskType.REFACTOR,
        [
            r"\brefactor\b", r"\brename\b", r"\bextract\b", r"\binline\b",
            r"\bmove\b", r"\bclean[- ]?up\b", r"重构", r"重命名",
        ],
    ),
    (
        TaskType.DOC,
        [
            r"\bdoc(?:s|umentation)?\b", r"\breadme\b", r"\bchangelog\b",
            r"\bcomment\b", r"\bjavadoc\b", r"\bdocstring\b", r"文档",
        ],
    ),
    (
        TaskType.FEATURE,
        [
            r"\badd\b", r"\bimplement\b", r"\bcreate\b", r"\bbuild\b",
            r"\bnew\b", r"\bsupport\b", r"\bgenerate\b", r"\bintroduce\b",
            r"新增", r"实现", r"创建",
        ],
    ),
]

# Pre-compile patterns for performance.
_COMPILED: list[tuple[TaskType, list[re.Pattern]]] = [
    (tt, [re.compile(p, re.IGNORECASE) for p in patterns])
    for tt, patterns in _RULES
]


def classify_task(task: str) -> TaskType:
    """Classify a task string into a TaskType using keyword rules.

    First-match-wins in the rule order: TEST → REFACTOR → DEBUG →
    DOC → FEATURE → UNKNOWN. Order is chosen so that more specific
    intents (e.g. 'fix the test') are caught by DEBUG if the keyword
    'fix' appears — but 'test' alone is still TEST if no debug keyword.

    Heuristic, not exhaustive — UNKNOWN falls back to the default profile.
    """
    if not task:
        return TaskType.UNKNOWN
    text = task.lower()
    for task_type, patterns in _COMPILED:
        for p in patterns:
            if p.search(text):
                return task_type
    return TaskType.UNKNOWN
