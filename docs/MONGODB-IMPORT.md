# 本地 MongoDB 备份、测试导入、正式导入和恢复

这里是**你在本地执行的操作指南**，不是本任务已执行的记录。本工具不连接 MongoDB；本任务没有操作线上数据库。请先完成离线转换和 report 审核。

## 1. 首先确认真实库名与集合

源码已确认 Twikoo 2.0.17 的 Mongo 评论集合为单数 **`comment`**，不是 `comments`。Vercel / Netlify 的 MongoDatabase 由服务端 `MONGODB_URI` 初始化，取 URI pathname 作为数据库名。

在 Vercel / Netlify 私密环境变量页面查看当前后端实际使用的连接串，**只在本地识别库名，不向聊天、截图或 Git 复制连接串**。例如路径是 `/blog-comments` 时库名是 `blog-comments`，不是从博客域名猜一个名字。再用 Atlas Data Explorer 确认该库内确有当前 Twikoo 的 `comment`、`config` 等集合，并与已有评论核对。

URI 没有库名、部署代码被修改或使用其他数据适配器时，不要直接导入到名为 twikoo/test 的猜测数据库。先通过部署代码/Atlas 实际数据确定服务使用的库；本项目无法从公开仓库知道你的线上库名。

准备这些本地非秘密变量（下列 Bash 名称是示意，值由你核实后设置）：

```sh
export SOURCE_DB='实际确认的生产库名'
export TEST_DB='新建且为空的测试库名'
export RECOVERY_DB='新建且为空的恢复库名'
export TOOLS_SOURCE_CONFIG='private/source-tools.yml'
export TOOLS_TEST_CONFIG='private/test-tools.yml'
export TOOLS_RECOVERY_CONFIG='private/recovery-tools.yml'
export BACKUP_FILE='backups/before-typecho-migration.archive.gz'
```

PowerShell 对应设置为 `$env:SOURCE_DB = '实际确认的生产库名'`，命令里的 `$SOURCE_DB` 换成 `$env:SOURCE_DB`，其余变量同理；不要把密码写进 shell history。

## 2. 安装官方 Database Tools 与安全连接配置

安装 [MongoDB Database Tools](https://www.mongodb.com/docs/database-tools/installation/installation/)，建议使用当前官方发布且支持 `--config` 的版本（100.3.0+）。依平台安装适合的包，并核对：

```sh
mongoimport --version
mongodump --version
mongorestore --version
mongoexport --version
```

官方参考：[mongoimport](https://www.mongodb.com/docs/database-tools/mongoimport/)、[mongodump](https://www.mongodb.com/docs/database-tools/mongodump/)、[mongorestore](https://www.mongodb.com/docs/database-tools/mongorestore/)、[mongoexport](https://www.mongodb.com/docs/database-tools/mongoexport/)。

使用私密本地 YAML 配置文件，避免 URI/密码出现在命令参数、进程列表和终端记录中。Database Tools 的 config 支持 `uri` 与 `password` 等敏感选项。示意模板（**不是可直接使用的连接串**）：

```yaml
uri: "从 Atlas 官方复制并在本地配置好的连接串"
password: "仅在本地安全填写的数据库用户密码"
```

URI 中不要再内嵌 password，用户名和保留的 authSource 按 Atlas 给出的配置填写。数据库用户仅授予操作需要的权限，Atlas 网络访问仅允许你本地实际出口；不需要为了本工具设置全网 IP。Linux/macOS `chmod 600 private/*-tools.yml`，Windows 用 ACL 限制访问。也可按官方工具交互密码提示输入，或从本地秘密管理器注入配置；不要开启 shell tracing 或打印配置内容。

分别配置生产、测试、恢复用文件。**URI 中如有数据库路径，必须与该命令的 `--db` 一致**；否则工具可能直接拒绝冲突。测试库连接串只改目标数据库路径、保留正确认证库 authSource 与拓扑/TLS 参数；也可在私密 Tools URI 中不指定默认库，所有命令显式指定数据库，但仍要保持 authSource 明确正确。服务端应用使用的 URI 则应含真实库路径。用户权限应限制在必要库上。

## 3. SRV 解析失败时取得官方标准连接串

`mongodb+srv://` 会查询 DNS SRV/TXT；当前云环境存在此类限制，本任务没有用反复连接线上库来排查。换到你自己的电脑后，可以先确认本机网络、DNS、Atlas 允许 IP 和数据库用户权限。

如果本机也无法解析 SRV，进入 Atlas 对应 cluster 的 **Connect → Drivers / Connect your application**，查找连接串类型选项。若界面提供 SRV 开关（可能叫 “SRV Connection String”），关闭它并复制 Atlas 生成的标准 **`mongodb://` seed list**。不同 Atlas 版本界面可能不同；找不到选项时按 [Atlas 官方连接说明](https://www.mongodb.com/docs/atlas/connect-your-application/) 或 Atlas 支持取得该 cluster 的标准连接串。不要选择一个“不支持你当前驱动”的旧版本仅为了拼出地址。

标准串包含真实的节点主机名和端口、`replicaSet`、`authSource`、`tls=true` 等配置。**不能只把 `mongodb+srv://` 改成 `mongodb://`**，也不能猜节点名、拿单个 IP 替代副本集、丢掉 TXT 对应参数或关闭 TLS。保留 Atlas 提供的主机列表和连接选项，在本地工具配置中安全填写用户名/密码和目标数据库。用户名或内嵌 URI 凭据若含保留字符需要官方规定的百分号编码；优先 password 配置字段而非内嵌密码。

标准连接串只绕开 SRV 发现步骤，不会修复普通 A/AAAA DNS、TLS、网络访问规则或权限问题。若仍失败，在本地对照 Atlas 官方诊断；不需要把秘密连接串发送给 Codex。

## 4. 正式数据备份与 ID 快照

先停下正式导入，确认 SOURCE_DB 的值。尽量在正式切换窗口暂停评论写入，再做最终备份和 ID 快照；否则快照之后仍可能有并发变化。保留应用配置/版本的私密记录。

```sh
mkdir -p backups private
mongodump --config "$TOOLS_SOURCE_CONFIG" --db "$SOURCE_DB" --archive="$BACKUP_FILE" --gzip
# 上一步必须成功退出，确认备份文件非空并记录大小/时间；最好先恢复到新库验证可用。
mongoexport --config "$TOOLS_SOURCE_CONFIG" --db "$SOURCE_DB" --collection comment --fields _id --out private/existing-ids.jsonl
python migrate.py validate --file output/twikoo-comments.jsonl --existing-ids private/existing-ids.jsonl
```

备份包含整个目标数据库，便于保留 config 等状态。`mongoexport` 是重复 ID 比对输入，不是完整 BSON 备份替代品。ID 冲突检测为纯离线集合交集；即使旧快照有 ObjectId，也区分它与字符串类型。快照只代表导出时刻，不能防止之后并发冲突。

只要出现冲突，不要继续导入，也不要默认改 namespace 绕开检查。核实是否上一轮已导入、部分成功或与其他来源冲突。迁移文件本身的重复 _id 已由 validate 拒绝。

## 5. 导入到新的测试库

先确认 TEST_DB 与 SOURCE_DB 不同，并在 Atlas/Data Explorer 中确认测试库的 `comment` 集合为空。为测试后端创建与正式版相同的 Twikoo 2.0.17 部署，把其 MONGODB_URI 指向 TEST_DB；不要把正式后端临时切到测试库而影响读者。

```sh
python migrate.py validate --file output/twikoo-comments.jsonl
mongoimport --config "$TOOLS_TEST_CONFIG" --db "$TEST_DB" --collection comment --type json --mode insert --stopOnError --file output/twikoo-comments.jsonl
```

JSONL 每行一对象，**不要加 `--jsonArray`**。数字时间是毫秒，_id/pid/rid 保持 JSON 字符串，不能转成 BSON ObjectId。检查工具的退出码、导入成功/失败数量；数量应等于 report.exported。没有额外导入 counter 集合，Twikoo 当前评论数 API 是按 comment 查询统计，不需要伪造阅读量。

在本地或测试站浏览：

1. 主楼、第二层和第三层的被回复昵称与归组是否正确。
2. 不同文章/独立页面是否各自独立；commentId/commentPath 与输出一致。
3. 创建时间、Markdown 代码、图片、链接、Emoji、昵称是否合理。
4. waiting/spam 是否没有公开；有意隔离的异常评论是否不在输出。
5. 最新评论、计数、CORS、头像、博主标记与当前客户端是否正常。
6. 原始 UA 是否按服务端 SHOW_UA 正常显示；未迁移 IP 导致属地为空属于预期。

测试导入、后端 HTTP API、浏览器展示均需要你在本地验证；本项目的合成测试不能替代它们。

## 6. 正式库导入

前提：测试通过、映射经本人核对、报告接受、正式库已有可恢复备份、临时暂停写评论并重新导出 ID 快照做冲突检查。最终核对变量所指库和私密配置：

```sh
python migrate.py validate --file output/twikoo-comments.jsonl --existing-ids private/existing-ids.jsonl
mongoimport --config "$TOOLS_SOURCE_CONFIG" --db "$SOURCE_DB" --collection comment --type json --mode insert --stopOnError --file output/twikoo-comments.jsonl
```

该命令只插入，不使用 `--drop`、upsert 或 merge，不主动清空/覆盖任何已有评论。MongoDB 唯一 _id 索引是最后一道冲突保护。`--stopOnError` **不是事务**：错误前可能已经插入一部分文档。失败后立即记录成功条数与错误，不要原样盲目重试。先导出新的已有 ID 快照，核对 report.id_map / 迁移文件、实际新增记录，再决定恢复或另做经审核的补导清单。

相同 namespace 重跑转换 ID 不变，因此再次直接导入会出现 duplicate key；这不代表覆盖已完成，也不代表“重复执行没影响”。改 namespace 会绕开冲突并制造内容重复，应避免。工具不自动去重、不写补丁清单、不自动删除部分导入记录。

正式导入后核对数量、抽样页面、回复层次与服务日志，再恢复写评论。不要把最终真实条数填成测试的 5 条；记录实际结果。

## 7. 恢复方案：先恢复到新库

安全的默认恢复路径是：保留当前生产库，把备份恢复到**新的空恢复库**，核对后切换应用连接。不要直接把备份恢复到已经有数据的生产库。

例如来源库和恢复库变量已确认，URI 路径/authSource 配置正确：

```sh
mongorestore --config "$TOOLS_RECOVERY_CONFIG" --archive="$BACKUP_FILE" --gzip --nsInclude="${SOURCE_DB}.*" --nsFrom="${SOURCE_DB}.*" --nsTo="${RECOVERY_DB}.*" --stopOnError
```

这里 archive 中的源命名空间用 SOURCE_DB，目标映射到 RECOVERY_DB。**不要额外加与 nsFrom/nsTo 冲突的 --db，不加 --drop。** 在隔离的测试后端上连接恢复库，核对 comment/config 等集合及抽样评论。确认恢复有效后，由你把正式 Twikoo 的 MONGODB_URI 私密更新为恢复库并重新部署；保留原库供取证和必要的数据合并。

备份后的新评论不会出现在恢复库。如果生产仍在写入，先暂停写入、另备份当前库，再按精确 ID/时间范围人工合并新数据，不能靠整库恢复保留它们。要对原库进行精准删除或覆盖恢复属于另一次明确的维护操作，需要核对本轮实际新增 ID、并发写入与备份；本工具不会自动执行这些操作。
