# KARDS AI 助手

一个 Project N.E.K.O 原生插件：让智能体看着截图打《KARDS》，由悬浮球一键唤醒，使用真实的 Windows 鼠标与键盘完成操作。

> **平台**：Windows 10/11。插件依赖 Win32 窗口枚举与输入注入，其他系统会直接报错退出。
>
> **风险提示**：本插件会接管真实鼠标和键盘来操作游戏，这可能违反 KARDS 的服务条款并带来封号风险。请只在训练模式或你有权操作的环境中使用。插件不读取游戏内存、不修改网络协议、不注入进程。

## 它怎么工作

一个三步循环，直到对局结束：

1. **看** — 截取游戏窗口，把画面交给视觉模型
2. **想** — 结合规则文档与卡组手册决定这一步做什么
3. **动** — 用真实鼠标点击，动作后自动再截一张图确认生效

插件本身不含游戏 AI，所有决策由当前 N.E.K.O 会话里的模型完成。

## 安装

在市场里安装，或在 Plugin Center 导入构建好的 `.neko-plugin`。

安装后在 Plugin Center 启动插件，桌面上会出现一颗悬浮球。

## 使用

| 悬浮球 | 含义 |
|---|---|
| 蓝色 | 空闲，点击开始 |
| 黄色 | 正在唤醒智能体 |
| 绿色 | 对局中，鼠标已被接管 |
| 红色 | 出错，3 秒后自动恢复 |

- **单击**：开始 / 停止这一局
- **拖动**：换位置（位置会记住）
- **右键**：切换状态、切到 N.E.K.O 窗口、退出悬浮球

操作期间请不要同时移动鼠标。

## 工具

| 工具 | 作用 |
|---|---|
| `kards_windows` | 列出窗口、虚拟屏幕与光标位置，用于定位 KARDS。只读。 |
| `kards_screen` | 截取整个桌面或指定窗口，返回图片与「图像像素 ↔ 屏幕像素」换算。 |
| `kards_act` | 移动 / 点击 / 双击 / 右键 / 拖拽 / 滚轮 / 按键 / 切前台 / 等待。动作后默认附带新截图。 |
| `kards_knowledge` | 读取并刷新知识库文件。 |

`kards_act` 的坐标默认使用最近一张截图的图像坐标；只有显式传入 `space="screen"` 才按桌面绝对坐标处理。尚未截图时传图像坐标会直接报错，不会盲目点击。

## 知识库

插件按以下顺序扫描目录：

- `%USERPROFILE%\Desktop\kards`
- `%LOCALAPPDATA%\N.E.K.O\kards-ai-assistant\knowledge`
- 环境变量 `KARDS_KNOWLEDGE_DIRS` 指定的目录（Windows 用分号分隔）

匹配文件名：

- `kards_rules_zh.md` — 规则速查（优先加载，避免被截断）
- `kards_dahuayu.md` — 卡组与打法手册
- `kards_db_*.md` — 卡牌数据

合计上限 60000 字符。悬浮球唤醒时会把这部分内容并入会话上下文。

## 开发

插件目录本身是一个可发布的插件仓库，GitHub Actions 会调用官方的复用工作流做校验与发布：

```bash
uv run neko-plugin check kards_ai_assistant
uv run neko-plugin publish kards_ai_assistant
```

入口：

```toml
entry = "plugin.plugins.kards_ai_assistant:KardsAiAssistantPlugin"
```
