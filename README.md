# astrbot-vcp-agent

将 VCPToolBox 的 Agent 能力接入 AstrBot 的插件。

## 已实现

- `/vcp` 指令触发 Agent 对话。
- 规则触发（群白名单 / @机器人 / 关键词 / 概率）
- 捕获并重写 VCP 工具调用标记（`<<<[TOOL_REQUEST]>>> ... <<<[END_TOOL_REQUEST]>>>`），避免原始标记污染群聊。
- 支持图片输入（提取用户消息中的图片并传入 VCP 请求）。
- 支持图片输出（解析回复中的 Markdown 图片链接并发送图片消息）。
- 聊天记录以 Markdown 形式保存，保留完整 Agent 原始回复和工具调用数据（便于后续 RAG）。

## 配置

插件配置通过 AstrBot 原生 `_conf_schema.json` 管理，可在 WebUI 中配置：

- `vcp_base_url` / `vcp_api_key`
- `model` / `system_prompt` / `temperature`
- `stream`
- `history_window_size` / `history_storage_path`
- `rules`（template_list）

## 聊天记录

默认目录：`data/plugin_data/<plugin_name>/conversations/`

每个会话一个 `.md` 文件，按时间追加。

## 依赖

- `httpx>=0.27.0`
