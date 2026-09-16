# rednote.com DOM 提取技术

## 问题

rednote.com 不使用 SSR `window.__INITIAL_STATE__`，所有数据通过客户端 API 异步加载后渲染到 DOM。原 `search.py` 依赖 `__INITIAL_STATE__` 提取数据，导致 rednote.com 搜索始终失败。

## ⚠️ DOM 会变：不要锚定 Vue scoped 属性

rednote.com 的 Vue scoped 属性哈希会随发版变化（已见 `data-v-4832212a` → `data-v-f1b86190`），
搜索结果链接也从 `/explore/{id}?xsec_token=` 变成了 `/search_result/{id}`（**不再带 xsec_token**）。
因此**不要**用 `div[data-v-xxxxxxxx]` 选卡片。稳定的锚点是**语义类名**：

- 每张搜索结果卡片是 `section.note-item`（若该类名再变，回退到"只含一个 `a.cover` 的最小祖先"）。
- 卡片内：`a.cover`（封面链接）、`a.title`（标题，文本在 `a.title span` 或直接在 `a.title`）、
  `a.author` + `.name`（作者）、`.like-wrapper .count`（点赞，可能再次改名）。
- feed_id 从 `a.cover` 的 href 里取：`/(?:explore|search_result)/([a-zA-Z0-9]+)/`。

## 异步渲染：等标题出现，别只等卡片数

卡片先以骨架出现、标题稍后填充。只等 `section.note-item` 数量稳定会抓到空标题；应等到
**标题非空的卡片数**不再增长（见 `search.py` 的 `_wait_for_search_cards`）。

## __INITIAL_STATE__ 现在"有壳无肉"

rednote.com 的 `window.__INITIAL_STATE__.search.feeds` 现在**可能存在但只有 id、没有标题/作者**。
所以在 rednote 上应**优先 DOM 提取**（`search.py` 用 `BASE_DOMAIN` 判断），只有 DOM 提取不出
标题时才退回 `__INITIAL_STATE__`。

## 笔记详情（get_feed_detail）仍未适配

详情页需要 `xsec_token`（直接 `navigate('/explore/{id}')` 或 `/search_result/{id}` 会 404 到
`/404?source=/404/sec_...`）。而新版搜索结果的 href 已不含 token。后续方案：**点击卡片**（SPA 内跳转，
由前端带上 token）而非按 URL 跳转，然后从详情页 DOM 提取标题/正文/评论。

## 历史：旧搜索页 DOM 结构（data-v-4832212a 时代，供参考）

搜索结果的笔记卡片统一使用 `div[data-v-4832212a]`（Vue scoped 属性）。每个卡片内部结构：

```
div[data-v-4832212a]
├── a[style*="display: none"]              → 隐藏链接，href="/explore/{feed_id}"
├── a.cover.mask                           → 封面链接
│   ├── img                                → 封面图 src
│   ├── span.play-icon                     → 视频标记（存在=video, 不存在=normal）
│   └── href: "...?xsec_token={token}"     → xsec_token
├── div.footer
│   ├── a.title span                       → 笔记标题
│   └── div.card-bottom-wrapper
│       ├── a.author                       → 作者信息
│       │   ├── img.author-avatar          → 头像
│       │   ├── div.name                   → 昵称
│       │   └── div.time                   → 发布时间
│       └── span.like-wrapper span.count   → 点赞数
```

## 提取 JS

见 `scripts/xhs/search.py` 中的 `_EXTRACT_SEARCH_FROM_DOM_JS`。

## 已适配的函数

- `search_feeds()` — 优先尝试 `__INITIAL_STATE__`，失败后自动回退到 DOM 提取

## 待适配的函数

- `get_feed_detail()` — `feed_detail.py` 的 `_extract_feed_detail()` 同样依赖 `__INITIAL_STATE__`
- `list_feeds()` — `feeds.py` 的首页 feeds 提取
- `_get_interact_state()` — `like_favorite.py` 中验证点赞/收藏状态

## 临时验证方法

对 rednote.com 页面使用 bridge 的 `evaluate()` 直接检查 DOM：

```python
page = BridgePage()
page.navigate('https://www.rednote.com/search_result?keyword=测试&source=web_explore_feed')
page.wait_for_load(timeout=15)
# rednote 异步渲染，需要等待
time.sleep(5)
result = page.evaluate(_EXTRACT_SEARCH_FROM_DOM_JS)
```
