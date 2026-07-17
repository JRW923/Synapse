"""Database tool — read-only SQLite access by default, optional write mode."""

import sqlite3
from pathlib import Path

from synapse.protocols.tool import (
    Tool,
    ToolSchema,
    ToolResult,
    ToolCallMetadata,
    RiskLevel,
    ToolCategory,
)

_SELECT_STATEMENTS = frozenset({"SELECT", "WITH", "EXPLAIN", "PRAGMA"})


class DBTool:
    """Execute SQL queries against a SQLite database file.

    Read-only by default (SELECT / WITH / EXPLAIN / PRAGMA only).
    Set ``write=True`` in params to allow INSERT / UPDATE / DELETE / DDL.
    """

    name = "db"
    description = (
        "Execute SQL queries against a SQLite database file. "
        "Read-only by default (SELECT only). "
        "Set write=True to allow INSERT, UPDATE, DELETE, and DDL statements."
    )
    parameters = ToolSchema(
        name="db",
        description="SQLite database query tool",
        parameters={
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Absolute path to the SQLite .db file",
                },
                "query": {
                    "type": "string",
                    "description": "SQL query to execute",
                },
                "write": {
                    "type": "boolean",
                    "description": "Allow write operations (INSERT/UPDATE/DELETE/DDL). "
                    "Defaults to false.",
                    "default": False,
                },
            },
            "required": ["db_path", "query"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.EXTERNAL
    category = ToolCategory.INTEGRATION

    def __init__(self, workspace_root: str = "."):
        self._workspace_root = Path(workspace_root).resolve()

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        db_path = params["db_path"]
        query = params["query"]
        write = params.get("write", False)
        meta = ToolCallMetadata(tool_name="db")

        # --- Auth: file must be inside workspace ---
        try:
            resolved = Path(db_path).resolve()
        except (ValueError, OSError):
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid database path: {db_path!r}",
                metadata=meta,
            )

        if not str(resolved).startswith(str(self._workspace_root)):
            return ToolResult(
                success=False,
                output="",
                error=f"Database path {db_path!r} is outside the workspace",
                metadata=meta,
            )

        # --- File must exist ---
        if not resolved.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"Database file not found: {db_path!r}",
                metadata=meta,
            )

        # --- Read-only guard ---
        statement_type = _classify_statement(query)
        if not write and statement_type not in _SELECT_STATEMENTS:
            return ToolResult(
                success=False,
                output="",
                error=f"Write operation '{statement_type}' blocked: set write=True to allow modifications",
                metadata=meta,
            )

        # --- Execute query ---
        try:
            conn = sqlite3.connect(str(resolved))
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(query)
                if statement_type in _SELECT_STATEMENTS:
                    rows = cursor.fetchall()
                    output = _format_rows(cursor, rows)
                else:
                    conn.commit()
                    output = f"Query OK, {cursor.rowcount} row(s) affected"
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata=meta,
            )

        return ToolResult(success=True, output=output, metadata=meta)


def _classify_statement(query: str) -> str:
    """Return the first keyword of *query*, uppercased."""
    stripped = query.strip()
    if not stripped:
        return ""
    first_word = stripped.split(None, 1)[0].upper()
    return first_word


def _format_rows(cursor, rows: list[sqlite3.Row]) -> str:
    """Format query result rows as a readable text table."""
    if not rows:
        return "(empty result set)"

    col_names = [desc[0] for desc in cursor.description or []]
    col_widths = [len(name) for name in col_names]

    str_rows: list[list[str]] = []
    for row in rows:
        values = [str(row[i]) if row[i] is not None else "NULL" for i in range(len(col_names))]
        str_rows.append(values)
        for i, val in enumerate(values):
            col_widths[i] = max(col_widths[i], len(val))

    lines: list[str] = []
    # Header
    header = " | ".join(name.ljust(col_widths[i]) for i, name in enumerate(col_names))
    lines.append(header)
    lines.append("-+-".join("-" * col_widths[i] for i in range(len(col_names))))
    # Rows
    for vals in str_rows:
        lines.append(" | ".join(val.ljust(col_widths[i]) for i, val in enumerate(vals)))

    return "\n".join(lines)
