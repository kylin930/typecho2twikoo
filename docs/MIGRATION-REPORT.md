# 实际执行报告

> 下半部分记录第二阶段（2026-11-03）的连接、备份和自动导入开发结果。第一阶段的 49 项测试保留为当时快照；当前完整测试为 96 项。两阶段都没有真实 Atlas 连接/导入或 Twikoo 服务端端到端结果。

## 第一阶段快照（2026-11-02）

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

## 第二阶段：MongoDB 模块与自动迁移（2026-11-03）

用户时区 Asia/Shanghai。继续使用固定 Twikoo 2.0.17 契约，不重新猜测 schema。新增代码在 migration/，新增参数定义在 migrator/db_args.py，离线 CLI 仅在请求数据库命令时加载可选依赖。

### 实际网络与凭据检查

按 cloud-environment-runtime 运行规范读取平台状态和 `/etc/codex/network-policy.json`，只检查凭据名称和存在性。当前平台 secrets/runtime_variables 均为空；进程 MONGODB_URI、MONGODB_DATABASE、MONGODB_COLLECTION、MONGODB_CA_FILE 均不存在，仓库 `.env` 不存在。

平台 running、observations_current=true；HTTP 网络策略状态为 unknown，不能推断其已 enforce。启动快照 restricted、TCP domains/ip_ranges 均为空、vpn_configured=false。没有修改网络策略、Atlas Access List 或权限。

实际 DNS 基线命令使用 `socket.getaddrinfo('pypi.org', 443)`，返回 `gaierror`、errno=-3。随后 `dns_query('pypi.org', 'A', 2)` 返回 resolver_error、0 条。软件包安装通过现有 HTTP 代理成功，不代表原生 MongoDB TCP 或 DNS 可用。

实际执行 `.venv/bin/python migrate.py db-check` 返回：

```text
错误：credentials_missing: 未配置 MONGODB_URI；请在本地 .env 或安全环境变量中设置，勿在聊天提供密码。
```

退出码 2。未获得官方 Atlas URI，因此没有尝试虚构目标、没有实际 Atlas SRV/TXT/节点 A/AAAA、27017、TLS 或认证结果。本次没有复现出一条真实 Atlas SRV 错误；此前问题是用户提供的背景，不能写成这次的调试成果。`command -v mongodump` 没有找到工具。

### 完成的实现

| 模块 | 行为与保护 |
| --- | --- |
| migration/mongodb.py | 安全 .env/环境加载、数据库来源标记、SRV/标准 URI、池/超时/有限读重试、强制 TLS 校验、错误分类；连接仅 ping，不写 |
| migration/diagnostics.py | SRV/TXT、权威节点 A/AAAA、TCP、TLS、ping 与 connectionStatus 认证检查；不猜节点或误判 IP 白名单 |
| migration/backup.py | 官方 mongodump 检查、私密临时配置、数据库覆盖时保留 authSource、时间归档、gzip/hash/24 小时目标绑定凭据 |
| migration/validator.py | 本地输入/路径/回复校验、只读 schema/index 预览、已有 ID 冲突、完整 BSON 比对、存储核验 |
| migration/importer.py | 必须确认目标和备份；insert_many only、majority、每批日志、写前 pending、写后确认、标签恢复、只读回滚计划 |
| migration/cli.py | db-check、db-info、db-backup、db-import（默认 dry-run）、db-verify、db-rollback-plan |

数据库名顺序为 CLI → 环境 → URI → 未确认的 test；写入不接受隐式 test。已有数据只追加不同 ID，发现冲突/不兼容样本/自定义唯一索引等生成审查计划并阻断，不修改旧结构。

新文档添加 `_migration` 批次元数据，原离线 JSONL 不变。journal 记录 planned/acknowledged/pending IDs、输入 hash、目标和状态，不含正文/邮箱/URI。断线时不把不确定结果当失败重发：原批次 resume 根据数据库完整 BSON 以及标签核对，缺失的未知写入继续，已确认后消失/已修改/外来 ID 则阻断。

### 实际命令和测试结果

```sh
.venv/bin/python -m pip install -r requirements-db.txt
.venv/bin/python migrate.py --help
.venv/bin/python migrate.py db-check
.venv/bin/python migrate.py db-import --help
.venv/bin/python -m unittest discover -q
.venv/bin/python -m compileall -q migrator migration scripts tests
.venv/bin/python -m pip check
git diff --check
```

固定 PyMongo 4.15.3、dnspython 2.8.0、python-dotenv 1.2.1 安装成功；pip check、compileall、差异空白检查通过。完整测试 **96 项通过、0 失败、0 跳过**：49 项原离线测试 + 47 项数据库模拟测试。所有 Mongo 数据读写、mongodump 成功/失败/超时、DNS/TCP/TLS/认证分支都使用合成 fakes/mocks，不是在线测试。实际执行平台仍为 Linux/Python 3.12；没有新加 Windows/macOS 实机验证声明。

新增测试覆盖：缺配置/配置优先级、隐式 test、SRV seed/权威域验证、安全 TLS 参数、只读连接、认证/授权/证书/网络分类、匿名 ping 不能算认证成功、备份损坏/过期/错目标/缺工具、临时凭据清理、dry-run 零数据库写入、目标确认、批量插入、旧数据保持、重复 ID 阻断、部分写入与丢响应、resume、不一致 BSON 类型、原数据被修改/删除、索引/validator/capped 阻断、路径检查、本机锁和只读回滚计划。

实际还对 5 条带 `_migration` 的合成文档执行了：

```sh
node scripts/check_upstream.mjs /tmp/typecho-research/twikoo /tmp/typecho-tagged-contract.jsonl
```

官方 Twikoo 2.0.17 函数结果为 documents=5、roots=3、replies=2、importPreserved=true，确认附加字段不影响所检查的官方导入/DTO 路径。仍是源码函数契约核对，不是正在运行的 HTTP 后端、MongoDB 驱动或浏览器测试。

### 发现并解决的问题

- 首轮模拟集合在清空行后错误地“丢失索引”，修正测试替身以表达真实 MongoDB 空集合保留索引；未通过削弱断言来规避失败。
- 普通 MongoDB ping 可能在未认证时成功，新增 connectionStatus，认证状态为 false 时不把 db-check 判成功。
- PyMongo 会拒绝 tlsInsecure=false 与显式安全参数并存；先拒绝不安全值，再规范化无作用的旧别名，实际做了 connect=False 的构造器核对（合成域名，没有连接尝试）。
- 测试库覆盖可能让 mongodump 的 URI 路径和 --db 冲突；Tools 的临时 URI 匹配目标库，同时保留原驱动认证源和 TLS。
- 网络上下文管理器原本可能吞掉业务错误并丢失恢复提示，已保留 MigrationError 的安全、明确消息，并加入测试。

### 未完成的真实操作和本地行动

未执行：Atlas SRV/标准 URI 真实连接、真实 mongodump/恢复、测试库/生产库实际导入、Twikoo 测试服务和浏览器显示。工具没有将这些结果置为 true；storage_verified 也只代表精确存储核对。

本地需要：安全填写 `.env`，以 Vercel/Netlify 实际 URI 和 Atlas 数据确认库；必要时从 Atlas 官方取得标准 URI；安装官方 Tools；按连接指南先诊断与只读预检，再测试库备份/明确提交、存储验证、Twikoo 2.0.17 真实服务验收。正式写入必须由用户另行明确授权。

限制：schema 统计最多抽样 1000 条；批次不是全输入事务；本机锁不跨机器；原日志丢失不自动恢复；恢复超过 24 小时需新备份；外部旧父亲/增量改旧线程需另做计划；回滚仅生成审查计划，不自动删除；备份完整性通过不等于恢复演练成功。详见 [MONGODB-CONNECTION.md](MONGODB-CONNECTION.md)。
