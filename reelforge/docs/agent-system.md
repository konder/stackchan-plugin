# Agent 体系与一致性

## 1. 三个 Agent（剪辑后置）

- **导演 / 制片 Agent**：拆剧本 → 镜头清单，排镜头，管一致性，调度本地 / 云，做高层规划。
  对应「项目层」。
- **搭图 Agent**：为每个具体生成任务造 / 改 ComfyUI 工作流（Graph IR）。对应「工作流层」。
- **剪辑 Agent（后置，独立迭代）**：剪辑 / 调色 / 字幕 / 口型同步 / 和片。MVP 不做。

## 2. 可靠性策略：不让 LLM 凭空写整张图

直接让模型吐一整张 ComfyUI graph JSON 不可靠（节点多、连线易错、不同插件节点名不同）。采用：

1. **配方库优先**：维护一批验证过的模板（见 §3），Agent 主要做「选配方 → 填参 → 局部改图」，
   而非发明拓扑。可靠性高一个量级。
2. **用 `/object_info` 做事实约束**：索引当前 ComfyUI 环境的真实节点 schema，改图前后都校验
   （节点是否存在、类型是否匹配、必填项是否齐、枚举值是否合法），杜绝幻觉节点。
3. **图编辑原语当工具**（而非吐整段 JSON）。
4. **澄清式对话**：信息不足时主动问关键项（时长 / 分辨率 / 帧率、风格参考、本地还是云）。
5. **可解释**：每次改图附「为什么这么搭 / 这么调参」，便于用户学习与接管。

## 3. 配方库（Recipe Library）

每个配方是一个**参数化的工作流模板** + 元数据：

```jsonc
{
  "id": "char_turnaround_v1",
  "title": "角色多视角定稿",
  "stage": "asset",                 // asset / storyboard / generate / post
  "backends": ["local"],            // 适用后端
  "params_schema": { /* 暴露给 Agent/用户的可调参数及取值范围 */ },
  "graph_template": { /* Graph IR 模板，含占位符 */ },
  "notes": "依赖节点: ... ; 已知坑: ..."
}
```

MVP 配方清单（最少集合）：
- `char_concept`（文生图人物定稿）、`char_turnaround`（多视角/三视图）
- `keyframe_compose`（inpaint/controlnet 把角色摆进场景出关键帧）
- `i2v_local`（本地图生视频，Wan 2.x）、`i2v_cloud`（云图生视频，即梦/Qwen）
- `interp_upscale`（补帧 + 超分）

配方用「节点 schema + 配方文本」做本地向量/关键词混合检索，供 `search_recipes` 调用。

## 4. Agent 工具集

图编辑原语与执行/估算工具（搭图 Agent 主要使用）：

| 工具 | 作用 |
|---|---|
| `search_recipes(intent)` | 检索匹配的配方 |
| `instantiate_recipe(id, params)` | 用参数实例化配方为 Graph IR |
| `add_node / connect / set_param / delete_node / replace_subgraph` | 局部改图原语 |
| `validate(graph)` | 对照 `/object_info` 干跑校验 |
| `estimate(graph)` | 预估耗时 / 显存 / 云费用 |
| `run(graph)` / `run_to(node)` | 执行 / 局部执行到某节点看中间结果 |
| `submit_cloud(provider, params)` / `poll_cloud(task_id)` | 云作业 |

导演 Agent 额外有项目级工具：`create_shot`、`assign_assets(shot, [asset_ids])`、
`plan_shotlist(script)`、`route_backend(shot)` 等。

## 5. 一致性方案（核心竞争力）

角色 / 服装 / 风格要在所有镜头里保持一致——这是 AI 叙事视频成败关键，做成一等公民：

- 角色定稿后，把「定稿图集 + 身份 embedding（PuLID / InstantID）+ 可选用 5090 训的 LoRA + 触发词」
  打包成 **角色档案（Character Bible 条目）**。
- 之后所有镜头生成时，导演 Agent **自动注入**这套档案（参考图 + IPAdapter / identity 锁定 + 触发词），
  用户不必每次手动设。这正是「Agent 替你管繁琐参数」最有价值的体现。
- 服装 / 道具 / 风格同理，可复用、可锁定。
- **一致性质量是项目最大技术风险**，需在 M2 重点验证（跨镜头角色/服装/风格漂移）。
