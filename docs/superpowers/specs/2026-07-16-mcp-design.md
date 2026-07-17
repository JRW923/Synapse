# MCP 协议支持 — 设计 Spec

> 为 Synapse 添加 MCP (Model Context Protocol) Client 能力，使其能动态发现和调用任意 MCP Server 提供的工具。

## 架构

遵循 Synapse 现有 Protocol → 实现 → Container 注入模式：

```
protocols/mcp.py          → McpClient Protocol + McpServerConfig dataclass
modules/mcp/
  ├── official_sdk.py     → OfficialSdkMcpClient（基于 mcp 包）
  ├── manager.py          → McpManager（多连接管理 + ToolRegistry 集成）
  └── wrappers.py         → McpToolWrapper（MCP tool → Synapse Tool Protocol）
```

## Protocol 设计

```python
class McpClient(Protocol):
    async def connect(self, config: McpServerConfig) -> None: ...
    async def list_tools(self) -> list[dict]: ...       # MCP tool schemas
    async def call_tool(self, name: str, args: dict) -> dict: ...
    async def disconnect(self) -> None: ...
    @property
    def connected(self) -> bool: ...
```

## Transport 支持

- **stdio**：启动 MCP Server 子进程，JSON-RPC over stdin/stdout
- **Streamable HTTP**：连接远程 MCP Server 的 HTTP 端点

## McpManager

- 管理多个 McpClient 连接
- `add_server(config)` → 连接 → 发现 tools → 包装 → 注册到 ToolRegistry
- `remove_server(name)` → 注销 tools → 断开连接
- tools 命名规则：`mcp.<server_name>.<tool_name>`（避免与内置工具冲突）

## 集成点

- `Synapse(mcp_servers=[...])` — 通过 facade 配置 MCP Server 列表
- CLI：`synapse run --mcp-server "name:cmd:args" --mcp-server "name2:url"`
- MCP 工具的 risk_level 由用户在配置中声明（默认 READ_ONLY）

## Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| 用官方 SDK vs 自实现 | 官方 mcp 包 | 协议细节（JSON-RPC 帧、初始化握手、能力协商）已有成熟实现 |
| 工具命名空间 | `mcp.<server>.<tool>` | 避免与 10 个内置工具名冲突 |
| Resources 支持 | 首期不做 | Resources 本质是只读数据源，可映射为 READ_ONLY 工具，后续迭代 |
| 动态注册 vs 静态 | 动态注册到现有 ToolRegistry | 对 Agent 完全透明，MCP 工具和内置工具无区别 |
