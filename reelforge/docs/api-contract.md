# Orchestrator API 契约（草案）

Mac 客户端 ↔ Orchestrator（5090 机器）之间的接口。REST + SSE（Agent 流式）+ WebSocket（事件）。
本文是草案，实现时细化。

## REST

| 方法 / 路径 | 作用 |
|---|---|
| `POST /projects` / `GET /projects/{id}` / `PATCH /projects/{id}` | 项目 CRUD |
| `GET /projects/{id}/assets` / `POST /projects/{id}/assets` | 资产库读写（角色/服装/道具/背景/风格） |
| `POST /assets/{id}/character-bible` | 把资产固化为角色档案（identity/LoRA/触发词） |
| `GET /projects/{id}/shots` / `POST .../shots` / `PATCH .../shots/{sid}` | 分镜 CRUD |
| `POST /graphs/{id}/validate` | 对照 `/object_info` 校验 Graph IR |
| `POST /graphs/{id}/estimate` | 预估耗时 / 显存 / 云费用 |
| `POST /graphs/{id}/run` | 提交执行（返回 job_id） |
| `POST /cloud/{provider}/submit` / `GET /cloud/tasks/{task_id}` | 云作业提交 / 轮询 |
| `GET /recipes?stage=&q=` | 检索配方库 |
| `GET /object_info` | 透传/缓存 ComfyUI 节点 schema（供客户端与校验用） |
| `GET /assets/{id}` / `GET /assets/{id}/thumbnail` | 资产 / 缩略图下载 |

## Agent（SSE 流式）

`POST /chat` — 驱动 Agent，SSE 流式返回：
```
event: thinking     // 可选，过程说明
event: tool_call    // {tool, args}
event: tool_result  // {tool, result}
event: graph_patch  // 对 Graph IR / 项目的增量改动（客户端据此更新画布）
event: message      // 给用户的自然语言（含「为什么这么搭」）
event: ask          // 需要用户澄清时的提问
event: done
```

## 事件流（WebSocket）

`WS /events?project={id}` — 进度 / 中间产物 / 产物：
```jsonc
{ "type": "progress",  "job": "...", "node": "n1", "step": 12, "total": 25 }
{ "type": "executing", "job": "...", "node": "n1" }
{ "type": "preview",   "job": "...", "node": "n1", "thumb_url": "..." }   // 中间预览
{ "type": "executed",  "job": "...", "node": "n9", "asset_id": "...", "kind": "video" }
{ "type": "error",     "job": "...", "node": "n5", "human_message": "..." }
{ "type": "cost",      "job": "...", "provider": "jimeng", "amount": 0.12 }
{ "type": "done",      "job": "..." }
```

设计约束：
- 断线重连后客户端可用 `GET /jobs/{job_id}` 拉回当前状态与已产出资产。
- 预览/产物事件只带 URL，大文件按需拉取；缩略图优先。
