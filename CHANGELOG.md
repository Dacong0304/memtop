# Changelog

## v1.1.0 — 2026-11-23

首个公开版本，包含三种形态：

- 命令行 / TUI：`memtop`、`memtop -1`、`memtop pressure`、`memtop kill`、`memtop protect`
- Web 仪表盘：`memtop web`，环形占比、内存构成、应用分组、搜索排序、结束确认、保护名单、操作记录
- 菜单栏 App：常驻占比、应用列表、逐进程勾选、全选、批量「结束选中」、一键「清除」
- 统一的安全模型：硬保护、自定义保护名单、SIGTERM → SIGKILL、JSONL 审计日志
- 菜单栏 App 使用 Big Sur 规格图标（容量条），支持 16–1024 全尺寸

## v1.0.0 — 2026-11-21

- 内部版本：命令行与 TUI 原型，完成 Activity Monitor 口径对齐
