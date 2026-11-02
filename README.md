# Typecho → Twikoo 离线评论迁移

Python 3.11+。从 Typecho SQL 备份生成 Twikoo **2.0.17 MongoDB** 评论 JSONL；转换、检查均不需要数据库或网络，不包含线上写库功能。

适用于 Nekomi 的文章与独立页面。先核实主题的实际评论键，再人工确认映射；默认只迁移 `approved` 普通评论。异常父链及其后代会隔离，待审核与垃圾评论不会公开。

## 安装

Linux / macOS：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -v
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -v
```

Windows 下后续命令中的 `python` 可换为 `.\.venv\Scripts\python.exe`，不必修改 PowerShell 执行策略。

## 使用

```sh
python migrate.py inspect --sql private/typecho.sql --prefix typecho_
python migrate.py prepare --sql private/typecho.sql --nekomi ../Nekomi --site https://nekopara.us
# 用文本编辑器核对 output/url-map.csv；确认的行 status 改为 ready。
python migrate.py convert --sql private/typecho.sql --nekomi ../Nekomi --map output/url-map.csv --site https://nekopara.us --namespace nekopara-typecho
python migrate.py validate --file output/twikoo-comments.jsonl
```

`convert` 要求 `--nekomi`：它会再次验证映射，拒绝不存在、关闭评论、草稿或路径不符的目标。`--namespace` 是永久来源标识，以后重跑保留原值。所有子命令支持 `--help`。

输出：

| 文件 | 用途 |
| --- | --- |
| `output/url-map.csv` | 可编辑的 cid → 目标内容 / 稳定 url / 实际 href 映射 |
| `output/catalog.json` | prepare 生成的目标清单，便于查找不同文件名的内容 |
| `output/twikoo-comments.jsonl` | mongoimport 输入，每行一个文档，字符串 `_id` |
| `output/report.json` | 统计、隔离原因、异常和已导出评论的稳定 ID 映射 |
| `output/report.md` | 人类可读报告，不含评论正文、邮箱或 IP |

退出码 `0` 表示该命令成功；`2` 表示参数/解析错误，或转换存在未处理的已审核评论、没有可导出评论。发现缺失仍会生成审查文件，请先看报告。只有明确接受部分迁移时才使用 `--allow-partial`；空导出始终返回 `2`。命令失败时不要使用输出目录中可能遗留的旧文件导入。

## 文档与验证范围

- [完整中文教程](docs/MIGRATION-GUIDE.md)：备份、SQL 备用导出、映射、转换、Nekomi 配置及回滚。
- [独立 MongoDB 导入指南](docs/MONGODB-IMPORT.md)：确认真实库名、SRV 故障处理、备份、测试库、正式库、重复 ID 和恢复。
- [源码调查与兼容性依据](docs/RESEARCH.md)：锁定的提交、字段和官方导入格式取舍。
- [实际执行报告](docs/MIGRATION-REPORT.md)：实际命令、测试结果及未验证项。
- [博客文章素材](docs/BLOG-MATERIALS.md)：第一人称技术记录与待补充内容。

只使用合成数据测试。没有连接 MongoDB Atlas，没有执行真实导入，没有验证线上评论显示。工具不承诺兼容未知服务端版本；客户端和服务端建议均固定 2.0.17 后在测试环境核对。

SQL、JSONL、`.env`、备份及输出目录默认被 Git 忽略。**JSONL 仍含评论者邮箱和 UA，必须私下保管**；`.gitignore` 不是加密或访问控制。工具默认不导出 IP，不在终端打印正文、邮箱或连接串。
