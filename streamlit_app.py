from __future__ import annotations

import asyncio
import base64
import datetime as dt
import html
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse, urlunparse
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile

import orjson
import pandas as pd
import streamlit as st
from waifuboard import Booru
from waifuboard.utils import normalize_filepath


ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "data" / "collection_index.json"
DETAIL_API = "https://api.bilibili.com/x/vas/dlc_act/asset_bag"
REFERER = "https://www.bilibili.com"
APP_TIMEZONE = dt.timezone(dt.timedelta(hours=8), "UTC+8")
PAGE_SIZE_OPTIONS = [12, 24, 48, 96]
DEFAULT_PAGE_SIZE = 24
DETAIL_CONCURRENCY = 8
MEDIA_CONCURRENCY = 24
DETAIL_PROGRESS_WEIGHT = 0.35
MEDIA_CHUNK_SIZE = 1024 * 1024
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
ZIP_SPOOL_MAX_SIZE = 32 * 1024 * 1024
TRUSTED_MEDIA_HOST_SUFFIXES = ("hdslb.com", "bilibili.com", "bilivideo.com", "bilivideo.cn")
SELECTED_IDS_KEY = "selected_collection_ids"
ARCHIVE_BYTES_KEY = "archive_bytes"
ARCHIVE_NAME_KEY = "archive_name"
ARCHIVE_SELECTION_KEY = "archive_selection"
ARCHIVE_REPORT_KEY = "archive_report"
ARCHIVE_LOGS_KEY = "archive_logs"
BATCH_SELECT_KEY = "batch_select_ids"
PAGE_KEY = "result_page"
PENDING_PAGE_KEY = "pending_result_page"
RESULTS_TOP_ID = "collection-results-top"
SEARCH_HELP = """- 支持按收藏集 ID、名称、状态、描述、奖励和开售时间搜索，多个关键词会同时匹配。
- 示例：`鸣潮 预约` 会定位同时包含这两个关键词的收藏集。
- 搜索只读取每天自动更新的索引文件，不会请求 Bilibili 接口。"""
PAGE_SIZE_HELP = """- 控制当前页面一次渲染多少个收藏集卡片。
- 数量越大，页面封面越多，首次渲染和滚动会更重。
- 示例：浏览全量索引时可用 `24`，快速翻页时可用 `48`。"""
BATCH_SELECT_HELP = """- 从当前搜索结果中批量添加或移除收藏集。
- 这里的选择会和下方卡片 checkbox 保持同步。
- 示例：先搜索 `预约中`，再在这里多选需要下载的收藏集。"""
PAGE_SELECT_HELP = """- 勾选后选择当前页显示的所有收藏集。
- 取消勾选后只取消当前页，不影响其他分页上已选择的收藏集。
- 切换搜索条件或分页时，此 checkbox 会反映当前页是否已全部选中。"""
PREPARE_ARCHIVE_HELP = """- 只有点击这个按钮后，才会开始获取收藏集明细、下载公开图片/视频并写入 zip。
- 打包发生在运行 Streamlit 的服务端；本机运行时是本机，远程部署时是远程服务器。
- 选择变化后，已经生成的 zip 会失效，需要重新生成。"""
SAVE_ARCHIVE_HELP = """- 只有成功生成 zip 后才可点击。
- 点击后浏览器保存已经生成好的压缩包，不会再次请求 Bilibili 接口。
- 如果新增或取消选择，此按钮会重新变为不可点击，直到再次生成压缩包。"""


st.set_page_config(
    page_title="Bilibili collection downloader",
    page_icon=":material/download:",
    layout="wide",
)


st.markdown(
    """
    <style>
    :root {
        --bc-ink: #111827;
        --bc-muted: #64748b;
        --bc-border: rgba(15, 23, 42, 0.11);
        --bc-soft: #f8fafc;
        --bc-pink: #fb7299;
        --bc-teal: #00a1d6;
        --bc-green: #059669;
    }

    .block-container {
        padding-top: 2rem;
        padding-bottom: 2.25rem;
    }

    [data-testid="stMetric"] {
        background: transparent;
        border: 0;
        padding: 0;
    }

    [data-testid="stTextInput"] [data-testid="InputInstructions"],
    [data-testid="stNumberInput"] [data-testid="InputInstructions"] {
        display: none;
    }

    .app-kicker {
        color: var(--bc-teal);
        font-size: 0.78rem;
        font-weight: 760;
        letter-spacing: 0;
        margin-bottom: 0.35rem;
    }

    .app-title {
        color: var(--bc-ink);
        font-size: 2.1rem;
        font-weight: 780;
        line-height: 1.12;
        margin: 0;
    }

    .app-subtitle {
        color: var(--bc-muted);
        font-size: 0.98rem;
        line-height: 1.6;
        margin-top: 0.55rem;
        max-width: 900px;
    }

    .stat-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-top: 1rem;
    }

    .stat-chip {
        align-items: center;
        background: #f8fafc;
        border: 1px solid var(--bc-border);
        border-radius: 8px;
        color: var(--bc-ink);
        display: inline-flex;
        font-size: 0.82rem;
        gap: 0.38rem;
        line-height: 1;
        padding: 0.42rem 0.58rem;
        white-space: nowrap;
    }

    .stat-chip span {
        color: var(--bc-muted);
        font-size: 0.76rem;
    }

    .section-heading {
        align-items: center;
        color: var(--bc-ink);
        display: flex;
        font-size: 1rem;
        font-weight: 760;
        gap: 0.45rem;
        margin: 0.35rem 0 0.6rem;
    }

    .toolbar-note {
        color: var(--bc-muted);
        font-size: 0.84rem;
        margin-top: 0.25rem;
    }

    .collection-card {
        border: 1px solid var(--bc-border);
        border-radius: 8px;
        background: #ffffff;
        min-height: 312px;
        padding: 0.8rem;
    }

    .collection-cover {
        align-items: center;
        background:
            linear-gradient(135deg, rgba(0, 161, 214, 0.08), rgba(251, 114, 153, 0.10)),
            #f8fafc;
        border: 1px solid rgba(15, 23, 42, 0.07);
        border-radius: 8px;
        display: flex;
        height: 150px;
        justify-content: center;
        margin-bottom: 0.65rem;
        overflow: hidden;
        width: 100%;
    }

    .collection-cover img {
        height: 100%;
        object-fit: cover;
        width: 100%;
    }

    .collection-name {
        color: var(--bc-ink);
        font-size: 0.95rem;
        font-weight: 730;
        line-height: 1.35;
        margin-bottom: 0.25rem;
        min-height: 2.55rem;
        word-break: break-word;
    }

    .collection-meta {
        color: var(--bc-muted);
        font-size: 0.78rem;
        line-height: 1.45;
    }

    .pill-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem;
        margin-top: 0.45rem;
    }

    .pill {
        border-radius: 999px;
        border: 1px solid #dbeafe;
        background: #eff6ff;
        color: #1d4ed8;
        display: inline-flex;
        font-size: 0.72rem;
        font-weight: 700;
        padding: 0.12rem 0.46rem;
        white-space: nowrap;
    }

    .pill-pink {
        background: #fff1f5;
        border-color: #ffd6e3;
        color: #be185d;
    }

    .pill-green {
        background: #ecfdf5;
        border-color: #bbf7d0;
        color: #047857;
    }

    .selection-line {
        color: var(--bc-muted);
        font-size: 0.86rem;
        line-height: 1.5;
    }

    .selection-title {
        color: var(--bc-ink);
        font-size: 0.98rem;
        font-weight: 750;
        line-height: 1.35;
        margin-bottom: 0.2rem;
    }

    .section-anchor {
        height: 1px;
        overflow: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def load_index(path: str, mtime_ns: int) -> dict[str, Any]:
    _ = mtime_ns
    payload = orjson.loads(Path(path).read_bytes())

    collections = payload.get("collections", [])
    if not isinstance(collections, list):
        raise ValueError("data/collection_index.json does not contain a collection list")

    return payload


@st.cache_data(show_spinner=False)
def collection_frame(collections: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(collections)
    if frame.empty:
        return pd.DataFrame(columns=["id", "name", "_search_text"])

    for column in [
        "id",
        "name",
        "status",
        "description",
        "price_label",
        "display_title",
        "product_introduce",
        "start_time",
        "pre_start_time",
        "pre_end_time",
        "lottery_names",
        "share_main_title",
        "share_sub_title",
    ]:
        if column not in frame:
            frame[column] = ""

    def search_value(value: Any) -> str:
        if isinstance(value, list):
            return " ".join(str(item).strip() for item in value if str(item).strip())
        if value is None or pd.isna(value):
            return ""
        return str(value)

    frame["id"] = pd.to_numeric(frame["id"], errors="coerce").astype("Int64")
    text_parts = [
        frame["id"].astype(str),
        frame["name"].fillna("").astype(str),
        frame["status"].fillna("").astype(str),
        frame["description"].fillna("").astype(str),
        frame["price_label"].fillna("").astype(str),
        frame["display_title"].fillna("").astype(str),
        frame["product_introduce"].fillna("").astype(str),
        frame["start_time"].fillna("").astype(str),
        frame["start_time"].map(format_unix_shanghai).astype(str),
        frame["pre_start_time"].fillna("").astype(str),
        frame["pre_start_time"].map(format_unix_shanghai).astype(str),
        frame["pre_end_time"].fillna("").astype(str),
        frame["pre_end_time"].map(format_unix_shanghai).astype(str),
        frame["lottery_names"].map(search_value),
        frame["share_main_title"].fillna("").astype(str),
        frame["share_sub_title"].fillna("").astype(str),
    ]
    frame["_search_text"] = text_parts[0]
    for part in text_parts[1:]:
        frame["_search_text"] = frame["_search_text"].str.cat(part, sep=" ")
    frame["_search_text"] = frame["_search_text"].str.casefold()
    return frame


def init_state() -> None:
    st.session_state.setdefault(SELECTED_IDS_KEY, set())
    st.session_state.setdefault(PAGE_KEY, 1)
    st.session_state.setdefault(ARCHIVE_LOGS_KEY, [])


def selected_ids() -> set[int]:
    current_ids = st.session_state.setdefault(SELECTED_IDS_KEY, set())
    if not isinstance(current_ids, set):
        current_ids = set(current_ids)
        st.session_state[SELECTED_IDS_KEY] = current_ids
    return current_ids


def selection_signature() -> tuple[int, ...]:
    return tuple(sorted(selected_ids()))


def checkbox_key(collection_id: int) -> str:
    return f"select_{collection_id}"


def invalidate_archive() -> None:
    st.session_state.pop(ARCHIVE_BYTES_KEY, None)
    st.session_state.pop(ARCHIVE_NAME_KEY, None)
    st.session_state.pop(ARCHIVE_SELECTION_KEY, None)
    st.session_state.pop(ARCHIVE_REPORT_KEY, None)


def set_collection_selected(collection_id: int, selected: bool) -> None:
    current_ids = selected_ids()
    before = collection_id in current_ids
    if selected:
        current_ids.add(collection_id)
    else:
        current_ids.discard(collection_id)
    st.session_state[checkbox_key(collection_id)] = selected
    if before != selected:
        invalidate_archive()


def sync_collection_checkbox(collection_id: int) -> None:
    set_collection_selected(collection_id, bool(st.session_state.get(checkbox_key(collection_id))))


def sync_checkbox_widget(collection_id: int) -> None:
    st.session_state[checkbox_key(collection_id)] = collection_id in selected_ids()


def sync_batch_selection(option_ids: list[int]) -> None:
    current_ids = selected_ids()
    before = set(current_ids)
    option_id_set = set(option_ids)
    picked_ids = set(st.session_state.get(BATCH_SELECT_KEY, []))

    current_ids.difference_update(option_id_set)
    current_ids.update(picked_ids)
    for collection_id in option_ids:
        sync_checkbox_widget(collection_id)

    if current_ids != before:
        invalidate_archive()


def sync_page_selection(page_ids: list[int], key: str) -> None:
    selected = bool(st.session_state.get(key))
    for collection_id in page_ids:
        set_collection_selected(collection_id, selected)


def filter_collections(
    collections: list[dict[str, Any]],
    frame: pd.DataFrame,
    query: str,
) -> list[dict[str, Any]]:
    terms = [term.casefold() for term in query.split() if term.strip()]
    if not terms:
        return collections

    mask = pd.Series(True, index=frame.index)
    for term in terms:
        mask &= frame["_search_text"].str.contains(term, regex=False, na=False)

    matched_ids = set(frame.loc[mask, "id"].dropna().astype(int))
    return [collection for collection in collections if int(collection["id"]) in matched_ids]


def page_count(total: int, page_size: int) -> int:
    return max(1, (total + page_size - 1) // page_size)


def apply_pending_page(max_page: int) -> None:
    pending = st.session_state.pop(PENDING_PAGE_KEY, None)
    if pending is not None:
        st.session_state[PAGE_KEY] = min(max(1, int(pending)), max_page)
    else:
        st.session_state[PAGE_KEY] = min(max(1, int(st.session_state[PAGE_KEY])), max_page)


def request_page(page: int) -> None:
    st.session_state[PENDING_PAGE_KEY] = page
    st.rerun()


def collection_by_id(collections: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(collection["id"]): collection for collection in collections}


def extension_from_url(url: str, default: str = ".bin") -> str:
    path = unquote(urlparse(url).path)
    suffix = Path(path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    return default


def mime_from_url(url: str) -> str:
    suffix = extension_from_url(url).lower()
    return {
        ".avif": "image/avif",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".mp4": "video/mp4",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")


def format_unix_shanghai(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return "-"
    if timestamp <= 0:
        return "-"
    return dt.datetime.fromtimestamp(timestamp, APP_TIMEZONE).strftime("%Y/%m/%d %H:%M")


def trusted_media_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return None
    if not any(
        host == suffix or host.endswith(f".{suffix}") for suffix in TRUSTED_MEDIA_HOST_SUFFIXES
    ):
        return None

    return urlunparse(parsed._replace(scheme="https"))


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def preview_data_uri(url: str) -> str | None:
    safe_url = trusted_media_url(url)
    if safe_url is None:
        return None

    request = Request(
        safe_url,
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": REFERER,
            "User-Agent": "Mozilla/5.0 BilibiliCollectionDownloader/1.0",
        },
    )
    try:
        with urlopen(request, timeout=20.0) as response:
            content_type = response.headers.get_content_type() or mime_from_url(safe_url)
            content = response.read(PREVIEW_MAX_BYTES + 1)
            if len(content) > PREVIEW_MAX_BYTES:
                return None
            encoded = base64.b64encode(content).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
    except (HTTPError, URLError, TimeoutError):
        return None


def render_header(index: dict[str, Any], collections: list[dict[str, Any]]) -> None:
    updated_at = str(index.get("updated_at") or "").replace("T", " ").replace("Z", " UTC")
    st.markdown(
        f"""
        <div class="app-kicker">BILIBILI COLLECTION ARCHIVE</div>
        <h1 class="app-title">Bilibili 收藏集下载器</h1>
        <div class="app-subtitle">
        搜索每天自动更新的 Bilibili 收藏集索引。勾选收藏集后，点击生成按钮才会由运行 Streamlit 的服务端获取公开图片、视频和元数据，并打包为 zip 供浏览器保存。
        </div>
        <div class="stat-strip">
            <div class="stat-chip"><span>索引</span>{len(collections):,} 个</div>
            <div class="stat-chip"><span>已选择</span>{len(selected_ids()):,} 个</div>
            <div class="stat-chip"><span>更新</span>{html.escape(updated_at)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_collection_card(collection: dict[str, Any], selected_ids: set[int]) -> None:
    collection_id = int(collection["id"])
    safe_name = html.escape(str(collection["name"]))
    cover_url = str(collection.get("preview_cover_url") or collection["cover_url"])
    safe_cover_url = trusted_media_url(cover_url)
    cover_src = preview_data_uri(cover_url) or safe_cover_url
    image_html = (
        f'<img src="{html.escape(cover_src, quote=True)}" alt="{safe_name}" loading="lazy" referrerpolicy="no-referrer">'
        if cover_src
        else '<span class="collection-meta">封面加载失败</span>'
    )
    key = checkbox_key(collection_id)
    checked = collection_id in selected_ids
    st.session_state[key] = checked

    st.markdown(
        f"""
        <div class="collection-card">
            <div class="collection-cover">
                {image_html}
            </div>
            <div class="collection-name">{safe_name}</div>
            <div class="collection-meta">ID: {collection_id}</div>
            <div class="collection-meta">价格: {html.escape(str(collection.get("price_label") or "-"))}</div>
            <div class="collection-meta">开售: {html.escape(format_unix_shanghai(collection.get("start_time")))}</div>
            <div class="collection-meta">描述: {html.escape(str(collection.get("description") or "-"))}</div>
            <div class="pill-row">
                <span class="pill">{html.escape(str(collection.get("status") or "未知状态"))}</span>
                <span class="pill pill-pink">封面图</span>
                <span class="pill pill-green">图片 / 视频</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.checkbox(
        "选择",
        key=key,
        on_change=sync_collection_checkbox,
        args=(collection_id,),
    )


def make_booru_client() -> Booru:
    return Booru(
        logger_level=40,
        base_url=REFERER,
        multiplexed=False,
        proxies=None,
        trust_env=False,
        max_attempt_number=3,
        retries=3,
        rate_limit=None,
        timeout=60.0 * 5,
    )


async def fetch_collection_detail(
    client: Booru,
    collection: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        response = await client.get(
            DETAIL_API,
            params={"act_id": int(collection["id"])},
            referer=REFERER,
        )

    payload = response.json()
    if payload.get("code") != 0:
        message = payload.get("message") or payload.get("msg") or "unknown error"
        raise RuntimeError(f"[{collection['id']}] {collection['name']}: API code={payload.get('code')}: {message}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"[{collection['id']}] {collection['name']}: detail response missing data")

    return dict(collection) | data


async def fetch_collection_detail_job(
    client: Booru,
    collection: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return collection, await fetch_collection_detail(client, collection, semaphore)


def media_job_path(
    collection: dict[str, Any],
    category: str,
    name: str,
    url: str,
    kind: str,
) -> str:
    collection_dir = f"{int(collection['id'])}_{normalize_filepath(str(collection['name']))}"
    safe_name = normalize_filepath(name)
    default_ext = ".mp4" if kind == "video" else ".png"
    ext = extension_from_url(url, default_ext)
    root = "videos" if kind == "video" else "images"
    return f"bilibili-collection/{root}/{collection_dir}/{category}/{safe_name}{ext}"


def unique_archive_path(path: str, seen_paths: dict[str, int]) -> str:
    count = seen_paths.get(path, 0) + 1
    seen_paths[path] = count
    if count == 1:
        return path

    parsed = Path(path)
    return str(parsed.with_name(f"{parsed.stem}__{count}{parsed.suffix}"))


def media_job(
    collection: dict[str, Any],
    category: str,
    name: str,
    url: str,
    kind: str,
    seen_paths: dict[str, int],
) -> tuple[str, str, str]:
    path = media_job_path(collection, category, name, url, kind)
    return unique_archive_path(path, seen_paths), url, name


def iter_media_jobs(
    collection: dict[str, Any],
    detail: dict[str, Any],
) -> Iterable[tuple[str, str, str]]:
    seen_paths: dict[str, int] = {}
    cover_url = str(detail.get("act_y_img") or collection.get("cover_url") or "")
    safe_cover_url = trusted_media_url(cover_url)
    if safe_cover_url:
        yield media_job(
            collection, "cover", str(collection["name"]), safe_cover_url, "image", seen_paths
        )

    for item in detail.get("item_list", []) or []:
        if not isinstance(item, dict):
            continue
        card_item = item.get("card_item")
        if not isinstance(card_item, dict):
            continue

        card_name = str(card_item.get("card_name") or card_item.get("card_type_id") or "collection")
        img_url = trusted_media_url(str(card_item.get("card_img") or ""))
        if img_url:
            yield media_job(collection, "collection", card_name, img_url, "image", seen_paths)

        video_list = card_item.get("video_list")
        if isinstance(video_list, list) and video_list:
            video_url = trusted_media_url(str(video_list[-1]))
            if video_url:
                yield media_job(collection, "collection", card_name, video_url, "video", seen_paths)

    for collect in detail.get("collect_list", []) or []:
        if not isinstance(collect, dict) or int(collect.get("redeem_item_type") or 0) != 1:
            continue
        collect_card_item = collect.get("card_item")
        if not isinstance(collect_card_item, dict):
            continue
        card_asset_info = collect_card_item.get("card_asset_info")
        if not isinstance(card_asset_info, dict):
            continue
        card_item = card_asset_info.get("card_item")
        if not isinstance(card_item, dict):
            continue

        card_name = str(card_item.get("card_name") or collect.get("redeem_item_name") or "curation")
        img_url = trusted_media_url(str(card_item.get("card_img") or ""))
        if img_url:
            yield media_job(collection, "curation", card_name, img_url, "image", seen_paths)

        video_list = card_item.get("video_list")
        if isinstance(video_list, list) and video_list:
            video_url = trusted_media_url(str(video_list[-1]))
            if video_url:
                yield media_job(collection, "curation", card_name, video_url, "video", seen_paths)


def download_media_to_file(media_url: str, target_path: Path) -> None:
    request = Request(
        media_url,
        headers={
            "Accept": "*/*",
            "Referer": REFERER,
            "User-Agent": "Mozilla/5.0 BilibiliCollectionDownloader/1.0",
        },
    )
    with urlopen(request, timeout=60.0 * 5) as response, target_path.open("wb") as file:
        while chunk := response.read(MEDIA_CHUNK_SIZE):
            file.write(chunk)


async def fetch_media_file(
    media_path: str,
    media_url: str,
    media_name: str,
    target_path: Path,
    semaphore: asyncio.Semaphore,
) -> tuple[str, Path, str]:
    async with semaphore:
        await asyncio.to_thread(download_media_to_file, media_url, target_path)
    return media_path, target_path, media_name


ProgressCallback = Callable[[float, str, str | None], None]


async def build_zip_async(
    selected_collections: list[dict[str, Any]],
    progress_callback: ProgressCallback | None = None,
) -> tuple[bytes, dict[str, Any]]:
    client = make_booru_client()
    details: list[tuple[dict[str, Any], dict[str, Any]]] = []
    failures: list[str] = []
    succeeded_media = 0
    detail_semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
    media_semaphore = asyncio.Semaphore(MEDIA_CONCURRENCY)

    try:
        if progress_callback:
            progress_callback(0.0, "准备获取收藏集明细", None)

        detail_tasks = [
            asyncio.create_task(fetch_collection_detail_job(client, collection, detail_semaphore))
            for collection in selected_collections
        ]
        for completed_index, task in enumerate(asyncio.as_completed(detail_tasks), start=1):
            try:
                collection, detail = await task
            except Exception as exc:
                message = f"明细获取失败: {exc.__class__.__name__}: {exc}"
                failures.append(message)
                if progress_callback:
                    progress_callback(
                        DETAIL_PROGRESS_WEIGHT * completed_index / len(detail_tasks),
                        f"明细 {completed_index}/{len(detail_tasks)}",
                        message,
                    )
            else:
                details.append((collection, detail))
                if progress_callback:
                    progress_callback(
                        DETAIL_PROGRESS_WEIGHT * completed_index / len(detail_tasks),
                        f"明细 {completed_index}/{len(detail_tasks)}: {collection['name']}",
                        f"[{int(collection['id'])}] 已获取 {collection['name']} 明细",
                    )

        if not details:
            if progress_callback:
                progress_callback(1.0, "生成失败", "未能获取任何收藏集明细")
            raise RuntimeError("未能获取任何收藏集明细，无法生成压缩包。")

        with (
            tempfile.TemporaryDirectory(prefix="bilibili-collection-media-") as temp_dir,
            tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_MAX_SIZE) as buffer,
            ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive,
        ):
            media_jobs: list[tuple[str, str, str]] = []
            for collection, detail in details:
                json_path = (
                    f"bilibili-collection/jsons/"
                    f"{int(collection['id'])}_{normalize_filepath(str(collection['name']))}.json"
                )
                archive.writestr(
                    json_path,
                    orjson.dumps(detail, option=orjson.OPT_INDENT_2).decode("utf-8") + "\n",
                )
                media_jobs.extend(iter_media_jobs(collection, detail))

            if not media_jobs:
                if progress_callback:
                    progress_callback(1.0, "没有可下载媒体，已写入元数据", None)
                archive.close()
                buffer.seek(0)
                return buffer.read(), {
                    "collections": len(details),
                    "media": 0,
                    "failures": failures,
                }

            temp_root = Path(temp_dir)
            media_tasks = [
                asyncio.create_task(
                    fetch_media_file(
                        media_path,
                        media_url,
                        media_name,
                        temp_root / f"media_{index}",
                        media_semaphore,
                    )
                )
                for index, (media_path, media_url, media_name) in enumerate(media_jobs)
            ]
            for completed_index, task in enumerate(asyncio.as_completed(media_tasks), start=1):
                try:
                    media_path, temp_path, media_name = await task
                except Exception as exc:
                    message = f"媒体下载失败: {exc.__class__.__name__}: {exc}"
                    failures.append(message)
                    if progress_callback:
                        progress_callback(
                            DETAIL_PROGRESS_WEIGHT
                            + (1 - DETAIL_PROGRESS_WEIGHT) * completed_index / len(media_tasks),
                            f"媒体 {completed_index}/{len(media_tasks)}",
                            message,
                        )
                    continue

                archive.write(temp_path, media_path)
                temp_path.unlink(missing_ok=True)
                succeeded_media += 1
                if progress_callback:
                    progress_callback(
                        DETAIL_PROGRESS_WEIGHT
                        + (1 - DETAIL_PROGRESS_WEIGHT) * completed_index / len(media_tasks),
                        f"媒体 {completed_index}/{len(media_tasks)}: {media_name}",
                        None,
                    )

            archive.close()
            buffer.seek(0)
            archive_bytes = buffer.read()

        return archive_bytes, {
            "collections": len(details),
            "media": succeeded_media,
            "failures": failures,
        }
    finally:
        await client.client.close()


def build_zip(
    selected_collections: list[dict[str, Any]],
    progress_callback: ProgressCallback | None = None,
) -> tuple[bytes, dict[str, Any]]:
    return asyncio.run(build_zip_async(selected_collections, progress_callback))


def selected_collections(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = collection_by_id(collections)
    current_ids = sorted(selected_ids())
    return [by_id[collection_id] for collection_id in current_ids if collection_id in by_id]


def option_label(collection_id: int, collections_by_id: dict[int, dict[str, Any]]) -> str:
    collection = collections_by_id[collection_id]
    status = str(collection.get("status") or "未知状态")
    return f"{collection_id} · {collection['name']} · {status}"


def sync_filtered_multiselect(
    filtered: list[dict[str, Any]],
    collections_by_id: dict[int, dict[str, Any]],
) -> None:
    option_ids = [int(collection["id"]) for collection in filtered]
    current_ids = selected_ids()
    st.session_state[BATCH_SELECT_KEY] = [
        collection_id for collection_id in option_ids if collection_id in current_ids
    ]

    st.multiselect(
        "批量选择收藏集",
        options=option_ids,
        format_func=lambda collection_id: option_label(collection_id, collections_by_id),
        help=BATCH_SELECT_HELP,
        placeholder="输入 ID、名称或状态后，在这里批量添加或移除匹配结果",
        key=BATCH_SELECT_KEY,
        on_change=sync_batch_selection,
        args=(option_ids,),
    )


def render_page_select_all(page_items: list[dict[str, Any]], page: int) -> None:
    if not page_items:
        return

    page_ids = [int(collection["id"]) for collection in page_items]
    key = f"select_page_{page}_{'-'.join(map(str, page_ids[:3]))}_{len(page_ids)}"
    all_selected = all(collection_id in selected_ids() for collection_id in page_ids)
    st.session_state[key] = all_selected
    st.checkbox(
        "选择当前页全部",
        key=key,
        help=PAGE_SELECT_HELP,
        on_change=sync_page_selection,
        args=(page_ids, key),
    )


def render_pagination(page: int, max_page: int, label: str) -> None:
    if max_page <= 1:
        return

    cols = st.columns([1, 1, 1.2, 1, 1], vertical_alignment="center")
    with cols[0]:
        if st.button("首页", disabled=page <= 1, key=f"{label}_first", icon=":material/first_page:"):
            request_page(1)
    with cols[1]:
        if st.button("上一页", disabled=page <= 1, key=f"{label}_prev", icon=":material/chevron_left:"):
            request_page(page - 1)
    with cols[2]:
        st.markdown(
            f"<div class='selection-line' style='text-align:center;'>第 {page} / {max_page} 页</div>",
            unsafe_allow_html=True,
        )
    with cols[3]:
        if st.button("下一页", disabled=page >= max_page, key=f"{label}_next", icon=":material/chevron_right:"):
            request_page(page + 1)
    with cols[4]:
        if st.button("末页", disabled=page >= max_page, key=f"{label}_last", icon=":material/last_page:"):
            request_page(max_page)


def archive_ready_for_current_selection() -> bool:
    return (
        st.session_state.get(ARCHIVE_BYTES_KEY) is not None
        and st.session_state.get(ARCHIVE_SELECTION_KEY) == selection_signature()
    )


def render_archive_report() -> None:
    report = st.session_state.get(ARCHIVE_REPORT_KEY)
    if not isinstance(report, dict):
        return

    failures = report.get("failures") or []
    st.markdown(
        f"""
        <div class="selection-line">
        已写入 {int(report.get("collections") or 0):,} 个收藏集 metadata，
        {int(report.get("media") or 0):,} 个媒体文件。
        </div>
        """,
        unsafe_allow_html=True,
    )
    if failures:
        with st.expander(f"查看 {len(failures)} 条失败日志", icon=":material/warning:"):
            for failure in failures:
                st.code(str(failure), language="text")


def render_persistent_logs() -> None:
    logs = st.session_state.get(ARCHIVE_LOGS_KEY) or []
    if not logs:
        return

    with st.expander("查看最近一次生成日志", icon=":material/subject:"):
        for log in logs[-120:]:
            st.code(str(log), language="text")


def render_download_panel(collections: list[dict[str, Any]]) -> None:
    picked = selected_collections(collections)
    selected_count = len(picked)
    ready = archive_ready_for_current_selection()

    st.markdown('<div class="section-heading">:material/download: 下载</div>', unsafe_allow_html=True)
    left_col, right_col = st.columns([1, 1], vertical_alignment="bottom")
    with left_col:
        st.markdown(
            f"""
            <div class="selection-title">已选择 {selected_count:,} 个收藏集</div>
            <div class="selection-line">生成压缩包后，保存按钮才会可用；选择变化会让已生成的压缩包失效。</div>
            """,
            unsafe_allow_html=True,
        )
    with right_col:
        if selected_count == 0:
            st.button(
                "生成压缩包",
                icon=":material/archive:",
                disabled=True,
                help=PREPARE_ARCHIVE_HELP,
                width="stretch",
            )
        else:
            if st.button(
                "生成压缩包",
                icon=":material/archive:",
                help=PREPARE_ARCHIVE_HELP,
                width="stretch",
            ):
                progress = st.progress(0.0, text="准备生成压缩包")
                logs: list[str] = []
                st.session_state[ARCHIVE_LOGS_KEY] = logs

                def report_progress(value: float, text: str, log: str | None) -> None:
                    progress.progress(min(max(value, 0.0), 1.0), text=text)
                    if log:
                        logs.append(log)
                        st.session_state[ARCHIVE_LOGS_KEY] = logs

                try:
                    archive_bytes, report = build_zip(picked, report_progress)
                except RuntimeError as exc:
                    progress.progress(1.0, text="生成失败")
                    st.error(str(exc), icon=":material/error:")
                else:
                    timestamp = dt.datetime.now(APP_TIMEZONE).strftime("%Y%m%d-%H%M%S")
                    st.session_state[ARCHIVE_BYTES_KEY] = archive_bytes
                    st.session_state[ARCHIVE_NAME_KEY] = f"bilibili-collection-{timestamp}.zip"
                    st.session_state[ARCHIVE_SELECTION_KEY] = selection_signature()
                    st.session_state[ARCHIVE_REPORT_KEY] = report
                    progress.progress(1.0, text="压缩包已生成")
                    st.toast("压缩包已生成，可以保存。", icon=":material/check_circle:")
                    ready = True

    if not ready:
        st.download_button(
            "保存压缩包",
            data=b"",
            file_name="bilibili-collection.zip",
            mime="application/zip",
            disabled=True,
            icon=":material/save:",
            help=SAVE_ARCHIVE_HELP,
            width="stretch",
        )
    else:
        st.download_button(
            "保存压缩包",
            data=st.session_state[ARCHIVE_BYTES_KEY],
            file_name=st.session_state.get(ARCHIVE_NAME_KEY, "bilibili-collection.zip"),
            mime="application/zip",
            icon=":material/save:",
            help=SAVE_ARCHIVE_HELP,
            width="stretch",
        )

    render_archive_report()
    render_persistent_logs()


def render_grid(page_items: list[dict[str, Any]]) -> None:
    if not page_items:
        st.info("没有匹配的收藏集。", icon=":material/search_off:")
        return

    for start in range(0, len(page_items), 4):
        cols = st.columns(4)
        for col, collection in zip(cols, page_items[start : start + 4], strict=False):
            with col:
                render_collection_card(collection, selected_ids())


def main() -> None:
    init_state()
    if not INDEX_PATH.exists():
        st.error("缺少 data/collection_index.json，请先运行 uv run scripts/update_collection_index.py。")
        return

    index = load_index(str(INDEX_PATH), INDEX_PATH.stat().st_mtime_ns)
    collections = list(index["collections"])
    frame = collection_frame(collections)
    collections_by_id = collection_by_id(collections)

    render_header(index, collections)

    top_cols = st.columns([2.6, 1, 1], vertical_alignment="bottom")
    with top_cols[0]:
        query = st.text_input(
            "搜索收藏集",
            placeholder="输入 ID、名称、状态或描述",
            help=SEARCH_HELP,
        )
    with top_cols[1]:
        page_size = st.selectbox(
            "每页数量",
            PAGE_SIZE_OPTIONS,
            index=PAGE_SIZE_OPTIONS.index(DEFAULT_PAGE_SIZE),
            help=PAGE_SIZE_HELP,
        )
    with top_cols[2]:
        if st.button("清空选择", icon=":material/delete:", disabled=len(selected_ids()) == 0, width="stretch"):
            for collection_id in list(selected_ids()):
                st.session_state[checkbox_key(collection_id)] = False
            selected_ids().clear()
            invalidate_archive()
            st.rerun()

    filtered = filter_collections(collections, frame, query)
    max_page = page_count(len(filtered), page_size)
    apply_pending_page(max_page)
    page = int(st.session_state[PAGE_KEY])
    start = (page - 1) * page_size
    page_items = filtered[start : start + page_size]

    st.markdown(
        f"""
        <div class="toolbar-note">
        当前显示 {len(page_items):,} / {len(filtered):,} 个匹配收藏集；索引共 {len(collections):,} 个。
        </div>
        <div id="{RESULTS_TOP_ID}" class="section-anchor"></div>
        """,
        unsafe_allow_html=True,
    )

    sync_filtered_multiselect(filtered, collections_by_id)
    render_page_select_all(page_items, page)
    render_download_panel(collections)
    render_pagination(page, max_page, "top")
    render_grid(page_items)
    render_pagination(page, max_page, "bottom")


if __name__ == "__main__":
    main()
