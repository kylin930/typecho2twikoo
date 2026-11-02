# 从 Typecho 到 Nekomi：把旧评论留在新的博客里

> 这是一份文章素材草稿，记录已做的源码调查和合成测试。真实数据库导入、线上显示和最终评论数量还没有完成，文中的待补充项不能在发布前改成未经验证的结论。关于选择主题的个人动机，下面仅按这次迁移目标整理；更具体的旧站使用经历由我自己补写。

## 我想换掉的是博客框架，也想保留过去的讨论

我准备把 nekopara.us 从 Typecho 迁到基于 Astro 的 Nekomi。对这次迁移来说，文章可以变成 Markdown，主题负责静态构建，评论交给独立服务。这让我可以分别维护内容、页面样式和评论后端。

但“文章搬过去”不等于“博客搬完了”。过去的讨论还关联着旧文章的数字 ID、旧路径和回复对象。如果只把评论正文导出成一份 JSON，新站不一定知道该把它们放在哪一篇文章下面。

[待补充：我使用 Typecho 的时间、选择 Nekomi 的具体个人原因、迁移前后的页面截图。不要根据这份草稿虚构性能提升或维护成本数字。]

## 评论交给 Twikoo，数据留在 MongoDB

这次评论后端沿用 Twikoo，部署选择是 Vercel 或 Netlify 加 MongoDB Atlas。Nekomi 自身还是静态网站，浏览器把评论请求交给独立的 Twikoo 服务，主题不保管数据库密码。

我选择这条路线，主要是让静态内容和有状态评论分别维护，同时能在迁移前备份数据库、核对输入文件和测试导入。这里没有做不同评论系统的性能评测，也没有测过它们的长期费用。

先核对版本很有必要。当前调查到的 Nekomi 固定加载 Twikoo **2.0.17**，而当时 Twikoo 的官方 `2.0.17` 标签和 main 恰好是同一提交。源码中某些 package.json 写着 `0.0.0`，那是构建发布时替换的占位，不能直接当成真正的部署版本。

[待补充：我最后选了 Vercel 还是 Netlify、实际服务端部署版本、服务 URL。不要在文章里贴数据库 URI 或管理密码。]

## 两套系统保存的是不同的身份

Typecho 的评论主要通过 `coid` 标识自己，通过 `cid` 找文章，通过 `parent` 找直接父评论。评论还带 `status`、`type`、Unix 秒时间，以及作者相关字段。

Twikoo 的 Mongo 文档则需要字符串 `_id`、稳定线程键 `url`、实际页面地址 `href`、毫秒时间和两种回复引用。它的 `pid` 是直接父评论，`rid` 是最顶层评论。前台读取时先找主楼，再按 rid 取整组回复。

| 意义 | Typecho | 这次生成的 Twikoo 文档 |
| --- | --- | --- |
| 评论身份 | coid 整数 | 来源 namespace + coid 的 UUIDv5 hex 字符串 |
| 所属内容 | cid | 查 contents 后经人工映射取得 url/href |
| 发布时间 | Unix 秒 | 乘 1000，保存整数毫秒 |
| 直接回复 | parent | pid |
| 回复组 | 沿 parent 查找 | rid 指向根 |
| 审核 | approved/waiting/spam | 只让 approved 普通评论进入公开导入文件 |

我没有直接把 coid 转成 BSON ObjectId。Twikoo 当前 Mongo 实现用字符串 ID 查询，回复引用也用字符串。稳定 UUID 的好处是重跑转换不会随机换一批 ID；代价是 namespace 必须保存好，不能为了绕开重复键而随手换一个。

关键代码在 `migrator/convert.py`：

```python
uuid.uuid5(
    uuid.NAMESPACE_URL,
    f'typecho2twikoo:v1:{namespace}:comment:{coid}',
).hex
```

这个 ID 不依赖当前时间或新站 URL。若文章改路由，评论本身的身份仍能保持稳定。

## 路径才是最容易静悄悄出错的地方

Nekomi 传给 Twikoo 的 path 不一定等于浏览器地址。默认线程键是：

```text
commentPath
或 keyPrefix + encodeURIComponent(commentId || 实际内容ID) + '/'
```

文章前缀默认 `/posts/`，页面默认 `/pages/`。站点域名和部署 base 不自动进入稳定键，实际 href 则需要带上它们。

比如我把 hello.md 改名为 new-title.md，仍可以写 `commentId: hello`。这样 `/posts/hello/` 是评论的身份，`https://nekopara.us/posts/new-title/` 是读者真正访问的地址。把两者混成一个字段，很可能会让评论挂到错误的地方，或者旧讨论看起来消失了。

还有一个实际调查中发现的细节：页面文档说 slug 不改内容 ID，但 Nekomi 使用的 Astro 7.4.2 glob loader 会先取 Frontmatter 的 slug 作为 ID。工具最后按源码处理，而不是只相信文档中的概念描述。这个差异也让我更愿意保留一份人工确认的映射表。

`prepare` 会生成 `url-map.csv` 和目标 catalog。它只给出精确 slug 候选，不靠相似标题猜测。即使自动找到了候选，我仍要核对内容，并把 status 改为 ready。转换时工具会再看一遍 Nekomi 文件，检查目标是否存在、评论是否开启、url/href 是否与主题一致。

[待补充：我自己的旧路径与新路径实例。不要用合成文章当成真实旧站文章。]

## 三层回复要保留两种关系

假设 A 是主楼，B 回复 A，C 回复 B：

```text
A: 没有 pid / rid
B: pid=A, rid=A
C: pid=B, rid=A
```

如果把 C 的 rid 也设为 B，Twikoo 查询 A 的回复组时就可能找不到 C。如果为了方便把所有 pid 都设为 A，C 虽然出现了，被回复的人却变了。

工具先建立 coid 索引，用非递归方式沿父链找根，缓存结果。这样很深的父链不会碰到 Python 的递归深度上限。测试中单独验证了 5000 层父链，但这只是寻根算法测试，不是浏览器展示了 5000 层楼。

父评论被删除、parent 跨文章、循环引用这些情况，我没有选择默默“修好”。工具会隔离受影响评论和后代，记录原 coid/cid 和原因。待审核父评论下面即使有 approved 子评论，也不会绕过父链直接公开。怎么整理这些历史异常，应该由我看过报告后决定。

## SQL 不能靠切字符串凑合读

旧工具的思路给了我字段转换的参考，但它依赖可连接 MySQL，而且会按旧 permalink 拼地址。这次我想先直接读备份，让最常见的一条路径不依赖运行数据库。

SQL 正文可能包含引号、逗号、分号、反斜杠、换行和 Emoji。拿正则去切 `INSERT (...)`，遇到一条带括号的评论就可能错位。因此这里用了有状态词法处理：先区分字符串、反引号标识符和注释，再分语句和元组；没有列名时从 CREATE TABLE 取列顺序。

我也没有把它包装成通用 MySQL 解析器。函数、特殊 SQL_MODE、复杂表达式或无法可靠处理的备份会直接报错。这种情况就在本地隔离 MariaDB 还原，用客户端 `JSON_OBJECT` 导出逐行数据，再交给同一套转换逻辑。这个备用导出器已经做过模拟客户端测试，真实 MariaDB 还原仍待本地验证。

## HTML 既要能看，也不能把脚本搬过去

Typecho 评论可能混有 Markdown、原始 HTML 和旧表情。工具先用 CommonMark 渲染，再用 Bleach 清洗，允许代码块、图片和普通链接，去掉事件属性、style、iframe、SVG 和危险协议。

我没有在导入后指望 Twikoo 再替我做一次提交时的清洗，因为 mongoimport 直接写入的是存储文档，不走“发表评论”的服务端处理链。导入文件里的 HTML 应该在生成时就处理好。

保留内容也不等于逐像素复刻旧站。旧插件表情短码、自定义 Markdown 扩展、已失效图片可能需要额外整理。这次没有为它们编造兼容性。评论者邮箱和 UA 仍保留在私密 JSONL 中，IP 默认不迁移，日志和报告不打印正文或邮箱。

## Atlas 的 SRV 问题让我把边界分得更明确

这次 Codex 环境对 `mongodb+srv://` 所需的 DNS SRV 解析存在限制。我没有让它不断尝试连线上 Atlas，也没有把生产连接串贴进聊天。转换器本来就不该为了生成 JSONL 而要求连上数据库。

需要导入时，我会在自己的电脑上安装 MongoDB 官方 Database Tools，从 Atlas 官方界面拿对应 cluster 的标准 seed-list 连接串。如果 SRV 开关可用，就关闭它复制 `mongodb://` 格式；找不到入口则按官方说明或支持渠道取得。不能只改协议名，因为节点列表、replicaSet、TLS、authSource 等参数不是凭空补出来的。

[待补充：我本地是否也遇到 SRV 问题，实际通过哪一种官方连接方式成功。不要把“有备用方案”写成“已解决线上连接并导入”。]

## 离线转换，手动导入

我把流程拆成了几步：

```sh
python -m pip install -r requirements.txt
python migrate.py inspect --sql private/typecho.sql
python migrate.py prepare --sql private/typecho.sql --nekomi ../Nekomi --site https://nekopara.us
# 人工核对 output/url-map.csv，把确认行改为 ready。
python migrate.py convert --sql private/typecho.sql --nekomi ../Nekomi --map output/url-map.csv --site https://nekopara.us --namespace nekopara-typecho
python migrate.py validate --file output/twikoo-comments.jsonl
```

正式导入前，还要在本地确认真实库名、做 mongodump、导出已有 ID 快照并比较冲突，在新测试库导入并检查页面。正式命令使用 insert 和 stopOnError，不使用 drop 或 upsert。

```sh
# 以下是待我本地执行的流程；这些私密配置/变量不属于仓库。
mongodump --config "$TOOLS_SOURCE_CONFIG" --db "$SOURCE_DB" --archive="$BACKUP_FILE" --gzip
mongoexport --config "$TOOLS_SOURCE_CONFIG" --db "$SOURCE_DB" --collection comment --fields _id --out private/existing-ids.jsonl
python migrate.py validate --file output/twikoo-comments.jsonl --existing-ids private/existing-ids.jsonl
mongoimport --config "$TOOLS_TEST_CONFIG" --db "$TEST_DB" --collection comment --type json --mode insert --stopOnError --file output/twikoo-comments.jsonl
```

这里的 `stopOnError` 不是事务，导入失败可能已经插入一部分。这也是我不想把导入自动藏进转换脚本的原因：出错后要先核对已经发生了什么，再决定恢复或补导。恢复方案先把备份还原到新的恢复库，避免直接覆盖现有生产数据。

## 到目前为止，真的验证了什么

第一阶段 Linux / Python 3.12 环境下，**49 项自动化测试通过**，覆盖普通评论、多层回复、不同文章、页面、路径设置、转义、审核、异常父链、XSS、重复 ID、错误映射和重复输出一致性。

另外有两类直接对照：8 组内容身份结果与实际 Nekomi commentKey / Astro 默认规则一致；合成迁移文件交给 Twikoo 2.0.17 的实际导入和回复 DTO 函数，5 个文档完整保留，组装出 3 个主楼、2 条回复。演示输入里有 7 条源评论，waiting 和 spam 各 1 条没有导出。

这些数字只是测试数据，不是我的博客评论数量。它们说明离线转换和所核对的源码契约通过了测试，不能说明 MongoDB 驱动、云函数、CORS 或线上 UI 已经正常。

[待补充：真实旧站评论总数、最终导入条数、隔离原因如何处理、测试库导入日志、正式导入结果、页面截图、回滚演练。]

这次最有用的经验是把“已经知道”和“还没发生”写清楚。路径按源码核对，异常留给报告，私密数据留在本地。这样到我真正切换站点时，每一步都有可以检查的输入和输出。

## 第二阶段补记：让文件能安全地进数据库

第一阶段把转换做成了离线工具。接下来我希望同一个入口也能做数据库诊断、备份和分批导入，但默认行为仍然是只读。这里的进展是连接/导入模块和模拟测试完成，还没有我的真实 Atlas 连接结果。

我先让 Codex 检查已有环境，而不是再贴一次密码。结果是没有 MONGODB_URI，没有本地 .env，平台也没有注入 MongoDB 凭据。启动网络快照没有数据库 TCP 授权；对允许的软件包域名做 DNS 基线时，系统解析返回 gaierror(-3)，dnspython 查询得到 resolver_error。这能说明当前执行上下文里这次解析没成功，不能直接说明 Atlas 白名单错了。没有真实 Atlas URI，也就没有一条本次实际复现的 SRV/TLS/认证错误可以贴在文章里。

因此我没有让它换 DNS、猜集群节点或加全网白名单。工具支持两种官方 URI：SRV 让驱动查询 SRV/TXT，标准 URI 直接用 Atlas 提供的节点清单；在本地遇到 SRV 受限时，我会从官方界面取得标准串再配置。不能只改协议名，因为 replicaSet、TLS 和 authSource 都有实际含义。

连接参数放在本地 .env 或安全环境变量里。PyMongo 的池、连接超时、服务器选择超时和 socket 超时各有边界，读操作有限重试，写操作不盲目重试。日志把异常变成认证、权限、DNS、TLS 或网络等类别，不把含连接串的错误原文打出来。

这次还有一个容易忽略的点：ping 成功不一定代表登录成功。所以 db-check 在 ping 后还读当前连接的认证状态。db-info 只看指定库/集合、现有数量、索引和字段类型统计，不下载评论正文到日志，更不靠“哪个库看着像”自动选择生产库。若没写库名，候选 test 会明确标成未确认，不能直接 commit。

接下来我会在自己的电脑上运行：

```sh
python -m pip install -r requirements-db.txt
# 先在本地按 .env.example 安全配置，不能把密码发到聊天。
python migrate.py db-check
python migrate.py db-info
python migrate.py db-import --database twikoo_migration_test --dry-run --nekomi ../Nekomi --site https://nekopara.us
python migrate.py db-backup --database twikoo_migration_test
```

这些数据库步骤在当前任务里还没有对真实库执行。dry-run 会重新检查文件、映射、父链和数据库 ID 冲突。已有数据走仅追加新 ID 的合并方案，撞 ID 或发现不兼容结构就给出阻断计划，不用 upsert 掩盖差异。

写入的门槛是 --commit、精确确认库/集合、同一目标的有效备份凭据。备份调用官方 mongodump，凭据只放私密临时配置，不带进命令参数；归档完整性和校验值通过后才发备份凭据，失败就不能正式导入。恢复演练还是另一步，不能把 gzip 校验当成“已经恢复成功”。

我最在意的是断线发生在“服务器已经写了，但客户端没收到回复”的时刻。模块会在发送前把 pending IDs 写入本地日志，给新文档加 _migration 批次标签。之后用原文件、原批次 --resume 时，根据数据库中实际存在的文档和完整 BSON 比对来决定哪些已成功、哪些还没写，而不是把整批再发一次。已经被管理员改过或确认后被删除的记录不会自动重建。

测试里专门模拟了只写入一条后断线、整批已写但响应丢失、后续编辑、外来重复 ID 和恢复输入变化。第二阶段完成后，完整套件是 **96 项测试通过**，其中 47 项是新增数据库模拟测试。带批次字段的 5 条合成数据也通过了官方 Twikoo 2.0.17 导入/回复函数核对。这些都没有把 Twikoo 服务真正跑起来。

本地后续命令会包含以下显式写入步骤；备份凭据和批次 ID 要用实际输出，不能照抄占位符：

```sh
python migrate.py db-import --database twikoo_migration_test --commit --confirm-database twikoo_migration_test --confirm-collection comment --backup-receipt backups/实际备份凭据.backup-receipt.json --nekomi ../Nekomi --site https://nekopara.us
python migrate.py db-verify --database twikoo_migration_test --batch-id 实际批次ID --nekomi ../Nekomi --site https://nekopara.us
python migrate.py db-rollback-plan --database twikoo_migration_test --batch-id 实际批次ID
```

这里没有自动删除功能。回滚计划只标出日志与数据库标签的对应关系，仍要看后续新评论和外部回复；必要时把备份先恢复到新库。分批写入不是整个文件的事务，维护窗口和恢复步骤仍然有用。

[待补充：真实本地 db-check 输出的脱敏摘要、最终使用 SRV 还是标准 URI、备份恢复演练、测试库写入结果、Twikoo 2.0.17 测试服务端与浏览器逐项验收、正式写入授权与最终数量。当前没有这些结果，不能写成已经迁移成功。]
