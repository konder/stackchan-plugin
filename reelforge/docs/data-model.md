# 数据模型

产品不是「一张 ComfyUI 图」，而是「一个项目」。一支片子横跨几十个生成任务、要管资产复用与一致性，
因此核心模型是项目结构，下面再嵌套与 ComfyUI 一一映射的 Graph IR。

## 1. 项目结构

```
Project（一支片子）
├─ meta: 标题 / 风格设定 / 宽高比 / 帧率 / 默认后端偏好 ...
├─ AssetLibrary / Character Bible        ← 一致性的锚点
│   ├─ Character[]   定稿图集 + 身份特征(PuLID/InstantID) + 可选 LoRA + 触发词
│   ├─ Wardrobe[]    服装
│   ├─ Prop[]        道具
│   ├─ Environment[] 场景背景设定图
│   └─ StyleFrame[]  美术风格 / lookdev
├─ Shots[]                                 ← 分镜
│   └─ Shot
│       ├─ script: 谁 / 干什么 / 景别 / 运镜 / 时长 / 对白
│       ├─ refs: 引用的 Character / Wardrobe / Environment / Prop ...
│       ├─ keyframes: 起始帧 (±尾帧)
│       ├─ takes[]: 多条生成结果 (本地或云) + 元数据(seed/参数/后端/耗时/花费)
│       ├─ selectedTake
│       └─ graph: 该镜头背后的 ComfyUI 工作流 (Graph IR)
└─ Timeline（后置，剪辑 Agent 阶段）
```

要点：
- **资产是可复用、可锁定的一等公民**。角色档案（Character）是一致性的核心载体，详见
  [agent-system.md](agent-system.md) 的一致性方案。
- **每个 Shot 自带一张 Graph IR**，既是它的「生成配方实例」，也是钻进工作流层画布时的编辑对象。
- **takes 保留完整元数据**，支撑选片对比、复现、回滚、「基于这次再改」。

## 2. Graph IR

与 ComfyUI 的 `prompt` / `workflow` JSON **一一映射**的中立数据结构（节点、inputs、links、widget 值）。
画布只是 IR 的可视编辑器；Agent 改的也是同一份 IR；导出时序列化成 ComfyUI API 格式。

```jsonc
// Graph IR 概要
{
  "nodes": [
    {
      "id": "n1",
      "type": "KSampler",
      "widgets": { "seed": 123, "steps": 25, "cfg": 7.0, "sampler_name": "euler" },
      "inputs":  { "model": {"node":"n0","slot":0}, "positive": {"node":"n2","slot":0} },
      "pos": [120, 80]
    }
  ]
  // links 可由 inputs 推导，或显式冗余存储以便画布渲染
}
```

设计约束：
- IR 必须能**无损往返** ComfyUI 的 API 格式（导出→执行→回读不丢信息）。
- IR 节点的合法性由 ComfyUI `/object_info` 的真实 schema 校验（见 agent-system.md）。
- 云生成任务在 IR / 项目层用**虚拟节点**表示（如 `CloudVideo(provider=jimeng, ...)`），由 Orchestrator
  的云适配器解释执行，不进 ComfyUI 图。

## 3. 版本与产物

- 每次运行的产物（图/视频/中间预览）留档并挂在对应 Shot/take 上，可对比、回滚。
- ComfyUI 的缓存机制天然支持「改了下游参数只重算受影响节点」，IR 设计需保留节点稳定 id 以命中缓存。
