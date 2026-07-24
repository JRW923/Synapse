"""Basic context retriever — builds context from tools + memory."""

from pathlib import Path
from synapse.protocols.retriever import (
    Context, ContextBlock, ContextBudget, ContextSource,
)


class BasicContextRetriever:
    """Phase 1 context retriever — uses grep/glob for CORE, memory for REFERENCE.

    Future phases will add AST indexing, git history, and four-zone budgeting.
    """

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

        # 2. CORE: grep for relevant code based on task
        ctx.core = await self._build_core(task, project_root, tools)

        # 3. REFERENCE: session memory entries related to task
        ctx.reference = await self._build_reference(task, memory)

        return ctx

    async def _build_system(self, project_root: Path) -> list[ContextBlock]:
        blocks = []
        # Look for CLAUDE.md / AGENTS.md style project instructions
        for name in ["CLAUDE.md", "AGENTS.md", "README.md"]:
            f = project_root / name
            if f.exists():
                content = f.read_text(encoding="utf-8")
                blocks.append(ContextBlock(
                    content=content,
                    source=ContextSource.MEMORY,
                    priority=9,
                    token_count=len(content) // 4,  # rough estimate
                ))
        return blocks

    async def _build_core(self, task: str, project_root: Path, tools) -> list[ContextBlock]:
        blocks = []
        keywords = self._extract_keywords(task)

        # Try grep for each keyword
        try:
            grep = tools.get("grep")
            for kw in keywords[:3]:
                result = await grep.execute({"pattern": kw, "path": str(project_root)})
                if result.success and result.output and result.output != "(no matches)":
                    blocks.append(ContextBlock(
                        content=result.output[:5000],
                        source=ContextSource.GREP,
                        priority=8,
                        token_count=len(result.output[:5000]) // 4,
                    ))
        except (KeyError, Exception):
            pass

        # Also glob for relevant Python files
        try:
            glob = tools.get("glob")
            result = await glob.execute({"pattern": "**/*.py", "path": str(project_root)})
            if result.success and result.output:
                files = result.output.split("\n")[:20]
                for f in files:
                    p = project_root / f
                    if p.exists():
                        content = p.read_text(encoding="utf-8")
                        blocks.append(ContextBlock(
                            content=f"# File: {f}\n\n{content[:3000]}",
                            source=ContextSource.GLOB,
                            priority=7,
                            token_count=len(content[:3000]) // 4,
                        ))
        except (KeyError, Exception):
            pass

        return blocks

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
                    token_count=len(entry.content) // 4,
                ))

            # TODO B — pull the rolling process-quality feedback (fixed id/tag)
            # and inject it into the next task's reference context so the agent
            # sees its own prior process-quality hint.  Stored at PROJECT level,
            # retrieved by a stable query that matches its content sentinel.
            fb = await memory.retrieve(
                "process quality feedback", MemoryLevel.PROJECT, top_k=1,
            )
            for entry in fb:
                if entry.content not in {b.content for b in blocks}:
                    blocks.append(ContextBlock(
                        content=entry.content,
                        source=ContextSource.MEMORY,
                        priority=6,
                        token_count=len(entry.content) // 4,
                    ))
        except Exception:
            pass
        return blocks

    @staticmethod
    def _extract_keywords(task: str) -> list[str]:
        """Naive keyword extraction — split and filter short words."""
        words = task.split()
        return [w for w in words if len(w) > 2 and w.lower() not in {
            "the", "and", "for", "with", "that", "this", "from",
        }]
