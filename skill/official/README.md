# 官方 H3 Skills（MiniMax 提供）

> 上游仓库：<https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills>
> 这里**不复制**上游内容，只给出安装方式和索引——官方一直在更新，直接装上游才不会过期。

## 一条命令装齐（推荐）

```bash
npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --skill '*'
```

只要写提示词那一个：

```bash
npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --skill h3-prompt-writing
```

先看看有哪些：`npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --list`

用 [skills CLI](https://github.com/vercel-labs/skills) 分发，所以装完是所有 agent 通用的。

## 索引

| skill | 用途 | 什么时候用 |
|---|---|---|
| **`h3-prompt-writing`** | 把需求改写成 H3 的提示词结构：`integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music`；Ref2VA 用六段式 | **必装**。用本插件写任何提示词之前都该读 |
| `minimalist-product-ad-generator` | 极简产品广告短片 | 电商、产品发布 |
| `brand-promo-video-generator` | 品牌 / 站点 / App 宣传片 | 营销、发布会 |
| `3d-animation-short-generator` | 风格化 3D 动画短片全流程 | 叙事动画、角色一致性要求高 |
| `paper-craft-stop-motion-explainer` | 纸艺定格科普 | 科普、教育 |
| `paper-collage-explainer-generator` | 拼贴风解说 | 观点、知识类短视频 |
| `music-video-subtitle-generator` | 按节拍排版的 MV / 歌词字幕 | 音乐视频 |
| `co-op-game-intro-generator` | 双人合作游戏开场动画 | 游戏概念片 |
| `handdrawn-live-video-generator` | 手绘发光笔触 + 实拍融合 | 单场景创意短片 |

## 和本插件的关系

这几个 skill 负责**想内容、写提示词**；本插件负责**在 ComfyUI 里把片子渲出来**。

配合方式：让 agent 先用 `h3-prompt-writing` 按目标模式（Ref2VA / FL2VA / T2VA…）产出提示词，
再把提示词贴进对应节点的 `prompt` 字段，`duration` 等参数见
[`../references/long-video.md`](../references/long-video.md)。

> ⚠️ 最容易踩的一条：**参考图模式（Ref2VA）要用六段式**，不是默认的
> `integrated_multimodal_description`。六段式里的 `retention_analysis` 专门写
> "哪些必须保持不变：脸型、发型、服装"，这是人物不跑偏最便宜的一步。
