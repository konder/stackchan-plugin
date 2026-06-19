# 流程图

GitHub 原生渲染下方 Mermaid 图。涵盖架构、制作管线、Agent 循环、路由、数据模型、反馈与云作业时序。

## 1. 系统架构（client / server）

```mermaid
flowchart TB
  subgraph Mac["Mac 客户端 (SwiftUI)"]
    chat["Chat / Agent 侧栏"]
    canvas["无限画布<br/>项目层 ⇄ 工作流层"]
    ir["Project / Graph IR<br/>唯一事实来源"]
    chat --- ir
    canvas --- ir
  end
  subgraph Server["5090 机器 (Linux, headless)"]
    orch["Orchestrator (FastAPI)<br/>导演 + 搭图 Agent / 配方库 / 校验 / 路由"]
    comfy["ComfyUI (本地 5090)"]
    cloud["云适配器<br/>即梦 / Qwen"]
    orch --> comfy
    orch --> cloud
  end
  ir -- "HTTP / SSE / WS（千兆 LAN）" --> orch
```

## 2. 制作管线（蓝色=MVP，灰色虚线=后置）

```mermaid
flowchart LR
  A["0 前期<br/>剧本 → 镜头清单"] --> B["1 资产<br/>角色/服装/道具/背景/风格"]
  B --> C["2 分镜<br/>关键帧 + 动线"]
  C --> D["3 生成<br/>图生视频 · 多 take"]
  D --> E["4 选片<br/>对比 + 补帧/超分"]
  E --> F["5 导出工程"]
  F -.后置.-> G["后期：剪辑 / 音轨 / 口型 / 调色<br/>独立剪辑 Agent"]
  classDef mvp fill:#e8f0fe,stroke:#4285f4,color:#1a1a1a;
  classDef later fill:#f1f1f1,stroke:#999,stroke-dasharray:4,color:#555;
  class A,B,C,D,E,F mvp;
  class G later;
```

## 3. Agent 搭图—校验—执行循环

```mermaid
flowchart TB
  intent["用户意图（对话）"] --> clarify{信息够吗?}
  clarify -- 否 --> ask["澄清提问<br/>时长/分辨率/风格/本地或云"] --> intent
  clarify -- 是 --> search["search_recipes 选配方"]
  search --> inst["instantiate_recipe 填参 → Graph IR"]
  inst --> edit["局部改图原语<br/>add / connect / set_param"]
  edit --> val["validate 对照 /object_info"]
  val -- 失败 --> edit
  val -- 通过 --> est["estimate 耗时 / 费用"]
  est --> run["run / submit_cloud"]
  run --> fb["反馈：进度 / 中间预览 / 产物"]
```

## 4. 本地 / 云路由决策

```mermaid
flowchart TB
  task["生成任务"] --> kind{阶段?}
  kind -- "图片资产 / 关键帧 / 补帧超分" --> local["本地 5090 ComfyUI"]
  kind -- "图生视频" --> q{质量 / 运镜 / 成本?}
  q -- "草稿 · 可控" --> localv["本地 Wan 2.x"]
  q -- "定稿 · 高质量 · 特定运镜" --> cloud["云：即梦 / Qwen<br/>预估 → 确认 → 计费"]
```

## 5. 数据模型

```mermaid
classDiagram
  Project "1" --> "*" Character
  Project "1" --> "*" Asset
  Project "1" --> "*" Shot
  Project "1" --> "0..1" Timeline
  Shot "1" --> "*" Take
  Shot "1" --> "1" GraphIR
  Shot "*" --> "*" Character : refs
  Shot "*" --> "*" Asset : refs
  class Character {
    定稿图集
    identity PuLID/InstantID
    LoRA(可选)
    触发词
  }
  class Asset {
    服装/道具/背景/风格
  }
  class Take {
    seed / 参数
    后端(本地/云)
    耗时 / 花费
  }
  class GraphIR {
    与 ComfyUI JSON 一一映射
  }
```

## 6. 反馈事件时序

```mermaid
sequenceDiagram
  participant U as 用户
  participant Mac as Mac 画布
  participant O as Orchestrator
  participant C as ComfyUI
  U->>Mac: 运行镜头
  Mac->>O: POST /graphs/{id}/run
  O->>C: 提交 prompt
  C-->>O: progress / executing / preview
  O-->>Mac: WS 事件（进度 / 中间预览）
  Mac-->>U: 节点高亮 + 进度条 + 预览缩略图
  C-->>O: executed（产物）
  O-->>Mac: WS executed（asset_url）
  Mac-->>U: 产物贴到节点 + 进入时间线
```

## 7. 云作业 submit / poll

```mermaid
sequenceDiagram
  participant O as Orchestrator
  participant Cl as 云（即梦 / Qwen）
  O->>Cl: submit(params)
  Cl-->>O: task_id
  loop 轮询
    O->>Cl: poll(task_id)
    Cl-->>O: status：running / 进度
  end
  Cl-->>O: done + 视频 URL
  O->>O: 下载 + 入库 + 计费
```

## 8. MVP 垂直切片（用户旅程）

```mermaid
flowchart LR
  s1["描述角色"] --> s2["多视角定稿"] --> s3["锁成角色档案"]
  s3 --> s4["场景 + 角色<br/>2~3 个分镜关键帧"] --> s5["图生视频<br/>多 take"]
  s5 --> s6["选片"] --> s7["导出片段 / 工程"]
```
