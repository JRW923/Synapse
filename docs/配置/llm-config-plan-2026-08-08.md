# LLM 配置体验调研与实施方案

## 1. 调研结论

Pi Agent 使用 `~/.pi/agent/models.json` 注册自定义 provider/model，并在
`settings.json` 中保存 `defaultProvider`、`defaultModel`。它的关键体验不是文件
数量，而是启动时自动加载、`/model` 可切换、用户不必每次传 CLI 参数。

参考资料：

- [Pi Custom Models](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md)
- [Pi Settings](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md)

Synapse 已有 `ProviderConfig.models`、`CustomProvider`、`_available_models()` 和首次
向导，问题是模型凭据、默认选择和其他 Agent 配置都混在 YAML 中，且 `/model`
切换只对当前进程有效。Library API 的 `Synapse()` 还会用硬编码的 `anthropic`
覆盖文件配置，破坏零参数启动。

## 2. 决策

新增用户级 `~/.synapse/models.json`，使用一个文件同时保存模型注册表和默认选择：

```json
{
  "version": 1,
  "defaultProvider": "deepseek",
  "defaultModel": "deepseek-chat",
  "providers": {
    "deepseek": {
      "apiKey": "sk-...",
      "models": [{ "id": "deepseek-chat" }]
    }
  }
}
```

- JSON 只管理 LLM provider/model；`synapse.yaml` 继续管理 tools、planning、security、
  hooks、plugins 等项目配置。
- 加载顺序为 YAML -> `models.json` -> 环境变量。显式环境变量仍可临时覆盖默认模型
  或 API key。
- `models.json` 不存在时，无参交互启动必须进入首次向导；旧 YAML 不阻止向导。
- `/model` 切换会写回默认模型；`/model add` 可追加配置并立即切换。
- Library API 改为 `Synapse(provider=None, model=None)`，只有显式参数才覆盖默认配置。
- 不实现 Pi 的热重载、shell command 凭据解析、OAuth 和模型能力元数据。这些对当前
  项目是过度设计；需要时可在同一 JSON schema 上向后兼容扩展。

## 3. 验收标准

1. 首次运行创建合法 JSON，API key 不回显。
2. 后续零参数运行自动使用 `defaultProvider/defaultModel`。
3. 多模型 upsert 不产生重复项，切换后默认选择持久化。
4. JSON 损坏或默认模型不存在时快速失败并指出文件路径。
5. YAML 的非 LLM 配置与现有 CLI 参数兼容性不受影响。
6. 本仓库测试结束后不创建真实 `~/.synapse/models.json`，确保本机下一次启动仍进入向导。

## 4. 实施结果

- 已实现 versioned JSON 校验、atomic write、模型 upsert 和默认选择持久化。
- 已接入无参 CLI、`run`、`serve`、`eval` 与 Library API；CLI 参数只保留为临时覆盖。
- 已实现隐藏 key 的首次向导、`/model add`、custom compatible endpoint 和跨 provider 凭据切换。
- 全量回归：`404 passed, 1 skipped, 1 warning`。warning 为既有 FastAPI/Starlette
  `httpx` deprecation；Windows Qdrant 临时 `.lock` 在解释器退出时仍可能产生清理 warning。
- 本机 `C:\Users\WJR\.synapse\models.json` 保持不存在，下一次交互启动会进入首次向导。
