"""AISessionManager — 一站式 AI 会话管理。

统一管理 Claude Code / Codex / DSH / WorkBuddy / ZCode 的会话历史：
列出、搜索、删除（回收站）、恢复、批量清理、磁盘占用，以及跨工具的
Token 用量工作台（按天 / 周 / 月 / 年）。

包内模块：
    core       会话管理 + 命令行入口（Claude 详细命令、tools/usage 汇总）
    server     本地 HTTP 服务（网页界面）
    providers  各家 AI 客户端的数据适配（claude/codex/dsh/workbuddy/zcode）
    usage      Token 用量解析缓存与汇总
    zstd       DSH 会话的 zstd 解压（标准库 / pyzstd / node 三档回退）
    webui      前端页面与第三方静态资源
"""

__version__ = "2.1.3"
__all__ = ["core", "server", "providers", "usage", "zstd"]
