# Synapse IDE Adapter

这是 Python runtime 的轻量 TypeScript HTTP/SSE client，供 VS Code、JetBrains
插件或其他 IDE host 复用。`AbortSignal` 会关闭 SSE 连接，服务端会同步取消对应 run。

```bash
npm install
npm run check
```
