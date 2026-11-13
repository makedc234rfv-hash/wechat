# 微信工作群每日归档 + 日报流程

在 Windows 台式机上只读读取本机已登录的微信 4.x（Weixin.exe）本地数据，按日期/会话导出成 JSONL + HTML，再由 AI 只整理工作内容写日报/周报。

本仓库只放**工具源码、说明和流程**。不含任何聊天记录、导出结果、微信数据库、密钥、账号或密码（见 `.gitignore`）。

## 组成

| 路径 | 内容 |
|---|---|
| `source/` | 工具源码：`wechat_tool.py`（命令行入口）、`archive_app.py`（图形界面/进程发现）、`probe.py`（只读取密钥材料，仅留内存）、`snapshot.py`（临时解密快照，含 WAL 回放）、`export_archive.py`（导出 JSONL/HTML/附件）、`media.py`、`period_report.py`（统计报表）、`portable_tests.py`（11 项合成数据离线自检） |
| `docs/接手说明.md` | 命令行用法、输出格式、故障处理 |
| `docs/每日导出与日报流程.md` | 每天导出 → 写工作摘要的固定流程和规则 |
| `docs/打包验证记录_20261027.md` | 便携版打包时的验证范围 |
| `packaging/` | 便携包里的启动/自检/校验脚本（需和 `app\WeChatExport\` 放在一起才能用） |

## 快速使用（源码方式）

要求：Windows x64、Python 3.14 x64（用到标准库 `compression.zstd`）、微信 4.x 已登录并在运行。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\source\requirements.txt
.\.venv\Scripts\python.exe .\source\wechat_tool.py --self-test          # 11 项通过，不读微信、不联网
.\.venv\Scripts\python.exe .\source\wechat_tool.py --list-accounts       # 多账号时用 --account 明确指定
.\.venv\Scripts\python.exe .\source\wechat_tool.py --date 2026-11-12 --account '<账号目录>' --output '<新输出目录>'
```

数据目录不在默认 Documents 下时用 `--data-root '<...\xwechat_files>'`。输出目录必须不存在（不覆盖）。最后一行 stdout 是 JSON 结果；退出码 0 成功、1 失败、2 参数错。

## 打包成便携 exe

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\source\requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onedir --console --name WeChatExport --distpath .\rebuild --workpath .\build-cache --specpath .\build-cache .\source\wechat_tool.py
```

产物 `rebuild\WeChatExport\` 整个目录一起拷，`_internal` 必须和 exe 相邻。

## 安全边界

- 只读访问运行中的微信进程取数据库密钥：不注入、不写进程、不改原数据库；密钥只在内存，不打印、不落盘。
- 临时解密快照在输出目录里，成功或可捕获失败都会清理；强制结束/断电可能残留，需手动核实后删。
- 导出结果是明文，只放本机私人目录，不放同步盘，不外发。
- 工具自带“日报/周报”只是统计导航，不是正式日报。
- 不支持语音转写；本机没有的历史（微信未登录期间的消息）导不出来，要先从手机“迁移到电脑”。

## 致谢

`probe.py` 的密钥定位算法参考 LifeArchiveProject/WeChatDataAnalysis（key_v4.py、dll_key_scan.py）。
