# 源码调查与兼容性依据

调查日期：2026-11-02。先读源码，再实现转换。所有上游检出在当前任务的 `/tmp/typecho-research/`，没有改动 Nekomi 或 Twikoo。

## 固定调查对象

| 项目 | 已读取版本 / 提交 |
| --- | --- |
| Nekomi main | `bd3c4dec8af27ace997a85b614f73e71da7a6cb4` |
| Twikoo main、官方标签 `2.0.17` | `b13ef7c018b0cb1bb4b9ac82d98728322ae5e79b`（二者当时相同） |
| 旧 Typecho 工具 | `b972d3048008c8163aeca6cfb8e7cc4cc04431cb` |
| Typecho | `1e53c04e1ef90e61150e78917314adaeb9028bf5` |
| Astro npm | Nekomi 锁定 `7.4.2`；读取实际包 `dist/content/loaders/glob.js` 与 `dist/content/utils.js` |
| github-slugger npm | `2.0.0`，Astro 使用的算法版本 |

上游 main 会变；这里的兼容结论只针对上述源码。线上后端的实际部署版本、配置和数据库名没有远程核查。

## Nekomi：稳定身份与真实地址

已阅读 [COMMENTS.md](https://github.com/kylin930/Nekomi/blob/bd3c4dec8af27ace997a85b614f73e71da7a6cb4/docs/COMMENTS.md)、[src/content/README.md](https://github.com/kylin930/Nekomi/blob/bd3c4dec8af27ace997a85b614f73e71da7a6cb4/src/content/README.md)、[PAGES.md](https://github.com/kylin930/Nekomi/blob/bd3c4dec8af27ace997a85b614f73e71da7a6cb4/docs/PAGES.md)，并核对以下实现：

- `src/integrations/comments/identity.ts`：`commentPath || prefix + encodeURIComponent(commentId || id) + '/'`。
- `src/lib/posts.ts`、`src/lib/pages.ts`：草稿过滤、评论键唯一性、页面身份、页面元数据。
- `src/lib/urls.ts`：文章地址 `base + 'posts/' + encodeURIComponent(id) + '/'`；页面 slug 分段编码。
- `src/content.config.ts`：glob loader、Frontmatter 类型、文章默认 comments=true、页面默认 false。
- `src/lib/page-rules.mjs`：slug NFC、保留路径与大小写冲突。
- `src/integrations/comments/catalog.ts`：Twikoo CDN 客户端固定 **2.0.17**，含 SRI。
- `src/integrations/comments/frame-runtime.ts`：显式传入 `path: key` 和 `href: new URL(post.url, current.base).href`。
- `src/config/schema.mjs`、`publishing.mjs`、`astro.config.mjs`：keyPrefix、pageKeyPrefix、publishing.base，以及环境覆盖。

`url` 是稳定键，不自动加域名和 SITE_BASE；`href` 是实际站点 origin + base + 路由。例如文章改名后保留 `commentId: old-id`：键仍为 `/posts/old-id/`，href 则指向新文章。

### 已确认的文档 / 实现差异

页面文档说改变 slug 不改变文件 ID。实际 Astro 7.4.2 的 `glob.generateIdDefault` **先返回 Frontmatter 的 truthy `data.slug`**，之后才根据路径生成 ID；Nekomi 没有覆写 generateId。因此 `renamed.md` 写 `slug: about` 时，当前实际 `id` 是 `about`，默认键是 `/pages/about/`。文章即使 schema 不保留 slug，loader 仍先用它生成 ID。

没有 slug 时，Astro 逐路径段调用 github-slugger（转小写、删除特定 Unicode 字符、空格改连字符），移除末尾 `/index`。本工具据此实现 `content_id`，并使用与 JavaScript `encodeURIComponent` 相同的安全字符集，**不是直接把文件名去掉扩展名**。`slugger-ranges.json` 是由 github-slugger 2.0.0 官方删除字符正则逐 Unicode 码点生成的区间；许可证保留在 [github-slugger-LICENSE](github-slugger-LICENSE)。Python Unicode 小写规则依赖其运行时 Unicode 版本，极新 Unicode 字符的大小写可能与 Node 不同；不常见文件名应核对构建后的真实键。

工具不是完整 Astro 构建器：不会执行自定义 loader、插件、MDX、静态路由冲突检查或整个主题 schema。支持当前 `.md` glob；自定义内容目录/配置需要显式 CLI 参数。升级主题/自定义 loader 后应重新调查并核对 catalog 与站点构建结果。

## Twikoo MongoDB 存储

依据 [database/mongo.ts](https://github.com/twikoojs/twikoo/blob/b13ef7c018b0cb1bb4b9ac82d98728322ae5e79b/packages/server-common/src/database/mongo.ts)、`ports/database.ts`、`handlers/comment-submit.ts`、`services/comment-dto.ts`、`handlers/comment-get.ts`：

| 字段 | 存储与本工具策略 |
| --- | --- |
| `_id` | Twikoo 原生生成去掉连字符的 UUID 字符串；本工具 UUIDv5 hex 稳定生成 32 位字符串，禁止 `$oid` |
| `pid` | 直接父评论字符串 ID；主楼省略 |
| `rid` | 最顶层评论字符串 ID；主楼省略；所有深层回复都分组到该根评论 |
| `created/updated` | JavaScript 毫秒 number；本工具 Unix 秒 × 1000，updated 等于 created，不写当前时间 |
| `url/href` | 稳定线程路径 / 实际文章地址，二者不要求 pathname 相同 |
| `comment` | 安全 HTML；Twikoo 前台用已存 HTML 展示，本工具先 Markdown 渲染再清洗 |
| `markdown` | 2.0 管理端编辑源，可选；本工具不存未经清洗的原文，以防绕过正文清洗；后台可从 HTML 回填 |
| `master` | 明确的博主身份，本工具仅使用操作者确认的用户 uid 与已登录 authorId 对应 |
| `isSpam` | 导出始终 false，因为只选 approved 普通评论，waiting/spam 根本不进入导入文件 |
| `nick/mail/link/ua` | 昵称、规范化邮箱、安全 HTTP(S) 网站、原始 UA |
| `mailMd5` | 邮箱 trim + lower 后 MD5，与官方导入器用法一致；2.0 非 cravatar 头像可由 mail 计算 SHA256 |
| `ip/uid` | 不导出 IP，不伪造登录 cookie token；迁移评论不继承原访客的编辑凭证 |

`MongoDatabase.col('comment')` 确认评论集合名是单数 `comment`。Vercel、Netlify 默认都从 `MONGODB_URI` 初始化 MongoDatabase；数据库名从 URI pathname 推导，**不能据此认定用户的库一定叫 twikoo**。配置/计数分别存在其他集合，本工具不写它们。

`getComments` 以 `rid` 为空找主楼，按根 ID 查回复；DTO 用 `pid` 查被回复者。这支持三层乃至更多逻辑父链，但前台是“主楼 + 扁平回复组”。本工具不会把所有回复的 pid 错设为主楼，也不把深层评论直接漏掉。

官方 `getUrlQuery` 同时查询带/不带尾斜杠的 url；本工具对这些等价键也拒绝碰撞，避免串帖。

## 导入机制与额外格式取舍

已读取官方 `TkAdminImport.vue`、`services/import.ts`、`handlers/comment-import-for-admin.ts` 及 `test/services/services.test.ts` 的导入测试。

| 格式 | 官方处理 | 本项目决定 |
| --- | --- | --- |
| Valine | 数组或 `{results: [...]}`，objectId → _id，日期字符串 → 毫秒，保留 pid/rid；master 强制 false，未传 href | 会丢失本工具保留的字段，没有额外生成价值 |
| Artalk v1 | JSON 数组，id 加 artalk 前缀，Markdown 渲染；pid/rid 都取单个 rid，isSpam 强制 false | 深层回复和审核语义不能完整表达，不额外生成 |
| Artalk v2 Artrans | JSON 数组，is_pending 字符串检查，rid 同上 | 仍不能完整表达本工具模型，不额外生成 |
| Disqus | XML，thread 与 post，沿 parent 寻根；邮箱/UA 缺失，updated 用当前时间 | 格式复杂且丢字段，不额外生成 |
| Twikoo 备份 | 数组或 results；规范化 `$oid`、数组字段及 null 回复，随后 bulkAddComments | 研究过，但本次主交付仍仅为 mongoimport JSONL，不增加管理端导入文件或宣称任意后端兼容 |

官方导入接口是管理员权限操作，不是离线验证器；它会批量插入，不能作为重复导入保护。不要把 Mongo JSONL 当成未经验证的 Valine/Artalk 文件上传。

### 已执行的上游函数契约核对

`scripts/check_upstream.mjs` 在锁定 SHA 的官方源码上，使用 Node 的类型擦除加载实际导入与 DTO 函数；移除外部 import，只为同步哈希提供 Node crypto，关闭 UA/IP 解析分支，未启动服务端或数据库。对 **合成示例**验证：官方 Twikoo 导入保留 5 条输出文档，官方回复组装得到 3 个主楼、2 条回复，并验证第三层 pid/rid。此检查范围有限，不能代表 HTTP 服务、MongoDB 驱动或浏览器端到端测试。

## Typecho 与旧工具

Typecho `Widget/Feedback.php` 只在已登录分支设置 `authorId = user.uid`；`ownerId` 是文章作者 uid，每条访客评论也可能带同样 ownerId。故 **不能仅用 ownerId、昵称或未认证邮箱判断博主**。用户可在本地 users 表核实自己的 uid，再传 `--owner-id`；默认全 false。旧系统若插件伪造 authorId 或历史数据被篡改，需要人工审核；工具不能重建旧认证证据。

旧工具提供了 Unix 秒转毫秒、parent 寻根、基础字段转换的参考。但它依赖在线 MySQL、按旧 permalink 拼 URL、缺文章时虚构删除路径、仅按邮箱认博主、输出日志包含正文，且没有这里的映射/安全/异常隔离约束。本项目独立实现，不复制其 URL 猜测、私密日志和输出策略。
