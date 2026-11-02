# 实际执行报告

日期：2026-11-02。项目：`kylin930/typecho2twikoo`。本次是工具开发任务，所有评论样本由测试代码合成；未读取真实评论备份，未连接任何 MongoDB。

## 方案与原因

采用 Python 离线流程：受限 SQL 词法解析 → Typecho 行索引 → 扫描 Nekomi 实际身份 → 人工 CSV 确认 → 审核/父链隔离 → Markdown/HTML 安全转换 → Mongo JSONL → 离线校验 → 用户本地备份和导入。

选择这个方案是因为运行环境不能可靠解析 Atlas SRV，且迁移不需要让开发环境持有线上数据库权限。路径必须以当前主题源码为准；MongoDB 集合与回复关系必须以目标 Twikoo 源码为准，不能套用旧工具里的旧 URL。

## 调查实际完成

只读获取了 Nekomi、Twikoo、旧工具及 Typecho 源码，以及 Nekomi 锁定 Astro 7.4.2、github-slugger 2.0.0 的 npm 包。完整提交、路径与结论见 [RESEARCH.md](RESEARCH.md)。

确认 Twikoo 标签名是 `2.0.17`，不是 `v2.0.17`：第一次取带 v 的标签失败，随后用只读 tags 查询确认标签与 main 当时都指向 `b13ef7c...`。Nekomi 固定使用该客户端版本；线上实际服务端版本未核实。

发现页面文档与 Astro 实际行为不同：slug 会先成为内容 ID。实现按源码处理，并用真实 Nekomi commentKey 函数与 Astro 默认表达式、官方 github-slugger 对照 8 组样本，全部相同。样本包括中文、Emoji、空格、嵌套目录、index、slug、commentId、commentPath；结果保存在 `tests/upstream-identity.json`，没有真实内容。

## 代码交付

| 文件/模块 | 完成内容 |
| --- | --- |
| `migrate.py`、`migrator/cli.py` | inspect / prepare / convert / validate；清晰退出码；无数据库客户端 |
| `migrator/source.py` | 注释/引号/转义词法状态，CREATE 列序、INSERT 元组、严格失败、前缀、JSONL 备用输入 |
| `migrator/nekomi.py` | YAML 解析、重复键拒绝、实际 Astro ID、稳定键/href、开关/草稿、候选 CSV、目标重验证 |
| `migrator/convert.py` | 稳定 UUIDv5、审核过滤、非递归父链、异常后代隔离、显式 authorId 博主、统计/ID 映射 |
| `migrator/render.py` | CommonMark/HTML、Bleach 允许列表、网站协议约束 |
| `migrator/validate.py` | ID/类型/HTML/时间/引用/同文章/根链校验；现有 ID 快照冲突检测 |
| `scripts/export_typecho.py` | 只允许本地 MariaDB 客户端的备用 JSONL 导出，私密 defaults 文件，失败保留原文件 |
| `scripts/check_upstream.mjs` | 对固定官方源码做有限导入/回复 DTO 契约验证，非服务端测试 |
| `scripts/check_identity.mjs` | 从可信 Nekomi 源码生成 8 组合成身份对照 |
| `tests/` | 合成 SQL、主题内容、CLI 流程、异常与导出器模拟测试 |
| `.gitignore` | 排除真实 SQL/JSONL、输出、备份、环境文件和本地凭据 |

未额外生成 Valine、Artalk、Disqus 或管理端 Twikoo 文件，因为额外格式会丢字段或增加未经本次完整验证的承诺。主文件是供 mongoimport 的 JSONL。

## 实际运行的命令与结果

开发环境为 Linux、Python **3.12.14**、Node **24.19.0**。安装与开发在 `/workspace/typecho2twikoo`；上游调查目录在 `/tmp/typecho-research`。下面是实际运行命令的整理，Python 使用 `.venv/bin/python`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q migrate.py migrator scripts tests
```

实际结果：最终 **49 项 unittest 全部通过，0 失败、0 跳过**；依赖完整性通过；源码编译检查通过。测试在当前 Linux / Python 3.12 下实际执行；Python 3.11、Windows、macOS 没有独立运行，代码按跨平台标准库接口实现，不把设计兼容写成已测通过。

合成 CLI 演示实际执行：

```sh
.venv/bin/python -m tests.make_demo /tmp/typecho-demo
.venv/bin/python migrate.py inspect --sql /tmp/typecho-demo/synthetic.sql
.venv/bin/python migrate.py prepare --sql /tmp/typecho-demo/synthetic.sql --nekomi /tmp/typecho-demo/Nekomi --site https://example.invalid --output /tmp/typecho-demo/output
# 对这份合成数据，按生成的目标清单核对后将 CSV 三行改为 ready。
.venv/bin/python migrate.py convert --sql /tmp/typecho-demo/synthetic.sql --nekomi /tmp/typecho-demo/Nekomi --map /tmp/typecho-demo/output/url-map.csv --site https://example.invalid --namespace synthetic-demo --output /tmp/typecho-demo/output
.venv/bin/python migrate.py validate --file /tmp/typecho-demo/output/twikoo-comments.jsonl
node scripts/check_upstream.mjs /tmp/typecho-research/twikoo /tmp/typecho-demo/output/twikoo-comments.jsonl
```

实际结果：源文章/页面 **3**，源评论 **7**（approved 5、waiting 1、spam 1），导出 **5**，跳过 **2**；离线验证得到 documents=5、roots=3、replies=2、threads=3。所有这些数量仅属于合成示例。

官方源码契约核对实际结果：`importPreserved=true`，5 个文档字段保持不变，官方 parseComment 组装出 3 个主楼、2 条回复；第三层 pid 指向第二层、rid 指向主楼。此检查擦除 TS 类型并移除外部 import，哈希用 Node crypto，关闭 UA/IP 分支；没有启动 HTTP 服务或 MongoDB。Node 会给出 stripTypeScriptTypes 实验 API 警告。

重复转换由 CLI 测试两次执行，逐字节比较 `twikoo-comments.jsonl`、`url-map.csv`、`report.json`、`report.md`；一致。

## 测试覆盖与验收边界

| 验收点 | 实际结果 |
| --- | --- |
| 普通、两层/三层回复、不同文章 | 通过；pid/rid 断言及官方 DTO 核对 |
| 独立页、commentId、commentPath、slug/base | 通过；8 组真实主题身份对照 |
| 中文、Emoji、引号、反斜杠、控制字符、换行、多行 INSERT | 通过 |
| Unix 秒到毫秒及明显误用毫秒输入 | 通过 |
| 删除父亲、循环、跨文章、无文章、过滤祖先 | 通过；隔离并统计 |
| waiting/spam、pingback | 通过；不公开 |
| 重复源 ID、重复输出 ID、已有库 ID 快照冲突 | 通过；拒绝 |
| 错误/过期 CSV、未确认候选、草稿/关闭目标 | 通过；拒绝或隔离 |
| XSS、代码块、图片、链接、HTML/Markdown | 通过允许列表与攻击样本断言 |
| 5000 层父链 | 非递归寻根测试通过 |
| MariaDB 备用导出 | 模拟客户端成功/失败与原文件保护通过；未做真实数据库还原测试 |
| MongoDB 导入/备份/恢复 | 未执行；只提供本地操作指南 |
| Nekomi 线上展示、CORS、邮件、头像 | 未执行 |

## 遇到的问题及处理

1. 云环境初始执行沙箱阻止 Git 代理连接。使用获准的只读 Git 网络操作获取指定项目，没有绕开网络策略，也未索要数据库凭据。
2. Twikoo 的 main 使用 2.0 monorepo、包内版本占位 `0.0.0`，不能只看 package.json 就认定不兼容。核对官方标签与 npm 发布版本，锁定 2.0.17 的实际源码。
3. Nekomi 的页面文档与 Astro 的 slug 优先 ID 规则不同，按实际 loader 处理并记录差异。
4. 第一轮依赖安装自动选择了较新的 webencodings；最终 requirements 固定全部依赖，重新按该文件安装并复验。
5. Node 的本地同步 Git 子进程也受沙箱约束；允许纯本地读取提交标识后，上游契约检查通过。没有把环境阻止误记成产品代码缺陷。
6. 补充拒绝带数据库限定名、数据后 DROP/重建等 SQL 情况，避免把不支持的复杂备份静默合并。

## 尚未验证与下一步

- 不支持的 SQL 需要你在本地隔离 MariaDB 恢复，运行备用导出；真实 MariaDB 还原/导出还需验证。
- 真实库名、服务端版本、Atlas 标准连接串和访问权限由你在本地确认；本工具未接触这些私密配置。
- 按实际内容核对 CSV，解决异常；合成自动测试不能证明你手工指定的文章内容语义一定正确。
- 本地先备份并恢复到新库验证，再测试导入和浏览器显示，最后在维护窗口正式导入。
- 实际迁移数量、线上截图、邮件与回滚演练结果均 **待补充**。

整个开发过程中没有线上数据库写入，也没有声称完成数据库端到端测试。
