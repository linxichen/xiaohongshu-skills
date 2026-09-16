"""搜索 Feeds，对应 Go xiaohongshu/search.go。"""

from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .errors import NoFeedsError
from .human import sleep_random
from .selectors import FILTER_BUTTON, FILTER_PANEL
from .types import Feed, FilterOption
from .urls import BASE_DOMAIN, make_search_url

logger = logging.getLogger(__name__)


def _titled_feed_count(result_json: str) -> int:
    """How many extracted feeds carry a non-empty title — used to tell a usable
    __INITIAL_STATE__ payload from rednote's title-less one."""
    try:
        data = json.loads(result_json)
    except (TypeError, ValueError):
        return 0
    count = 0
    for feed in data if isinstance(data, list) else []:
        note = feed.get("noteCard") or feed.get("note_card") or {}
        if (note.get("displayTitle") or note.get("display_title") or "").strip():
            count += 1
    return count

# 筛选选项映射表：{筛选组索引: [(标签索引, 文本), ...]}
_FILTER_OPTIONS: dict[int, list[tuple[int, str]]] = {
    1: [(1, "综合"), (2, "最新"), (3, "最多点赞"), (4, "最多评论"), (5, "最多收藏")],
    2: [(1, "不限"), (2, "视频"), (3, "图文")],
    3: [(1, "不限"), (2, "一天内"), (3, "一周内"), (4, "半年内")],
    4: [(1, "不限"), (2, "已看过"), (3, "未看过"), (4, "已关注")],
    5: [(1, "不限"), (2, "同城"), (3, "附近")],
}

# 从 __INITIAL_STATE__ 提取搜索结果的 JS
_EXTRACT_SEARCH_JS = """
(() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.search &&
        window.__INITIAL_STATE__.search.feeds) {
        const feeds = window.__INITIAL_STATE__.search.feeds;
        const feedsData = feeds.value !== undefined ? feeds.value : feeds._value;
        if (feedsData) {
            return JSON.stringify(feedsData);
        }
    }
    return "";
})()
"""

# 从 DOM 提取搜索结果的 JS（rednote.com 使用客户端渲染，数据不在 __INITIAL_STATE__ 中）
_EXTRACT_SEARCH_FROM_DOM_JS = """
(() => {
    // rednote.com is client-rendered and its Vue scoped-attribute hash rotates
    // (data-v-4832212a -> data-v-f1b86190 -> ...), and search links moved from
    // /explore/{id}?xsec_token= to /search_result/{id}. So anchor on the stable
    // structural classes (a.cover / a.title / a.author) instead of the hash,
    // and derive each card by climbing from its cover link. See
    // references/rednote-dom-extraction.md.
    const idFrom = (href) => {
        if (!href) return '';
        const m = href.match(/\\/(?:explore|search_result)\\/([a-zA-Z0-9]+)/);
        return m ? m[1] : '';
    };
    // Each result card is a <section.note-item> (stable class); fall back to the
    // element wrapping a single a.cover if the class ever changes.
    let cards = Array.from(document.querySelectorAll('section.note-item'));
    if (cards.length === 0) {
        const seenRoots = new Set();
        document.querySelectorAll('a.cover').forEach(cover => {
            let n = cover, best = cover;
            for (let i = 0; i < 8 && n && n.parentElement; i++) {
                if (n.parentElement.querySelectorAll('a.cover').length !== 1) break;
                best = n.parentElement; n = best;
            }
            if (!seenRoots.has(best)) { seenRoots.add(best); cards.push(best); }
        });
    }
    const seen = new Set();
    const feeds = [];
    cards.forEach(card => {
        const coverLink = card.querySelector('a.cover') || card.querySelector('a[href*="/explore/"], a[href*="/search_result/"]');
        if (!coverLink) return;

        // Feed ID: the cover href, or any explore/search_result link in the card.
        let feedId = idFrom(coverLink.getAttribute('href'));
        if (!feedId) {
            const anyLink = card.querySelector('a[href*="/explore/"], a[href*="/search_result/"]');
            if (anyLink) feedId = idFrom(anyLink.getAttribute('href'));
        }
        if (!feedId || seen.has(feedId)) return;
        seen.add(feedId);

        // xsec_token if the href still carries one (older layout / some routes).
        let xsecToken = '';
        const tok = (coverLink.getAttribute('href') || '').match(/xsec_token=([^&]+)/);
        if (tok) xsecToken = decodeURIComponent(tok[1]);

        const img = coverLink.querySelector('img');
        const coverUrl = img ? img.src : '';

        const type = card.querySelector('.play-icon') ? 'video' : 'normal';

        const titleEl = card.querySelector('a.title span') || card.querySelector('a.title');
        const displayTitle = titleEl ? titleEl.textContent.trim() : '';

        const authorLink = card.querySelector('a.author');
        let userId = '', nickname = '';
        if (authorLink) {
            const m = (authorLink.getAttribute('href') || '').match(/profile\\/([a-zA-Z0-9]+)/);
            if (m) userId = m[1];
            const nameEl = authorLink.querySelector('.name');
            if (nameEl) nickname = nameEl.textContent.trim();
        }
        if (!nickname) {
            const nameEl = card.querySelector('.author .name, .name');
            if (nameEl) nickname = nameEl.textContent.trim();
        }

        const likeEl = card.querySelector('.like-wrapper .count, .like-wrapper span, .count');
        const likedCount = likeEl ? likeEl.textContent.trim() : '0';

        feeds.push({
            id: feedId,
            xsecToken: xsecToken,
            modelType: type,
            noteCard: {
                type: type,
                displayTitle: displayTitle,
                user: { userId: userId, nickname: nickname },
                interactInfo: { likedCount: likedCount },
                cover: { url: coverUrl }
            }
        });
    });
    return JSON.stringify(feeds);
})()
"""


def _find_internal_option(group_index: int, text: str) -> tuple[int, int]:
    """查找内部筛选选项索引。

    Returns:
        (filters_index, tags_index)

    Raises:
        ValueError: 未找到匹配的选项。
    """
    options = _FILTER_OPTIONS.get(group_index)
    if not options:
        raise ValueError(f"筛选组 {group_index} 不存在")

    for tags_index, option_text in options:
        if option_text == text:
            return group_index, tags_index

    valid = [t for _, t in options]
    raise ValueError(f"在筛选组 {group_index} 中未找到 '{text}'，有效值: {valid}")


def _convert_filters(filter_opt: FilterOption) -> list[tuple[int, int]]:
    """将 FilterOption 转换为内部 (filters_index, tags_index) 列表。"""
    result: list[tuple[int, int]] = []

    if filter_opt.sort_by:
        result.append(_find_internal_option(1, filter_opt.sort_by))
    if filter_opt.note_type:
        result.append(_find_internal_option(2, filter_opt.note_type))
    if filter_opt.publish_time:
        result.append(_find_internal_option(3, filter_opt.publish_time))
    if filter_opt.search_scope:
        result.append(_find_internal_option(4, filter_opt.search_scope))
    if filter_opt.location:
        result.append(_find_internal_option(5, filter_opt.location))

    return result


def search_feeds(
    page: Page,
    keyword: str,
    filter_option: FilterOption | None = None,
) -> list[Feed]:
    """搜索 Feeds。

    Args:
        page: CDP 页面对象。
        keyword: 搜索关键词。
        filter_option: 可选筛选条件。

    Raises:
        NoFeedsError: 没有捕获到搜索结果。
        ValueError: 筛选选项无效。
    """
    search_url = make_search_url(keyword)
    page.navigate(search_url)
    page.wait_for_load()
    page.wait_dom_stable()

    # Wait until __INITIAL_STATE__.search.feeds is HYDRATED (entries carry a
    # title). On rednote.com the state object appears early with id-only feeds
    # and fills in titles + xsec_token a beat later; extracting before then is
    # what produced empty titles. Preferring the hydrated state also gives each
    # feed its xsec_token, which get_feed_detail needs and the search DOM no
    # longer exposes.
    _wait_for_initial_state(page)

    # 应用筛选条件
    if filter_option:
        internal_filters = _convert_filters(filter_option)
        if internal_filters:
            _apply_filters(page, internal_filters)

    # 提取搜索结果：优先 __INITIAL_STATE__（含标题与 xsec_token），失败再从渲染后的
    # DOM（section.note-item）抓取（DOM 无 token，仅作兜底）。
    result = page.evaluate(_EXTRACT_SEARCH_JS)
    if not result or _titled_feed_count(result) == 0:
        logger.info("__INITIAL_STATE__ 无可用标题，回退到 DOM 提取（section.note-item）...")
        _wait_for_search_cards(page)
        dom_result = page.evaluate(_EXTRACT_SEARCH_FROM_DOM_JS)
        if dom_result and _titled_feed_count(dom_result) >= _titled_feed_count(result or "[]"):
            result = dom_result
        elif not result:
            result = dom_result
    if not result:
        raise NoFeedsError()

    feeds_data = json.loads(result)
    return [Feed.from_dict(f) for f in feeds_data]


def _wait_for_search_cards(page: Page, timeout: float = 20.0) -> None:
    """Wait for rednote's client-rendered cards AND their titles to render.

    The cards appear as skeletons first and their a.title text fills in a beat
    later, so waiting only on card count extracts empty titles; wait until the
    count of cards whose title is non-empty stops growing."""
    deadline = time.monotonic() + timeout
    last_titled = -1
    while time.monotonic() < deadline:
        try:
            titled = int(
                page.evaluate(
                    "Array.from(document.querySelectorAll('section.note-item'))"
                    ".filter(c => (c.querySelector('a.title') || {}).textContent"
                    " && c.querySelector('a.title').textContent.trim()).length"
                )
            )
        except Exception:
            titled = 0
        if titled > 0 and titled == last_titled:
            return
        last_titled = titled
        time.sleep(1.0)


_FEEDS_HYDRATED_JS = """
(() => {
    const s = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.search;
    if (!s || s.feeds === undefined) return -1;                 // no search state yet
    const f = s.feeds;
    const arr = (f && (f.value !== undefined ? f.value : f._value)) || [];
    if (!arr.length) return 0;                                  // shell, no feeds
    const titled = arr.filter(x => x && x.noteCard && (x.noteCard.displayTitle || '').trim()).length;
    return titled;                                              // >0 once hydrated
})()
"""


def _wait_for_initial_state(page: Page, timeout: float = 30.0) -> None:
    """等待 __INITIAL_STATE__.search.feeds 就绪并 *水合*（feed 带上标题）。

    rednote.com 上 state 对象会先以「只有 id」的形态出现，标题与 xsec_token 稍后填充；
    只等 `.search` 存在会过早提取到空标题。等到「带标题的 feed 数」不再增长为止，
    这样提取才同时拿到标题和 xsec_token。"""
    deadline = time.monotonic() + timeout
    last_titled = -1
    while time.monotonic() < deadline:
        try:
            titled = int(page.evaluate(_FEEDS_HYDRATED_JS))
        except Exception:
            titled = -1
        if titled > 0 and titled == last_titled:
            return  # hydrated and stable
        last_titled = titled
        time.sleep(0.5)
    logger.warning("等待 __INITIAL_STATE__ 水合超时 (titled=%s)", last_titled)


def _apply_filters(page: Page, filters: list[tuple[int, int]]) -> None:
    """应用筛选条件。通过文本内容定位筛选项（兼容不同域名 DOM 差异）。"""
    # 悬停筛选按钮
    page.hover_element(FILTER_BUTTON)

    # 等待筛选面板出现
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if page.has_element(FILTER_PANEL):
            break
        sleep_random(300, 600)

    # 通过文本内容点击筛选项（比 nth-child 更稳健）
    for filters_index, tags_index in filters:
        option_text = _FILTER_OPTIONS[filters_index][tags_index - 1][1]
        # 在筛选面板中查找匹配文本的标签并点击
        js = (
            f"(function(){{"
            f"var panel=document.querySelector('div.filter-panel');"
            f"if(!panel)return'no_panel';"
            f"var groups=panel.querySelectorAll('div.filters');"
            f"var group=groups[{filters_index - 1}];"
            f"if(!group)return'no_group';"
            f"var tags=group.querySelectorAll('div.tags,div.tag,span.tag,span.tags');"
            f"var target=Array.from(tags).find(function(t){{"
            f"return t.textContent.trim()==='{option_text}'}});"
            f"if(!target)return'no_tag';"
            f"target.click();"
            f"return'ok';"
            f"}})()"
        )
        result = page.evaluate(js)
        if result != "ok":
            logger.warning("筛选 %s 失败: %s", option_text, result)
        sleep_random(300, 600)

    # 等待页面更新
    page.wait_dom_stable()
    _wait_for_initial_state(page)
