"""Project memory — file-based storage in .synapse/memory/.

Reads/writes Markdown files with YAML frontmatter. Each entry is stored as
a YAML document (delimited by ---) within a file. Entries are grouped by type:
  architecture  → architecture.md
  conventions   → conventions.md
  pitfalls      → pitfalls.md
  decision      → decisions/YYYY-MM-DD-{id}.md
  other         → {id}.md

MEMORY.md serves as the index with one line per entry:
  - [Title](relative/path.md) — description
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


# Files that group multiple entries of the same type.
_GROUP_FILES = {"architecture", "conventions", "pitfalls"}


def _derive_type(tags: list[str]) -> str:
    """Derive the entry type from its tags.

    Returns the first tag that matches a known group, or "general" as fallback.
    """
    for tag in tags:
        if tag.lower() in _GROUP_FILES or tag.lower() == "decision":
            return tag.lower()
    # Use the first tag if present, otherwise general.
    return tags[0].lower() if tags else "general"


def _title_from_content(content: str) -> str:
    """Extract a short title from the first meaningful line of content."""
    for line in content.strip().splitlines():
        stripped = line.lstrip("#").strip()
        if stripped:
            # Truncate to a reasonable length.
            return stripped[:80]
    return "Untitled"


def _description_from_content(content: str) -> str:
    """Extract a brief description from the content (first ~100 chars)."""
    text = content.strip()
    # Remove leading heading markers.
    if text.startswith("#"):
        text = text.lstrip("#").strip()
    return text[:100]


def _format_frontmatter(metadata: dict) -> str:
    """Format a dictionary as YAML frontmatter string."""
    return yaml.dump(metadata, default_flow_style=False, allow_unicode=True, sort_keys=False).strip()


class ProjectMemory:
    """File-based memory store for the PROJECT level.

    Stores entries as Markdown files with YAML frontmatter inside
    ``.synapse/memory/``.  The directory is created automatically on the
    first write.
    """

    def __init__(self, base_path: str | Path = ".synapse/memory"):
        self._base_path = Path(base_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store(self, entry: MemoryEntry) -> None:
        """Persist *entry* to disk and update the index."""
        if entry.level != MemoryLevel.PROJECT:
            return
        self._ensure_dir()
        file_rel, file_abs = self._resolve_file(entry)
        self._append_entry(file_abs, entry)
        self._update_index(entry, file_rel)

    async def retrieve(
        self, query: str, level: MemoryLevel, top_k: int = 5
    ) -> list[MemoryEntry]:
        """Return entries matching *query* at the PROJECT level."""
        if level != MemoryLevel.PROJECT:
            return []
        if not self._base_path.exists():
            return []

        matches: list[MemoryEntry] = []
        for md_file in self._iter_md_files():
            for entry in self._read_entries(md_file):
                if self._matches(query, entry):
                    matches.append(entry)

        matches.sort(key=lambda e: e.metadata.priority, reverse=True)
        return matches[:top_k]

    async def forget(self, entry_id: str) -> None:
        """Remove the entry identified by *entry_id* from disk."""
        if not self._base_path.exists():
            return

        for md_file in self._iter_md_files():
            entries = self._read_entries(md_file)
            remaining = [e for e in entries if e.id != entry_id]
            if len(remaining) != len(entries):
                # Found and removed the entry.
                if remaining:
                    self._write_entries(md_file, remaining)
                else:
                    md_file.unlink()
                self._rebuild_index()
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        """Create the base directory (and decisions/ subdir) if needed."""
        self._base_path.mkdir(parents=True, exist_ok=True)
        decisions_dir = self._base_path / "decisions"
        decisions_dir.mkdir(exist_ok=True)

    def _resolve_file(self, entry: MemoryEntry) -> tuple[Path, Path]:
        """Return (relative_path, absolute_path) for *entry*."""
        entry_type = _derive_type(entry.metadata.tags)

        if entry_type == "decision":
            date_str = entry.metadata.timestamp.strftime("%Y-%m-%d")
            filename = f"{date_str}-{entry.id}.md"
            rel = Path("decisions") / filename
        elif entry_type in _GROUP_FILES:
            filename = f"{entry_type}.md"
            rel = Path(filename)
        else:
            filename = f"{entry.id}.md"
            rel = Path(filename)

        return rel, self._base_path / rel

    def _append_entry(self, file_abs: Path, entry: MemoryEntry) -> None:
        """Append *entry* as a YAML document to *file_abs*."""
        frontmatter = {
            "id": entry.id,
            "type": _derive_type(entry.metadata.tags),
            "timestamp": entry.metadata.timestamp.isoformat(),
            "priority": entry.metadata.priority,
            "tags": entry.metadata.tags,
        }
        fm_str = _format_frontmatter(frontmatter)

        doc = f"---\n{fm_str}\n---\n\n{entry.content}\n"

        if file_abs.exists():
            # Ensure separation between documents.
            existing = file_abs.read_text(encoding="utf-8")
            if not existing.endswith("\n"):
                doc = f"\n{doc}"
            file_abs.parent.mkdir(parents=True, exist_ok=True)
            file_abs.write_text(existing + doc, encoding="utf-8")
        else:
            file_abs.parent.mkdir(parents=True, exist_ok=True)
            file_abs.write_text(doc, encoding="utf-8")

    def _read_entries(self, file_path: Path) -> list[MemoryEntry]:
        """Parse all YAML-document entries from a single .md file."""
        if not file_path.exists():
            return []

        text = file_path.read_text(encoding="utf-8")
        entries: list[MemoryEntry] = []

        for fm_dict, body in self._split_documents(text):
            ts_str = fm_dict.get("timestamp", datetime.now().isoformat())
            try:
                timestamp = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                timestamp = datetime.now()

            entry = MemoryEntry(
                id=fm_dict.get("id", ""),
                content=body,
                level=MemoryLevel.PROJECT,
                metadata=MemoryMetadata(
                    timestamp=timestamp,
                    priority=fm_dict.get("priority", 5),
                    tags=fm_dict.get("tags", []),
                ),
            )
            entries.append(entry)

        return entries

    def _split_documents(self, text: str) -> list[tuple[dict, str]]:
        """Split multi-document YAML text into (frontmatter, body) pairs."""
        results: list[tuple[dict, str]] = []
        if not text.strip():
            return results

        # Normalize: ensure text starts with ---
        text = text.strip()
        if not text.startswith("---"):
            return results

        # Split by "\n---\n" but keep track of positions.
        # Walk through finding document boundaries.
        remaining = text
        while remaining.startswith("---"):
            remaining = remaining[3:]  # skip opening ---
            # Find closing ---
            idx = remaining.find("\n---")
            if idx == -1:
                # Unterminated document — treat remainder as body.
                fm_raw = remaining.strip()
                body = ""
            else:
                fm_raw = remaining[:idx].strip()
                remaining = remaining[idx + 4:]  # skip \n---
                # Find next document start or end.
                next_idx = remaining.find("\n---")
                if next_idx == -1:
                    body = remaining.strip()
                    remaining = ""
                else:
                    body = remaining[:next_idx].strip()
                    remaining = remaining[next_idx + 1:]  # skip \n for next ---

            fm_dict = yaml.safe_load(fm_raw) if fm_raw else {}
            if isinstance(fm_dict, dict):
                results.append((fm_dict, body))

        return results

    def _write_entries(self, file_abs: Path, entries: list[MemoryEntry]) -> None:
        """Rewrite *file_abs* with the given *entries*."""
        parts: list[str] = []
        for entry in entries:
            frontmatter = {
                "id": entry.id,
                "type": _derive_type(entry.metadata.tags),
                "timestamp": entry.metadata.timestamp.isoformat(),
                "priority": entry.metadata.priority,
                "tags": entry.metadata.tags,
            }
            fm_str = _format_frontmatter(frontmatter)
            parts.append(f"---\n{fm_str}\n---\n\n{entry.content}")
        file_abs.parent.mkdir(parents=True, exist_ok=True)
        file_abs.write_text("\n".join(parts) + "\n", encoding="utf-8")

    def _update_index(self, entry: MemoryEntry, file_rel: Path) -> None:
        """Add or refresh the index line for *entry* in MEMORY.md."""
        title = _title_from_content(entry.content)
        description = _description_from_content(entry.content)
        # Use forward slashes for the link to keep it portable.
        link = file_rel.as_posix()
        line = f"- [{title}]({link}) — {description}"

        lines = self._read_index_lines()
        # Remove any existing line that references the same file.
        lines = [l for l in lines if f"]({link})" not in l]
        lines.append(line)
        self._write_index_lines(lines)

    def _rebuild_index(self) -> None:
        """Rebuild the entire MEMORY.md from all md files on disk."""
        lines: list[str] = []
        for md_file in self._iter_md_files():
            for entry in self._read_entries(md_file):
                title = _title_from_content(entry.content)
                description = _description_from_content(entry.content)
                rel = md_file.relative_to(self._base_path).as_posix()
                lines.append(f"- [{title}]({rel}) — {description}")
        self._write_index_lines(lines)

    def _read_index_lines(self) -> list[str]:
        """Read lines from MEMORY.md, excluding the header if present."""
        if not self._base_path.exists():
            return []
        index_path = self._base_path / "MEMORY.md"
        if not index_path.exists():
            return []
        lines = index_path.read_text(encoding="utf-8").strip().splitlines()
        # Filter out top-level heading (e.g. "# Project Memory Index")
        return [l for l in lines if l.strip() and not l.strip().startswith("# ")]

    def _write_index_lines(self, lines: list[str]) -> None:
        """Write the index file with a header."""
        self._ensure_dir()
        index_path = self._base_path / "MEMORY.md"
        header = "# Project Memory Index\n\n"
        content = header + "\n".join(sorted(set(lines))) + "\n"
        index_path.write_text(content, encoding="utf-8")

    def _iter_md_files(self) -> list[Path]:
        """Yield all .md files in the memory directory (excluding MEMORY.md)."""
        if not self._base_path.exists():
            return []
        files: list[Path] = []
        for path in self._base_path.rglob("*.md"):
            if path.name == "MEMORY.md":
                continue
            files.append(path)
        return files

    @staticmethod
    def _matches(query: str, entry: MemoryEntry) -> bool:
        """Return True if *query* matches *entry*'s content, title, or tags."""
        q = query.lower()
        if q in entry.content.lower():
            return True
        if q in _title_from_content(entry.content).lower():
            return True
        if q in _description_from_content(entry.content).lower():
            return True
        if any(q in tag.lower() for tag in entry.metadata.tags):
            return True
        return False
