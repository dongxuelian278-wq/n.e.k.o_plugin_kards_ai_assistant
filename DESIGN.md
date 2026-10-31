# KARDS AI 助手设计简报

## Identity Lock
- plugin_id: `kards_ai_assistant`
- folder: `plugin/plugins/kards_ai_assistant/`
- name: KARDS AI 助手
- entry: `plugin.plugins.kards_ai_assistant:KardsAiAssistantPlugin`
- main class: `KardsAiAssistantPlugin`

## Purpose
为用户提供截图驱动的 KARDS 桌面助手：定位窗口、查看画面、在明确触发后操作真实鼠标键盘，并通过悬浮球唤醒当前 N.E.K.O 会话。

## Package Type and Capabilities
- package type: Plugin
- capabilities: three LLM tools, Windows helper process, desktop orb, static read-only guide UI
- inferred architecture: Python plugin process + long-lived PowerShell JSONL helper + WinForms orb

## First Version Scope
- `kards_windows`, `kards_screen`, `kards_act`
- image coordinate mapping and post-action screenshot
- user-started orb and KARDS rules playbook
- no memory reading or network protocol modification

## Out of Scope
- unattended farming or automatic startup into a match
- game memory/process injection
- credential handling or network interception
- arbitrary shell/script execution from UI

## Risk Follow-ups
真实桌面输入可能违反 KARDS 服务条款；默认不自动开始对局，悬浮球需用户主动点击。插件包导入应通过 Plugin Center。
