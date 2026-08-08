"""Context retriever — file tree + relevance ranking + AST symbols.

Replaces the old prototype (grep 3 keywords + glob the first 20 files
alphabetically) with a real retriever:

* file list comes from ``git ls-files`` (so ``.gitignore`` is respected for
  free), falling back to a filtered walk outside a repo;
* every candidate file is scored by idf-weighted term frequency of the task
  against its path, its Python symbol names and its body;
* the top files are injected as excerpts *around the matching lines*, not as
  a blind head-truncation;
* a file-tree overview block gives the model a map of the repo.

No rg/glob subprocesses are involved anymore — we read the files once and
derive tree, scores, symbols and excerpts from that single pass.
"""

from __future__ import annotations

import ast
import asyncio
import math
import re
import subprocess
from pathlib import Path

from synapse.protocols.retriever import (
    Context, ContextBlock, ContextBudget, ContextSource,
)
from synapse.core.tokenizer import count_tokens

# ponytail: every retrieve() rescans the repo (O(files), one read per file to
# count terms).  MAX_FILES / MAX_FILE_BYTES are the guard rails.  For a repo
# large enough to feel this, swap in a persistent index (sqlite FTS5 / tantivy)
# behind the same _rank() call — nothing else needs to change.
MAX_FILES = 1500
MAX_FILE_BYTES = 200_000
MAX_TREE_ENTRIES = 300
TOP_FILES = 8
MIN_EXCERPT_CHARS = 1200
EXCERPT_CONTEXT_LINES = 4

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", "venv", "env", "node_modules", "dist",
    "build", "target", ".next", ".idea", ".vscode", "site-packages",
})

_TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".sh", ".bash", ".sql", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml",
    ".json", ".ini", ".cfg", ".html", ".css", ".scss", ".vue", ".proto",
})

_STOP = frozenset({
    "the", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "can", "could", "should",
    "an", "and", "or", "but", "if", "then", "else", "when", "where", "why",
    "how", "all", "any", "each", "some", "such", "no", "not", "only", "so",
    "than", "too", "very", "just", "in", "on", "at", "to", "of", "for",
    "from", "by", "as", "into", "with", "about", "it", "its", "this", "that",
    "these", "those", "we", "you", "they", "me", "my", "your", "our", "their",
    "please", "make", "need", "want", "add", "use", "using", "file", "code",
})

# ponytail: ASCII-only tokenizer.  A Chinese task scores through the English
# identifiers it carries ("修复 retriever 的 _build_core"); a task with zero
# ASCII terms degrades to the shallow-path ranking in _rank().
_TOKEN_RE = re.compile(r"[^a-zA-Z0-9_]+")


class BasicContextRetriever:
    """Ranks repository files against the task and builds a four-zone Context."""

    def __init__(self) -> None:
        # (rel, len, hash) -> symbol names; see _symbols.
        self._symbol_cache: dict[tuple, list[str]] = {}
        # resolved path -> (mtime_ns, size, decoded content)
        self._content_cache: dict[Path, tuple[int, int, str]] = {}

    async def retrieve(
        self,
        task: str,
        project_root: Path,
        tools,
        memory,
        budget: ContextBudget | None = None,
    ) -> Context:
        ctx = Context()
        budget = budget or ContextBudget()

        # 1. SYSTEM: project instructions
        ctx.system = await self._build_system(project_root)

        # 2. CORE: file tree + task-ranked source files (blocking I/O off-loop)
        ctx.core = await asyncio.to_thread(
            self._build_core, task, project_root, budget,
        )

        # 3. REFERENCE: session memory entries related to task
        ctx.reference = await self._build_reference(task, memory)

        # 4. OVERFLOW: route reference results that exceed the reference budget
        #    into the overflow zone so the ContextCompactor can summarize them.
        #    ponytail: overflow is populated here, but react.py does NOT inject
        #    it directly — agent._build_context folds the *compacted* overflow
        #    back into `reference` so the LLM actually consumes the summary.
        ctx.reference, ctx.overflow = self._route_overflow(ctx.reference, budget)

        return ctx

    def _route_overflow(
        self, blocks: list[ContextBlock], budget: ContextBudget,
    ) -> tuple[list[ContextBlock], list[ContextBlock]]:
        """Split reference blocks into (kept, overflow) by the reference budget.

        Keeps highest-priority blocks in ``reference`` up to
        ``reference_pct * total_tokens``; the rest go to ``overflow`` for
        compaction.  Original ordering is preserved within each bucket.
        """
        ref_budget = int(budget.total_tokens * budget.reference_pct)
        if ref_budget <= 0 or not blocks:
            return blocks, []
        kept: list[ContextBlock] = []
        overflow: list[ContextBlock] = []
        used = 0
        # Highest priority first; stable sort preserves input order on ties.
        for b in sorted(blocks, key=lambda x: -x.priority):
            size = b.token_count or 0
            if used + size <= ref_budget or not kept:
                kept.append(b)
                used += size
            else:
                overflow.append(b)
        kept_ids = {id(b) for b in kept}
        kept_sorted = [b for b in blocks if id(b) in kept_ids]
        overflow_sorted = [b for b in blocks if id(b) not in kept_ids]
        return kept_sorted, overflow_sorted

    async def _build_system(self, project_root: Path) -> list[ContextBlock]:
        blocks = []
        # Look for CLAUDE.md / AGENTS.md style project instructions
        for name in ["CLAUDE.md", "AGENTS.md", "README.md"]:
            f = project_root / name
            if f.exists():
                content = f.read_text(encoding="utf-8", errors="replace")
                blocks.append(ContextBlock(
                    content=content,
                    source=ContextSource.MEMORY,
                    priority=9,
                    token_count=count_tokens(content),
                ))
        return blocks

    # ------------------------------------------------------------------
    # CORE zone
    # ------------------------------------------------------------------

    def _build_core(
        self, task: str, project_root: Path, budget: ContextBudget,
    ) -> list[ContextBlock]:
        files = self._list_files(project_root)
        if not files:
            return []

        terms = self._tokenize(task)
        ranked = self._rank(terms, project_root, files)

        blocks: list[ContextBlock] = [self._tree_block(files, ranked)]

        # Character budget for the CORE zone, minus what the tree already ate.
        core_chars = int(budget.total_tokens * budget.core_pct * 4)
        remaining = core_chars - len(blocks[0].content)
        top = [r for r in ranked if r[0] > 0][:TOP_FILES] or ranked[:3]
        per_file = max(MIN_EXCERPT_CHARS, remaining // max(len(top), 1))

        for score, rel, symbols in top:
            if remaining <= 0:
                break
            text = self._read(project_root / rel)
            if not text:
                continue
            excerpt = self._excerpt(text, terms, min(per_file, remaining))
            header = f"# File: {rel}  (relevance {score:.1f})"
            if symbols:
                header += "\n# Symbols: " + ", ".join(symbols[:30])
            content = f"{header}\n\n{excerpt}"
            remaining -= len(content)
            blocks.append(ContextBlock(
                content=content,
                # AST when we actually parsed symbols out of it, GLOB otherwise
                # — both are classified as deterministic by the InjectionGuard.
                source=ContextSource.AST if symbols else ContextSource.GLOB,
                priority=8,
                token_count=count_tokens(content),
            ))
        return blocks

    def _tree_block(self, files: list[str], ranked) -> ContextBlock:
        """Repo map: the most relevant paths first, then the rest, capped."""
        order = [rel for _, rel, _ in ranked] or files
        shown = order[:MAX_TREE_ENTRIES]
        body = "\n".join(shown)
        if len(order) > len(shown):
            body += f"\n... (+{len(order) - len(shown)} more files)"
        content = f"# Project files ({len(files)} tracked, most relevant first)\n{body}"
        return ContextBlock(
            content=content,
            source=ContextSource.GLOB,
            priority=7,
            token_count=count_tokens(content),
        )

    # ------------------------------------------------------------------
    # File listing
    # ------------------------------------------------------------------

    def _list_files(self, project_root: Path) -> list[str]:
        """Relative paths of candidate text files, .gitignore-aware."""
        rels = self._git_files(project_root)
        if rels is None:
            rels = self._walk_files(project_root)
        keep = [
            r for r in rels
            if Path(r).suffix.lower() in _TEXT_SUFFIXES
            and not any(part in _SKIP_DIRS for part in Path(r).parts)
        ]
        keep.sort()
        return keep[:MAX_FILES]

    @staticmethod
    def _git_files(project_root: Path) -> list[str] | None:
        """``git ls-files`` = tracked + untracked, .gitignore already applied."""
        try:
            proc = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=str(project_root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None  # not a repo / git missing → caller walks instead
        return [line for line in proc.stdout.splitlines() if line.strip()]

    @staticmethod
    def _walk_files(project_root: Path) -> list[str]:
        out: list[str] = []
        stack = [project_root]
        while stack and len(out) < MAX_FILES * 2:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in _SKIP_DIRS and not entry.is_symlink():
                        stack.append(entry)
                elif entry.is_file():
                    out.append(entry.relative_to(project_root).as_posix())
        return out

    def _read(self, path: Path) -> str | None:
        try:
            resolved = path.resolve()
            stat = resolved.stat()
            if stat.st_size > MAX_FILE_BYTES:
                return None
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._content_cache.get(resolved)
            if cached is not None and cached[:2] == signature:
                return cached[2]
            text = resolved.read_text(encoding="utf-8", errors="replace")
            if len(self._content_cache) > MAX_FILES * 4:
                self._content_cache.clear()
            self._content_cache[resolved] = (*signature, text)
            return text
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    def _rank(
        self, terms: list[str], project_root: Path, files: list[str],
    ) -> list[tuple[float, str, list[str]]]:
        """Score every file; returns (score, rel_path, symbols) best-first."""
        if not terms:
            # No usable terms — shallow paths first is the least-bad guess.
            return [(0.0, rel, []) for rel in sorted(files, key=lambda r: r.count("/"))]

        stats: list[tuple[str, dict[str, int], list[str], set[str]]] = []
        doc_freq: dict[str, int] = {}
        for rel in files:
            text = self._read(project_root / rel)
            if text is None:
                continue
            low = text.lower()
            tf = {t: low.count(t) for t in terms}
            tf = {t: n for t, n in tf.items() if n}
            for t in tf:
                doc_freq[t] = doc_freq.get(t, 0) + 1
            symbols = self._symbols(rel, text)
            stats.append((rel, tf, symbols, {s.lower() for s in symbols}))

        total = max(len(stats), 1)
        idf = {t: math.log(1 + total / (1 + doc_freq.get(t, 0))) for t in terms}

        ranked: list[tuple[float, str, list[str]]] = []
        for rel, tf, symbols, sym_low in stats:
            low_rel = rel.lower()
            score = 0.0
            for t in terms:
                w = idf[t]
                n = tf.get(t, 0)
                if n:
                    score += w * (1 + math.log(n))       # saturating tf
                if t in low_rel:
                    score += w * 4.0                     # path hit is a strong signal
                if any(t in s for s in sym_low):
                    score += w * 2.0                     # named function/class
            ranked.append((score, rel, symbols))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return ranked

    def _symbols(self, rel: str, text: str) -> list[str]:
        """Top-level-ish function/class names. ponytail: Python only —
        other languages fall back to plain text scoring.

        ast.parse dominates retrieval cost (~200ms of a ~450ms pass on this
        repo), and a file's symbols only change when the file does, so results
        are memoized on content length + a hash of the source.
        """
        if not rel.endswith(".py"):
            return []
        key = (rel, len(text), hash(text))
        hit = self._symbol_cache.get(key)
        if hit is not None:
            return hit
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            names: list[str] = []
        else:
            names = [
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ][:60]
        if len(self._symbol_cache) > 4000:  # crude bound; repos churn slowly
            self._symbol_cache.clear()
        self._symbol_cache[key] = names
        return names

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Lowercase ASCII tokens plus their snake_case parts, deduped."""
        out: list[str] = []
        for raw in _TOKEN_RE.split(text.lower()):
            if len(raw) >= 2 and raw not in _STOP:
                out.append(raw)
            if "_" in raw:
                out.extend(p for p in raw.split("_") if len(p) >= 3 and p not in _STOP)
        return list(dict.fromkeys(out))[:12]

    @staticmethod
    def _excerpt(text: str, terms: list[str], max_chars: int) -> str:
        """Windows around matching lines; head of file when nothing matches."""
        max_chars = max(max_chars, 200)
        if len(text) <= max_chars:
            return text
        lines = text.splitlines()
        hits = [
            i for i, line in enumerate(lines)
            if any(t in line.lower() for t in terms)
        ]
        if not hits:
            return text[:max_chars] + "\n... (truncated)"

        keep: set[int] = set()
        for i in hits:
            keep.update(range(
                max(0, i - EXCERPT_CONTEXT_LINES),
                min(len(lines), i + EXCERPT_CONTEXT_LINES + 1),
            ))
        parts: list[str] = []
        used = 0
        previous = -2
        for i in sorted(keep):
            if i != previous + 1 and parts:
                parts.append("...")
                used += 4
            chunk = f"{i + 1:6}\t{lines[i]}"
            if used + len(chunk) > max_chars:
                parts.append("... (truncated)")
                break
            parts.append(chunk)
            used += len(chunk) + 1
            previous = i
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # REFERENCE zone
    # ------------------------------------------------------------------

    async def _build_reference(self, task: str, memory) -> list[ContextBlock]:
        blocks = []
        try:
            from synapse.protocols.memory import MemoryLevel
            entries = await memory.retrieve(task, MemoryLevel.SESSION, top_k=3)
            for entry in entries:
                blocks.append(ContextBlock(
                    content=entry.content,
                    source=ContextSource.MEMORY,
                    priority=5,
                    token_count=count_tokens(entry.content),
                ))

            # Semantic memory (vector layer, optional backend): retrieve prior
            # task summaries by similarity. Empty when the backend is absent or
            # nothing has been stored yet, so this never adds noise.
            sem = await memory.retrieve(task, MemoryLevel.SEMANTIC, top_k=3)
            for entry in sem:
                if entry.content not in {b.content for b in blocks}:
                    blocks.append(ContextBlock(
                        content=entry.content,
                        source=ContextSource.MEMORY,
                        priority=4,
                        token_count=count_tokens(entry.content),
                    ))

            # User memory is intentionally lower priority than project/session
            # context, but it must be visible for cross-project preferences.
            user = await memory.retrieve(task, MemoryLevel.USER, top_k=3)
            for entry in user:
                if entry.content not in {b.content for b in blocks}:
                    blocks.append(ContextBlock(
                        content=entry.content,
                        source=ContextSource.MEMORY,
                        priority=3,
                        token_count=count_tokens(entry.content),
                    ))

            # Pull the rolling process-quality feedback (fixed id/tag) and inject
            # it into the next task's reference context so the agent sees its own
            # prior process-quality hint. Stored at PROJECT level, retrieved by a
            # stable query that matches its content sentinel.
            fb = await memory.retrieve(
                "process quality feedback", MemoryLevel.PROJECT, top_k=1,
            )
            for entry in fb:
                if entry.content not in {b.content for b in blocks}:
                    blocks.append(ContextBlock(
                        content=entry.content,
                        source=ContextSource.MEMORY,
                        priority=6,
                        token_count=count_tokens(entry.content),
                    ))
        except Exception:
            pass
        return blocks
