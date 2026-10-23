# ComfyUI-EvoH3Node

**MiniMax H3 视频生成节点**，连任意 OpenAI 兼容的 `/v1/videos` 网关（自带 h3-gateway 即可）。

4 个节点，两种模式：

- **单条生成** — 4–10 秒，首尾帧 / 多图参考 / 参考音色
- **长视频拼接** — 一次跑完 **最长 682 秒（11 分 22 秒）**，分段渲染在插件侧自动完成，不用外部拼

---

## 快速开始：把仓库链接丢给你的 AI

**不用自己看文档。** 复制下面这一行，发给豆包 / WorkBuddy / Cursor / Claude Code 等任意能跑命令的 AI：

```
帮我安装这个 ComfyUI 节点：https://github.com/CoEvolutio/ComfyUI-EvoH3Node
安装和使用方式看仓库里内置的 skill（skill/SKILL.md）
```

AI 会自己读 `skill/SKILL.md` 和 `skill/references/install.md`，然后：

1. 把仓库 clone 到你 ComfyUI 的 `custom_nodes/` 目录
2. 检查 ComfyUI 版本和依赖（只需要 `aiohttp`，ComfyUI 自带）
3. 提醒你重启 ComfyUI

重启后，节点出现在分类 **EvoH3** 下。

再用一句话让它配好：

```
我要用这个节点生成视频，网关地址是 <运营方给你的地址>，密钥是 <运营方给你的 key>
```

> `base_url` 和 `api_key` **由网关运营方分配**，仓库里没有能直接用的默认值。
> 没填会报"请把 base_url 改成服务方分配给你的网关地址"。

想让它顺手装好官方提示词 skill（强烈建议）：

```
顺便装一下 MiniMax 官方的 h3-prompt-writing skill
```

---

## 为什么单独做一个包

多数中转站没有实现 `/v1/files`（EvoH3 文档列为 501，实测 404），所以"先上传素材再提交"
的节点包在上面必然失败。

本包两条路都走通了：

| 素材 | 上传方式 |
|---|---|
| 首帧 | multipart `input_reference`，**不依赖文件接口** |
| 参考图 / 参考音频 | `POST /v1/files` 拿 `file_…` id（自建网关已实现） |

**本地图片直接连进节点就能用，不需要图床。**

---

## 手动安装（不想用 AI 的话）

### 方式一：ComfyUI-Manager（推荐）

Manager → **Install via Git URL** → 填本仓库地址。

### 方式二：手动

```bash
cd <你的 ComfyUI>/custom_nodes
git clone <本仓库地址>
```

**依赖不用装** —— 只需要 `aiohttp`，ComfyUI 自带。

装完**重启 ComfyUI**，节点在分类 **EvoH3** 下。

### 要求

- ComfyUI ≥ 0.3.x，且支持 `comfy_api.latest` 的 IO schema（`IO.ComfyNode`）
- 太老的 ComfyUI 会报 `ImportError: comfy_api` —— 升级 ComfyUI，不是升级插件

---

## 配置

节点 **`Evo H3 API Key`** 需要两个东西，**由服务方分配**：

| 字段 | 说明 |
|---|---|
| `base_url` | 网关地址，不带结尾 `/v1`，例如 `https://your-gateway.example.com` |
| `api_key` | `sk-…` 令牌 |

**推荐用环境变量**（key 不进工作流 JSON，导出分享不会泄露）：

```bash
export EVOH3_API_KEY="sk-你的密钥"
```

⚠️ ComfyUI Desktop 从图标启动时**不继承 shell 环境**，要从终端启动才生效。
没生效时节点会报 "EvoH3 API key is required"。

**兜底**：直接填进节点的 `api_key` 字段——但那意味着**明文存进工作流 JSON**。

> 分享工作流前务必扫一遍：`grep -rE "sk-[A-Za-z0-9_-]{10,}" *.json`

---

## 必须接输出节点

4 个节点都输出 `VIDEO`。**必须接一个核心的 `Save Video` 节点**，否则 ComfyUI
直接拒跑：

> 工作流未包含任何输出节点（如保存图像、预览图像），无法生成结果

这是"点了没反应"最常见的原因。

---

## 四个节点

| 节点 | 用途 | 图上限 |
|---|---|---|
| `Evo H3 API Key` | 连接配置 | — |
| `Evo H3 First/Last Frame to Video` | 首帧（±尾帧）驱动 | 1 张首帧 |
| `Evo H3 Reference Images (max 9)` | 参考图锁人物/场景 | 9 |
| `Evo H3 Reference + Voice (3 img + audio)` | 参考图 **+ 参考音色** | 3 + 音频/视频 |

详细参数见 [`skill/references/nodes.md`](skill/references/nodes.md)。

---

## 长视频（重点）

单次生成超过约 10 秒会**静默**发散成彩色雪花——不报错、进度 100%、下载完才发现是废片。
所以长片必须分段，插件内置了分段拼接：

| 目标 | `long_mode` | `duration` | `chunk_seconds` |
|---|---|---|---|
| ≤ 10 秒 | `off` | **秒**（4–10） | 忽略 |
| > 10 秒 | `autocontext · 分段拼接长视频` | **段数** | 每段秒数（4–10，8 最优） |

**`duration` 的单位随模式变**：短模式是秒，长模式是段数，总时长 = `duration × chunk_seconds`。

```
75 段 × 8 秒 = 600 秒 = 10 分钟
85 段 × 8 秒 = 680 秒 = 上限
```

滑块范围和标签会跟着 `long_mode` 自动切换（`duration（秒）` ↔ `duration（段数）`），
上限 682 秒来自插件帧网格（`total_frames` 16376 ÷ 24fps）。

**显存只跟 `chunk_seconds` 有关**——10 分钟的片子和 8 秒的片子占用一样。

### 锁人物 / 锁音色

| 开关 | 取值 | 作用 |
|---|---|---|
| `ref_image_0..2` | 每段重复喂同一组 | 身份每段重新锚定 |
| `ref_sync_mode` | **`global`** | `segmented` 会把音色参考按段切片，7 秒样本切成零点几秒 → 第二段就换人 |
| `clip_mode` | `global` / `Clip_Tag` | `global` 省事但**段边界镜头会跳**；要一镜到底用 `Clip_Tag` 逐段写运镜 |
| `accel` | `auto` | 长视频自动用 sparse 0.3（实测 2.16× 加速） |

完整说明见 [`skill/references/long-video.md`](skill/references/long-video.md)。

---

## 最小可用工作流

仓库不附带示例工作流——H3 的节点参数变化快，示例很容易变成误导。自己串一条最快：

```
Evo H3 API Key ──key──▶ Evo H3 Reference Images (max 9)
加载图像 ──图像──▶ ref_image_0
Evo H3 Reference Images ──VIDEO──▶ Save Video
```

| 参数 | 第一次跑就填这个 |
|---|---|
| `base_url` / `api_key` | 运营方给你的 |
| `size` | `9:16` 或 `16:9` |
| `duration` | `4`（先跑短的验证链路） |
| `long_mode` | `off` |

**出片了再往上加**：换 `Reference + Voice` 节点加音色，或把 `long_mode` 切到
`autocontext`、`duration` 填段数（如 75）、`chunk_seconds` 填 8 做长视频。

> 三个最容易踩的点：忘记接 `Save Video`（直接报错）；`base_url` 留着占位符（报错提示）；
> 参考图是全身棚拍、提示词又写了和参考图不一致的服装（人物会变形）。

---

## 内置 Skill

仓库里的 [`skill/`](skill/) 是给 AI 助手用的说明（Agent Skills 格式），
**上面的"快速开始"就是靠它工作的**。

```
skill/
├── SKILL.md              入口：装什么、怎么配、长视频怎么填
├── official/README.md    ★ MiniMax 官方 9 个 skill 的安装指令与索引
└── references/
    ├── install.md        安装 / 升级 / 卸载 / 故障表
    ├── nodes.md          每个节点的每个控件
    ├── long-video.md     长视频参数、Clip_Tag、帧数账目、成本
    └── multi-segment.md  需要多文件时怎么拼
```

### 官方 H3 Skills

MiniMax 官方在 [MiniMax-AI/MiniMax-H3/skills](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills)
发布了 1 个提示词 skill + 8 个风格化视频 skill。一条命令装齐：

```bash
npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --skill '*'
```

其中 **`h3-prompt-writing` 是必装的**——本插件的参考图模式要用它定义的**六段式**提示词结构，
否则人物一致性会明显变差。索引见 [`skill/official/README.md`](skill/official/README.md)。

---

## 许可

见 [LICENSE](LICENSE)。
