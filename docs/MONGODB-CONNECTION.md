# MongoDB Atlas 连接与 Python 自动迁移

目标是让同一份工具在本地完成诊断、只读预检、备份、明确批准后的插入和断线恢复。离线子命令保持独立；没有配置 URI 时数据库命令退出码为 2，不会尝试猜测集群或登录。

**本文的真实执行边界：当前没有 Atlas 凭据，没有连接 Atlas、执行真实备份或导入。模拟测试不代表数据库/服务端端到端验收。** 本文写入命令供你在本地逐项检查后执行，不是对本任务的生产写入授权。

## 1. 安装和安全配置

```sh
python -m pip install -r requirements-db.txt
python -m unittest discover -v
```

数据库额外依赖固定为 PyMongo 4.15.3、dnspython 2.8.0、python-dotenv 1.2.1。仅做离线转换可继续安装 requirements.txt；数据库测试在缺少可选依赖时明确跳过，数据库 CLI 提示安装，不影响离线 CLI。

用编辑器将 `.env.example` 复制为本地 `.env`。PowerShell `Copy-Item .env.example .env`，Bash `cp .env.example .env`，然后**只在本地**填写：

```ini
MONGODB_URI=从Atlas官方取得并在本地安全填写的URI
MONGODB_DATABASE=当前实际部署使用的库名
MONGODB_COLLECTION=comment
MONGODB_SELECTION_TIMEOUT_MS=5000
MONGODB_CONNECT_TIMEOUT_MS=5000
MONGODB_SOCKET_TIMEOUT_MS=10000
MONGODB_MAX_POOL_SIZE=5
MONGODB_CA_FILE=
```

示例 URI 占位文字不能运行；仓库 `.env.example` 的 URI 留空，不包含密码。使用文件权限（POSIX 600 / Windows ACL）、本地秘密管理器或安全进程环境注入凭据。进程环境覆盖 `.env`；`--env-file` 可指定其他私密路径。不执行 shell、不展开 `${变量}`，防止密码内 `$` 被改变。不要把 URI 放在 CLI 参数、主题配置、PUBLIC_ 变量、截图或聊天中。

MONGODB_CA_FILE 可指定你信任的 CA bundle；默认使用系统可信根。始终校验证书及主机名，拒绝 URI 中关闭校验或关闭 TLS 的选项，不提供不安全的“临时绕过”。原生 MongoDB TCP/TLS 连接不会自动使用 HTTPS_PROXY；HTTP 注入凭据的占位值也不等于可用的原生 MongoDB 密码。不要通过清除代理、猜节点、改路由或扩大白名单绕过环境规则。

## 2. 先确定真实目标

数据库选择优先级：

1. CLI 的 `--database`。
2. `MONGODB_DATABASE`。
3. URI 的数据库路径。
4. 缺省只读候选 `test`，标为 `driver_default_test_unconfirmed`。

MongoDB 驱动常见默认库为 test，但不能断言每一版 Twikoo 都如此。上一阶段锁定的 2.0.17 源码会显式从 URI pathname 推导库名；旧版、部署改动或 SDK 默认行为可能不同。因此请在 Vercel/Netlify 实际 MONGODB_URI 和 Atlas Data Explorer 中交叉核对。工具不会遍历多个库挑一个“像 Twikoo”的库，也不会无提示写 test；隐式 test 禁止 commit，若确实是 test，须显式设置它并精确确认。

Twikoo 2.0.17 的原生 Mongo 集合是单数 `comment`。本工具允许环境指定集合，以支持你核实过的部署，但这不代表官方后端会读取另一个名称。若改集合，先确认部署代码；CLI 不会替你改服务端。

`--database twikoo_migration_test` 仅改变目标库，不改用户认证源。PyMongo 保留原 URI 的认证配置；mongodump 的私密临时 URI 会匹配目标库并保留原 authSource（SRV 以官方 DNS TXT/驱动解析结果为准），避免 Tools 的 URI 路径和 --db 冲突。不要自行删除 authSource/replicaSet/TLS 等参数。

## 3. SRV、标准 URI 和诊断顺序

`mongodb+srv://` 先查 `_mongodb._tcp.<seed>` 的 SRV，再查 TXT 选项，随后解析节点 A/AAAA 并连接。`mongodb://` 直接使用 URI 内官方提供的节点清单和端口，不需要 SRV/TXT，但仍依赖节点 DNS、TCP 和 TLS。

SRV 不可用时，优先从 Atlas 对应 cluster 的 **Connect → Drivers / Connect your application** 获取官方标准 seed-list URI；如界面提供 SRV 开关则关闭后复制。界面版本不同找不到入口时，按 [Atlas 官方说明](https://www.mongodb.com/docs/atlas/connect-your-application/) 或官方支持取得。把这条官方 URI 安全设为 MONGODB_URI 后重新运行；工具不会从 seed 猜节点名，也不会只替换协议名、使用第三方猜出的节点或跳过 TLS。

```sh
python migrate.py db-check
python migrate.py db-info
```

`db-check` 各阶段只读：

| 阶段 | 实际检测 |
| --- | --- |
| 配置 | URI 语法、安全选项、目标库来源、超时范围 |
| SRV / TXT | 各查询一次，输出状态/记录数量，不打印 TXT 内容；失败停止后续连接 |
| 节点 A / AAAA | 对标准 URI 或权威 SRV 返回的主机查询，两种地址族分别报告 |
| TCP | 按每个 URI/权威记录给出的端口连接，Atlas 常见 27017，不猜端口 |
| TLS | 用同一 TCP 连接握手，校验可信链和 SNI/主机名 |
| Mongo | PyMongo ping，再读取 connectionStatus 当前认证状态；匿名 ping 不标为认证成功 |
| IP Access List | 只能提示核对，无法从超时断定 Access List 是根因 |

输出使用节点编号，不记录完整 URI、凭据、TXT 或评论。最多检查 12 个节点，超过数量明确标记截断。每个 DNS/TCP/TLS 阶段受配置的连接超时约束，多节点总耗时可累加；服务器选择、socket、连接池等待也有超时。连接池默认最多 5；读操作使用驱动有限重试，写操作关闭 retryWrites，不做应用层无限重连。

错误分类包含 configuration、credentials_missing、dns、network、timeout、tls_certificate、tls_handshake、authentication（服务端 code 18）、authorization（code 13）、duplicate 和 write_uncertain。原始异常消息可能带 URI 或文档，统一不输出。超时只能说明连接未完成，还需结合 DNS、网络策略、Atlas 实际出口 IP、集群健康和 TLS 排查；不能自动扩大 IP Access List，尤其不能添加 `0.0.0.0/0`。

官方参考：[PyMongo 连接](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/)、[Atlas 连接故障排查](https://www.mongodb.com/docs/atlas/troubleshoot-connection/)。

## 4. 当前 Codex 环境的实际观测

第二阶段检查日期为 2026-11-03（用户时区 Asia/Shanghai）：

- 平台未声明任何 MongoDB secret/runtime binding；进程里 URI、DATABASE、COLLECTION、CA_FILE 均未设置，本地 `.env` 不存在。只检查名称/是否存在，没有打印凭据。
- 平台环境为 running，观测当前；HTTP 策略状态为 unknown，不能宣称最新策略已生效。启动网络快照是 restricted，TCP domains/ip_ranges 均为空，VPN 未配置。
- 对允许的软件包域名 pypi.org 做系统 DNS 基线，实际返回 `gaierror`、errno `-3`；dnspython A 查询实际为 `resolver_error`、0 条结果。这是该执行上下文中的观测，不能据此单独归因整个云平台或 Atlas 配置。
- 实际运行 `python migrate.py db-check` 返回 `credentials_missing`、退出码 2。由于没有 Atlas URI，**没有实际 Atlas SRV/TXT/A/AAAA、27017、TLS 或认证错误可记录**。先前的 SRV 故障是用户报告的背景，本次没有伪造一次复现。
- `mongodump` 未安装。备份工具缺失的阻断通过模拟测试核对；没有为了缺少目标的操作去安装/连接数据库。

因此当前没有“已经解决 Atlas 连接”的结果。交付的连接方案是：本地安全配置真实 URI，先诊断；SRV 受限时换 Atlas 官方标准 URI，使用同一组命令。没有凭据及目标出站能力时继续开发和模拟测试，不反复重试、不改云环境网络规则。

## 5. db-info 与合并预检

`db-info` 只输出选定库、集合是否存在、现有数量、最多 1000 条的字段类型统计和索引规格；不输出评论原文/邮箱/IP。包括 `_id/url/href/pid/rid/created/updated/isSpam/master`，区分 string、objectId、int32、int64、double 等。样本不是全库结构证明；所有与输入 ID 相撞的记录另外完整比较。

下列情况生成阻断与人工审查计划：样本有非字符串主键、不兼容核心字段/审核/引用类型，自定义唯一索引、集合 validator 或 capped 集合。本工具不执行结构更新、索引创建/删除、权限调整或集合替换。老 ObjectId 数据不自动转字符串，也不改旧 pid/rid；先导出相关范围、对照实际服务端版本人工评估。不要把一条“结构看起来像”当成兼容保证。

```sh
python migrate.py db-import --database twikoo_migration_test --dry-run --file output/twikoo-comments.jsonl --map output/url-map.csv --nekomi ../Nekomi --site https://nekopara.us
```

输入数量、重复 ID、BSON 字段契约、安全正文、pid/rid 全链、文章路径与当前 Nekomi ready 映射重新验证；输出 `output/db-preview.json`。dry-run/default 仅查询数据库并写本地报告。

已有评论的合并策略是 **新 ID 追加**，任何 ID 冲突都阻断。只有显式恢复同一批次时，完整 BSON 内容及批次标记均一致的已写入文档才跳过。没有 upsert、replace、drop 或删除，不会默认“内容相似就算相同”。新旧评论可在同一 url 下共存；若需要把新回复挂到文件外的旧父亲、改旧线程或增量重迁旧评论，必须另做修改/增量计划，当前完整输入契约不自动处理这些情况。

## 6. 官方备份与正式写入门槛

安装 [官方 Database Tools](https://www.mongodb.com/docs/database-tools/installation/installation/)，mongodump 版本至少 100.3.0（安全 --config 支持）。

```sh
python migrate.py db-backup --database twikoo_migration_test --backup-dir backups
```

备份覆盖选定**整个数据库**，不只是 comment。文件名含 UTC 时间及随机标识。URI 只写入权限受限的临时 YAML，命令 argv 不带凭据；工具完成、失败或超时后删除临时配置。备份/临时文件 POSIX 模式为 600；Windows 使用受控目录 ACL。stdout/stderr 不转发到日志，避免工具错误包含敏感内容。进程被强制 kill/主机断电时仍需按本地安全策略检查并清理遗留临时文件。

成功后检查 exit status、文件存在/大小、gzip EOF/CRC 与 SHA256，生成 `*.backup-receipt.json`。凭据绑定目标端点指纹（不含密码）、库、集合、创建时间和文件 hash。默认 24 小时内有效，错目标、过期、损坏、缺失、工具失败全部阻止 commit。备份完整性检查不等于 mongorestore 已验证，返回 `restore_verified=false`；应先恢复到新库做一次演练。

在 Atlas 上先准备独立测试库（为空也建议先核实权限与目标），然后明确写测试库：

```sh
python migrate.py db-import --database twikoo_migration_test --commit --confirm-database twikoo_migration_test --confirm-collection comment --backup-receipt backups/上一步实际返回的.backup-receipt.json --file output/twikoo-comments.jsonl --map output/url-map.csv --nekomi ../Nekomi --site https://nekopara.us --batch-size 200
```

这里的测试库名只是显式示例，非自动保护名单。不要把它改成生产库后未经审批执行。本次任务没有生产写入授权，也没有运行此命令。正式库操作由你另行明确授权并在本地执行：先冻结写入窗口、核对真实库名、只读预检，针对正式库重新 db-backup，再给 commit 相同的精确确认。测试库的备份凭据不能用于正式库。

## 7. 批次日志与安全恢复

每个新文档附加 `_migration`（tool/version/batch_id/input_sha256）。Twikoo 2.0.17 CommentDoc 支持附加字段，公开 DTO 不转发它；已用该版本实际导入/DTO 函数核对合成数据，但没有启动服务端。

本地 `output/db-migrations/<批次ID>.migration-journal.json` 保存目标、输入哈希、planned/acknowledged/pending IDs 和状态，不保存正文/邮箱/URI。网络发送前先原子写日志并 fsync 文件；只进行 insert_many，majority 写确认，随后从 primary/majority 读取并按完整 BSON 类型和值核对。完成后再核对整个输入。

断线后的返回可能是“部分/未知写入”，这是有意保留的不确定性。用**相同文件、相同映射、相同目标、原日志**恢复：

```sh
# 保留上一节完整命令与确认/备份参数，追加：
# --resume 实际批次ID
python migrate.py db-import --database twikoo_migration_test --commit --confirm-database twikoo_migration_test --confirm-collection comment --backup-receipt backups/有效的.backup-receipt.json --nekomi ../Nekomi --site https://nekopara.us --resume 实际批次ID
```

恢复先查询实际存在的 ID：只有批次/输入 hash 和完整文档一致才计为已写入；未知结果中尚未存在的记录继续插入。已确认成功但后来缺失的记录不会悄悄重建；被编辑、异批次、外来相同 ID 立即阻断。相同批次重复恢复不会增加评论。改输入后不能恢复，换 namespace 绕过会制造重复。

日志目录使用本机目标锁，异常中断可能遗留锁；先确认没有仍在运行的进程，再人工移除遗留 `.lock`。锁不是跨机器分布式锁；不同机器并发仍靠唯一 _id 和冲突阻断，不能并发操作同一批次。不要丢失日志；丢失后不提供凭空恢复向导，需对数据库标签、备份和原输入人工重建计划。

批次不是全输入事务：中断期间可能短暂出现缺父回复，应在维护窗口执行并尽快恢复/审查。自定义非 _id 唯一索引预先阻断，运行中新增索引、并发写入、权限变化仍可能导致部分插入。工具从不盲目重试或更新原数据。

## 8. 写后验证与真实 Twikoo 验收

```sh
python migrate.py db-verify --database twikoo_migration_test --batch-id 实际批次ID --nekomi ../Nekomi --site https://nekopara.us
```

逐文档比对包括 url/href、pid/rid、创建时间和批次标记；成功只返回 `storage_verified=true`、`twikoo_e2e_verified=false`。数据库数量可随并发写入改变，因此核对的是本输入精确 ID/内容，不用“总数刚好增加”代替验证。

本地下一步必须是独立 **Twikoo 2.0.17** 测试后端指向测试库，并用相同版本 Nekomi 客户端：

1. 安全设置该测试部署的 MONGODB_URI（指向测试库，保留正确 authSource），按官方流程启动/部署。不要改正式后端连接串来充当测试。
2. 在独立测试站设置 comments.twikoo.envId 为测试服务地址，保持 keyPrefix/pageKeyPrefix 与映射一致，运行该版本主题的安装/构建流程。
3. 打开映射中的文章和页面，观察真实网络 COMMENT_GET、计数与最新评论请求；检查服务没有错误对象、CORS/TLS/认证阻碍。
4. 对照 report.id_map 核对主楼、二/三层被回复人、不同文章、页面、时间、HTML、图片、代码和表情；检查 waiting/spam 缺席、没有串帖。若测试评论提交，仅在隔离测试库进行。
5. 保存去除私密信息的版本记录、测试条目、实际结果，再决定是否批准生产导入。

本工具不自动托管 Twikoo，不自动做上述 HTTP/浏览器操作，也没有输入一个手工“已测试”参数就把结果改成 true。你验证通过前不能称线上迁移成功。

## 9. 回滚与恢复

```sh
python migrate.py db-rollback-plan --database twikoo_migration_test --batch-id 实际批次ID
```

只生成 `output/db-rollback-plan.json`：分别列出本日志计划中仍带匹配标签的 ID、缺失 ID、外来 ID。即使带标签也可能已被管理员编辑或有后续新回复，必须人工检查；**不提供自动删除 commit**。不能单纯按时间范围或昵称删除，更不能删除 foreign IDs。

推荐恢复路径继续使用 [官方 Tools 指南](MONGODB-IMPORT.md#7-恢复方案先恢复到新库)：把迁移前备份还原到新的恢复库，用隔离后端验证后，由你切换正式配置。恢复前另存当前库、考虑备份后新评论与新回复；不要用 --drop 直接覆盖生产库。若确需按批次精确撤销，先冻结写入、另备份、审查标签与 planned IDs 交集以及外部 pid/rid 引用，再制定独立维护计划。本工具从不执行删除、权限修改或集合替换。
