# Typecho → Nekomi / Twikoo 完整离线迁移教程

本教程只在本地读备份、生成文件。真实数据库导入由你在自己的电脑上执行；不要把数据库连接串放入主题或本仓库。目标存储契约是 Twikoo **2.0.17 MongoDB**，源码依据见 [RESEARCH.md](RESEARCH.md)。

## 1. 准备与安装

准备 Python 3.11+、本工具仓库、你**实际发布使用**的 Nekomi 内容和配置，以及 Typecho SQL 备份。Git 仅用于获取源码；转换时可以完全断网。常规路径只需三个直接依赖：PyYAML（Frontmatter）、markdown-it-py（CommonMark）、Bleach（基于 HTML5 解析的允许列表清洗）。requirements 同时固定传递依赖。

Linux / macOS：

```sh
cd typecho2twikoo
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -v
```

Windows PowerShell：

```powershell
cd typecho2twikoo
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -v
```

后续 Windows 命令用虚拟环境内的 python.exe 替换 `python`，其余参数相同。包含空格的路径加引号。不要在公开网盘或 Git 中保存迁移输入/输出。

## 2. 从旧 Typecho 备份

可在旧站控制台下载完整 UTF-8 SQL 备份，或在旧站有 MySQL 客户端的机器导出。表前缀先核对 Typecho 配置，例如 `typecho_`、`blog_`。至少要有 `contents` 和 `comments`；只有评论没有文章表无法可靠映射。

Bash 示例（库名、用户名为你本地设置的非秘密环境变量，密码由交互提示输入）：

```sh
mkdir -p private
mysqldump --host="$TYPECHO_HOST" --user="$TYPECHO_USER" --password --default-character-set=utf8mb4 --single-transaction --no-tablespaces "$TYPECHO_DB" typecho_contents typecho_comments > private/typecho.sql
```

MariaDB 客户端可改用 `mariadb-dump`，按本地版本支持情况去掉 MySQL 专用 `--no-tablespaces`。如果是全站备份，另外保存其他表；本工具只读取这两张表。为一致性先暂停旧站写评论，或使用支持一致快照的存储引擎。Windows 原生命令重定向的编码行为依 PowerShell 版本而异，优先用 dump 客户端的 `--result-file=private/typecho.sql`，避免把 UTF-8 重新编码成 UTF-16。

```sh
python migrate.py inspect --sql private/typecho.sql --prefix typecho_
```

`inspect` 只打印表记录数与审核状态数量，不打印正文、邮箱或 IP。

### 直接 SQL 支持范围

支持 UTF-8 / UTF-8 BOM、常见单库 mysqldump / MariaDB dump 的 CREATE TABLE、带/不带列名的多行/扩展 INSERT、单引号字符串、成对引号、MySQL 反斜杠转义、NULL、整数、行/块注释和常见版本化 SET 指令。根据词法状态识别分号和括号，**不会用一条正则切 INSERT**。不会执行输入 SQL。

未指定列名时必须先有目标表的 CREATE TABLE，以取得真正列顺序。不假设固定的 Typecho 版本列顺序。`--prefix ""` 可表示无前缀。

遇到下列情况会明确拒绝，而不尝试“差不多”恢复：非 UTF-8 编码、NAMES latin1 等非支持字符集、NO_BACKSLASH_ESCAPES / ANSI_QUOTES、目标数据中的函数/表达式/十六进制/浮点字面量、INSERT SELECT/SET、REPLACE、UPDATE、动态 SQL、数据库限定表名、重复目标表/ID、无分号的截断文件、非常规 ALTER、存储过程/DELIMITER 等。非目标表普通 INSERT 会忽略。仅为读两张表而实现的受限解析器不是通用 MySQL 引擎。当前会把整个 SQL 和记录放内存；大型备份优先走备用 JSONL，JSONL 记录仍需放内存以核对父链。

不要编辑复杂 SQL 让它“碰巧通过”；使用下一节备用方式。

## 3. 备用：本地临时 MariaDB 还原与 JSONL 导出

仅在你自己的隔离本地数据库执行。本工具不会帮你运行任意备份里的 SQL。不要使用生产数据库或生产账号还原；先检查 SQL 中的 `USE`、`CREATE DATABASE` 等，保证不会切换到其他数据库。尤其不要把来源不可信的 SQL 导入有生产权限的实例。

可安装本地 MariaDB，或启动一次性容器。以下 Docker 命令只供本地操作；环境文件 `private/mariadb.env` 使用文本编辑器创建，内容是 `MARIADB_ROOT_PASSWORD=本地随机生成的临时密码`，不要照抄固定密码、不要提交该文件。

```sh
docker run --name typecho-migration -d --env-file private/mariadb.env -p 127.0.0.1:3307:3306 mariadb:11.4
# 等待容器日志显示 ready for connections；以下操作在交互客户端内完成。
docker exec -it typecho-migration mariadb --user=root --password
```

在客户端创建独立 `migration_sandbox` 数据库：

```sql
CREATE DATABASE migration_sandbox CHARACTER SET utf8mb4;
USE migration_sandbox;
-- 通过客户端 SOURCE 读取已复制到该临时容器的备份，或用本地客户端还原。
```

若用容器，可执行 `docker cp private/typecho.sql typecho-migration:/tmp/typecho.sql`，再在客户端 `SOURCE /tmp/typecho.sql;`。为导出创建一个只对该库拥有 SELECT 权限的本地用户；用户名和密码在本地设置，不需要发到聊天中。主机客户端应安装 MariaDB 或兼容 MySQL CLI。

创建 `private/local.cnf`，限制为只有本人可读（Linux/macOS `chmod 600 private/local.cnf`，Windows 使用文件 ACL）：

```ini
[client]
user=你创建的本地只读用户
password=本地填写的临时密码
```

导出脚本只接受 loopback 主机，覆盖配置中的 host/protocol，固定查询列、JSON_OBJECT 和排序，不读取/打印配置文件内容：

```sh
python scripts/export_typecho.py --defaults-file private/local.cnf --host 127.0.0.1 --port 3307 --database migration_sandbox --prefix typecho_ --output private/typecho-source.jsonl
python migrate.py inspect --source-jsonl private/typecho-source.jsonl
```

`--client` 可指定 mariadb.exe/mysql 等实际路径。使用已恢复数据库的 JSON_OBJECT 输出，正确保留换行、中文、Emoji、引号。脚本逐行封装为 `{"table":"comments","row":{...}}`；空表用 `{"table":"comments","schema":1}` 标记，contents 同理。可在可信本地库上自行生成这个格式。required 字段见 `scripts/export_typecho.py`；旧 schema 缺列时应先核实字段语义，不能虚构 authorId。

所有后续 `--sql private/typecho.sql` 都可替换为 `--source-jsonl private/typecho-source.jsonl`。完成并核对后自行停止、删除临时容器；保留原始备份，按自己的数据保留策略清理临时凭据。**当前任务只对导出脚本做了模拟客户端测试，没有实际启动 MariaDB。**

## 4. 扫描 Nekomi 并核对路径

```sh
python migrate.py prepare --sql private/typecho.sql --prefix typecho_ --nekomi ../Nekomi --site https://nekopara.us --output output
```

工具读取 `site.config.json` 的 `comments.keyPrefix`、`comments.pageKeyPrefix`、`publishing.base`。默认扫描 `src/content/posts/**/*.md` 和 `src/content/pages/**/*.md`。自定义部署需要把实际配置显式传入：

```sh
python migrate.py prepare --sql private/typecho.sql --nekomi ../Nekomi --site https://nekopara.us --base /Nekomi/ --nekomi-config private/nekomi.config.json --posts-dir ../my-content/posts --pages-dir ../my-content/pages
```

这些参数分别对应主题的 SITE_BASE、NEKOMI_CONFIG、NEKOMI_CONTENT_DIR、NEKOMI_PAGES_DIR；工具不会悄悄读取 shell 中的部署覆盖。`convert` 必须使用相同参数。`--site` 只填 origin，子路径单独用 base，避免重复拼接。

生成 `output/catalog.json` 和 `output/url-map.csv`。用文本编辑器编辑 CSV，保留 UTF-8、逗号和引号；不要让电子表格软件自动改 CID 类型或执行单元格公式。

| CSV 列 | 如何处理 |
| --- | --- |
| cid / source_type / source_slug | 旧文章身份，不能为绕过检查随意改动 |
| target | `post:实际AstroID` 或 `page:实际AstroID`，从 catalog 复制 |
| url | Twikoo 稳定键，必须与当前 Nekomi 算出来的值完全一致 |
| href | 实际页面 HTTP(S) 地址，可能与 url 的路径不同 |
| status | `review` 候选；确认后改 `ready`；`unmapped` 无匹配；`disabled` 关闭/草稿；`skip` 人工不迁移 |
| reason | 备注，供人工参考，不控制转换 |

自动匹配仅使用 Typecho slug 与同种内容的实际 ID/页面 slug 精确相等，并且仍要求人工确认。不会用相似标题、数字 cid 或发布日期拼 URL。无法匹配时，找到实际目标，在该行填写 catalog 的 target/url/href，核对内容后改 ready。

例如，旧 slug=hello 的文章已重命名为 new-title.md，可在 Nekomi 写：

```yaml
commentId: hello
```

新地址可能是 `/posts/new-title/`，稳定键仍是 `/posts/hello/`。如果希望维持另一条已有线程：

```yaml
commentPath: /archives/123.html
```

Twikoo 只接受不带域名的 commentPath。页面需要 `comments: true`，默认是关闭；默认页面键前缀 `/pages/`，真实地址由 slug 决定。当前 Astro 的 slug 还会影响实际 ID，详见调查文档；要稳定保留讨论，明确设置 commentId/commentPath，然后重新 prepare。

每个目标只能绑定一个 cid，重复 url（包含尾斜杠等价）、关闭/草稿/不存在目标会拒绝。工具能核对路径是否存在及一致，不能判断你是否把两篇真实文章的内容人为交换；需要本人审核 CSV。`prepare` 重跑会覆盖同目录旧映射，人工改过后请先另存私密副本，或用新输出目录。

## 5. 转换、审核与校验

```sh
python migrate.py convert --sql private/typecho.sql --prefix typecho_ --nekomi ../Nekomi --map output/url-map.csv --site https://nekopara.us --namespace nekopara-typecho --output output
python migrate.py validate --file output/twikoo-comments.jsonl
```

namespace 确定后保留，不因域名、文件名、输出目录或重跑而改变。ID 基于固定 namespace 与 coid 的 UUIDv5；不依赖本次运行时间、Python 字典顺序或目标 URL。相同输入、配置和依赖版本产生相同输出字节。改 namespace 会生成另一批新 ID，可能造成重复评论。

如果确认旧站用户 uid=某个值属于自己且 authorId 是可信的登录记录，可加 `--owner-id 该值`；多个博主可重复此参数。默认不认定任何人为博主。ownerId 是文章作者，不能据此判断每条评论都是博主发的；昵称和邮箱也不是认证证据。不要把真正的 uid 用示例值替换后直接跑生产。

正文默认当 CommonMark 渲染（保留合法原始 HTML），再由 Bleach 清洗。存储 HTML，不带可执行脚本、事件属性、style、iframe、SVG 或危险协议。代码块、图片、普通链接、中文 Emoji 保留；外部图片不会被下载，失效图链也不会自动修复。旧表情插件短码不会凭空变成图片。若原站正文全是预渲染 HTML，使用 `--body-mode html`，避免 Markdown 二次处理；自定义 Typecho 插件语法需要对照样本人工验证。不会声称不同渲染器输出与旧站完全一致。

`created` 按 UTC Unix 秒乘 1000；不按本机时区重新解释。原始修改时间不存在，所以 updated=created。终端和报告不打印正文、邮箱；导入文件包含邮箱/UA，权限应受控。默认不带 IP、匿名 cookie uid，不保留旧访客编辑权限。

### 看懂报告和退出码

- `status_counts` 单独统计 approved、waiting、spam、other。
- `type_counts` 单独统计 comment、trackback、pingback、other。
- `issues` 是实际被隔离/过滤的 coid、cid 和原因；`anomalies` 包括未审核记录在内的结构异常。
- 删除父评论、跨文章 parent、循环及受影响后代全部不导出。被过滤父评论的已审核子评论也隔离，不自动升为主楼。
- 缺文章、不发布的内容、未确认映射不公开；没有假造“已删除文章”的线程。
- `id_map` 只列真正导出评论，便于核对与恢复。

若存在任何未导出的 approved 普通评论，转换返回 `2` 且 `incomplete=true`。这包括你有意 skip 的内容。先检查报告；确实接受部分迁移时可明确 `--allow-partial`，报告仍保留缺失。不导出任何评论时始终报错。重复 coid/cid、错误 CSV 和不支持 SQL 会直接拒绝；更正输入后重跑。

`validate` 校验字段类型、字符串 _id、重复 ID、安全 HTML、url/href、时间范围、pid/rid 引用、同文章及根关系。它不会连接数据库，也不能仅凭文档判断一个毫秒整数当初是否误用了秒；转换阶段已核对源时间。它同样不能保证与数据库现有 ID 不冲突，可用本地导出的 ID 快照加 `--existing-ids` 做第二层校验。

## 6. 配置主题并在本地导入

Nekomi 的公开配置只写服务地址，不写数据库连接串：

```json
{
  "comments": {
    "provider": "twikoo",
    "keyPrefix": "/posts/",
    "pageKeyPrefix": "/pages/",
    "twikoo": { "envId": "https://你自己的评论服务地址", "lang": "zh-CN" }
  }
}
```

保持与本次映射相同的前缀。确认服务端实际版本为 2.0.17（管理端显示、部署锁文件或官方部署包），配置允许博客域名和嵌入场景的 CORS。仅换客户端不能证明旧服务端支持全部 API；若服务端不同版，先对照该版本源码/测试环境重新验证。主题 npm 构建及线上 HTTP/CORS、邮件、头像、浏览器展示不在本次离线验证范围内。

数据库备份、标准连接串、测试库、正式库、去重、恢复见 [MongoDB 独立导入指南](MONGODB-IMPORT.md)。先在本地/测试后端确认主楼、三层回复、页面、昵称、图片、日期和最新评论，再考虑正式导入。

## 7. 回滚与收尾

保留源 SQL、人工确认的 CSV、namespace、输出 JSONL 和 Mongo 备份的私密副本。正式切换前暂停评论写入并做最终备份；不要在未区分新旧数据时直接整库覆盖恢复。推荐把备份还原到新的恢复库，再让测试后端验证，最后切换配置，详见导入指南。

完成后补记实际迁移数量、服务端版本、浏览器截图和导入结果到你自己的记录。仓库的执行报告只记录合成测试，不能把测试数量当成真实评论总数。
