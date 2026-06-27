#!/usr/bin/env python3
"""Fetch Bilibili collection index and update README."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import html
import json
from pathlib import Path
import random
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


API_URL = "https://api.bilibili.com/x/vas/dlc_act/act/list"
BASIC_API_URL = "https://api.bilibili.com/x/vas/dlc_act/act/basic"
README_START = "<!-- BILIBILI_COLLECTION_INDEX_START -->"
README_END = "<!-- BILIBILI_COLLECTION_INDEX_END -->"
SHANGHAI_TZ = dt.timezone(dt.timedelta(hours=8))
BASIC_WORKERS = 4
REQUEST_RETRIES = 3
COVER_PREVIEW_WIDTH = 300
USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
)
ENRICHED_FIELDS = (
    "basic_fetched_at",
    "basic_error",
    "title",
    "preview_cover_url",
    "start_time",
    "end_time",
    "pre_start_time",
    "pre_end_time",
    "display_title",
    "effective_forever",
    "is_can_donate",
    "is_up_chain",
    "is_collector_rank",
    "is_pre",
    "total_book_cnt",
    "total_buy_cnt",
    "product_introduce",
    "share_main_title",
    "share_sub_title",
    "related_mids",
    "lottery_count",
    "lottery_names",
    "lottery_types",
    "lottery_prices",
    "item_total_count",
    "total_sale_amount",
)
REQUIRED_BASIC_FIELDS = (
    "basic_fetched_at",
    "preview_cover_url",
    "start_time",
    "pre_start_time",
    "lottery_count",
    "item_total_count",
    "total_book_cnt",
    "total_buy_cnt",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update README with the latest Bilibili collection index."
    )
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--data", type=Path, default=Path("data/collection_index.json"))
    parser.add_argument("--recent-days", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Rebuild generated files from the existing data file without API access.",
    )
    return parser.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def request_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Origin": "https://www.bilibili.com",
            "Referer": "https://www.bilibili.com",
            "User-Agent": random.choice(USER_AGENTS),
        },
    )
    opener = build_opener(ProxyHandler({}))

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                response_body = response.read()
            payload = json.loads(response_body.decode("utf-8"))
            break
        except HTTPError as exc:
            if exc.code in {412, 429, 500, 502, 503, 504} and attempt < REQUEST_RETRIES:
                time.sleep(0.8 * attempt)
                continue
            raise RuntimeError(f"Bilibili API returned HTTP {exc.code}") from exc
        except URLError as exc:
            if attempt < REQUEST_RETRIES:
                time.sleep(0.8 * attempt)
                continue
            raise RuntimeError(f"Failed to request Bilibili API: {exc.reason}") from exc
        except (TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if attempt < REQUEST_RETRIES:
                time.sleep(0.8 * attempt)
                continue
            raise RuntimeError(f"Failed to parse Bilibili API response: {url}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Bilibili API response is not a JSON object")
    if payload.get("code") != 0:
        message = payload.get("message") or payload.get("msg") or "unknown error"
        raise RuntimeError(f"Bilibili API returned code={payload.get('code')}: {message}")

    return payload


def fetch_basic_info(act_id: int, timeout: float) -> dict[str, Any]:
    query = urlencode({"csrf": "", "act_id": act_id})
    payload = request_json(f"{BASIC_API_URL}?{query}", timeout)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Bilibili basic API did not return data for act_id={act_id}")
    return data


def fetch_collections(timeout: float) -> list[dict[str, Any]]:
    site = 0
    records: list[dict[str, Any]] = []

    while True:
        query = urlencode({"scene": 1, "site": site})
        payload = request_json(f"{API_URL}?{query}", timeout)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Bilibili API response does not contain a data object")

        items = data.get("list")
        if not isinstance(items, list):
            raise RuntimeError("Bilibili API response does not contain a collection list")
        records.extend(item for item in items if isinstance(item, dict))

        if not data.get("is_more"):
            break
        try:
            site = int(data["site"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Bilibili API pagination did not return a valid site") from exc

    return records


def load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "collections": []}

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} does not contain a JSON object")

    return payload


def collection_id(collection: dict[str, Any]) -> int | None:
    try:
        return int(collection["act_id"])
    except (KeyError, TypeError, ValueError):
        return None


def price_label(value: Any) -> str:
    try:
        milli_yuan = int(value)
    except (TypeError, ValueError):
        return ""
    if milli_yuan <= 0:
        return ""
    amount = f"{milli_yuan / 1000:.2f}".rstrip("0").rstrip(".")
    return f"{amount} 元"


def normalize_time(value: str, fallback: str) -> str:
    return value if value else fallback


def shanghai_date_from_iso(value: str) -> dt.date | None:
    try:
        normalized = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(normalized).astimezone(SHANGHAI_TZ).date()
    except ValueError:
        return None


def format_iso_shanghai(value: str) -> str:
    try:
        normalized = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(normalized).astimezone(SHANGHAI_TZ).strftime(
            "%Y/%m/%d %H:%M"
        )
    except ValueError:
        return ""


def format_unix_shanghai(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return dt.datetime.fromtimestamp(timestamp, SHANGHAI_TZ).strftime("%Y/%m/%d %H:%M")


def shanghai_date_from_unix(value: Any) -> dt.date | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return dt.datetime.fromtimestamp(timestamp, SHANGHAI_TZ).date()


def compact_int(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:,}"


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sum_int_field(records: list[dict[str, Any]], field: str) -> int | None:
    total = 0
    found = False
    for record in records:
        value = int_or_none(record.get(field))
        if value is None:
            continue
        total += value
        found = True
    return total if found else None


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def list_text(values: list[Any], limit: int = 3) -> str:
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    if not cleaned:
        return ""
    visible = cleaned[:limit]
    suffix = f" 等 {len(cleaned)} 个" if len(cleaned) > limit else ""
    return "、".join(visible) + suffix


def lottery_price_label(prices: list[Any], fallback: Any) -> str:
    unique_prices = sorted({int(price) for price in prices if int_or_none(price) is not None})
    if not unique_prices:
        return price_label(fallback)
    labels = [price_label(price) for price in unique_prices]
    labels = [label for label in labels if label]
    if not labels:
        return price_label(fallback)
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]} - {labels[-1]}"


def basic_fields(basic: dict[str, Any], now_iso: str, sale_price: Any) -> dict[str, Any]:
    lottery_list = basic.get("lottery_list")
    lotteries = [item for item in lottery_list if isinstance(item, dict)] if isinstance(lottery_list, list) else []
    lottery_prices = [lottery.get("price") for lottery in lotteries if lottery.get("price") is not None]
    item_total_count = sum_int_field(lotteries, "item_total_cnt")
    total_sale_amount = sum_int_field(lotteries, "total_sale_amount")
    share_info = basic.get("share_info") if isinstance(basic.get("share_info"), dict) else {}

    return {
        "basic_fetched_at": now_iso,
        "basic_error": "",
        "title": str(basic.get("act_title") or "").strip(),
        "preview_cover_url": str(basic.get("act_y_img") or "").strip(),
        "start_time": int_or_none(basic.get("start_time")),
        "end_time": int_or_none(basic.get("end_time")),
        "pre_start_time": int_or_none(basic.get("pre_start_time")),
        "pre_end_time": int_or_none(basic.get("pre_end_time")),
        "display_title": str(basic.get("display_title") or "").strip(),
        "effective_forever": bool_or_none(basic.get("effective_forever")),
        "is_can_donate": bool_or_none(basic.get("is_can_donate")),
        "is_up_chain": bool_or_none(basic.get("is_up_chain")),
        "is_collector_rank": bool_or_none(basic.get("is_collector_rank")),
        "is_pre": bool_or_none(basic.get("is_pre")),
        "total_book_cnt": int_or_none(basic.get("total_book_cnt")),
        "total_buy_cnt": int_or_none(basic.get("total_buy_cnt")),
        "product_introduce": str(basic.get("product_introduce") or "").strip(),
        "share_main_title": str(share_info.get("main_title") or "").strip(),
        "share_sub_title": str(share_info.get("sub_title") or "").strip(),
        "related_mids": basic.get("related_mids") if isinstance(basic.get("related_mids"), list) else [],
        "lottery_count": len(lotteries),
        "lottery_names": [str(lottery.get("lottery_name") or "").strip() for lottery in lotteries],
        "lottery_types": sorted(
            {
                int(lottery["lottery_type"])
                for lottery in lotteries
                if int_or_none(lottery.get("lottery_type")) is not None
            }
        ),
        "lottery_prices": lottery_prices,
        "item_total_count": item_total_count,
        "total_sale_amount": total_sale_amount,
        "price_label": lottery_price_label(lottery_prices, sale_price),
    }


def needs_basic_info(record: dict[str, Any]) -> bool:
    return any(field not in record or record.get(field) in {None, ""} for field in REQUIRED_BASIC_FIELDS)


def enrich_missing_basic_info(
    records: list[dict[str, Any]],
    timeout: float,
    now_iso: str,
) -> list[dict[str, Any]]:
    missing = [record for record in records if needs_basic_info(record)]
    if not missing:
        return records

    by_id = {int(record["id"]): record for record in missing}
    print(f"Fetching basic metadata for {len(missing)} collections")
    with ThreadPoolExecutor(max_workers=BASIC_WORKERS) as executor:
        future_by_id = {
            executor.submit(fetch_basic_info, int(record["id"]), timeout): int(record["id"])
            for record in missing
        }
        for future in as_completed(future_by_id):
            act_id = future_by_id[future]
            record = by_id[act_id]
            try:
                basic = future.result()
                record.update(basic_fields(basic, now_iso, record.get("sale_price")))
            except Exception as exc:
                record["basic_error"] = f"{exc.__class__.__name__}: {exc}"
                continue

    return records


def collection_record(
    collection: dict[str, Any],
    previous: dict[str, Any] | None,
    now_iso: str,
    initial_import: bool,
) -> dict[str, Any] | None:
    act_id = collection_id(collection)
    name = str(collection.get("act_name") or "").strip()
    cover_url = str(collection.get("act_pic") or "").strip()
    if act_id is None or not name or not cover_url:
        return None

    first_seen_at = now_iso
    if previous and previous.get("first_seen_at"):
        first_seen_at = str(previous["first_seen_at"])

    record = {
        "id": act_id,
        "name": name,
        "cover_url": cover_url,
        "sale_price": collection.get("sale_price"),
        "price_label": price_label(collection.get("sale_price")),
        "status": str(collection.get("tag") or "").strip(),
        "description": str(collection.get("act_desc") or "").strip(),
        "lottery_id": collection.get("lottery_id"),
        "lottery_type": collection.get("lottery_type"),
        "link": str(collection.get("act_link") or "").strip(),
        "first_seen_at": first_seen_at,
        "last_seen_at": now_iso,
        "initial_import": bool(previous.get("initial_import")) if previous else initial_import,
    }
    if previous:
        for field in ENRICHED_FIELDS:
            if field in previous:
                record[field] = previous[field]
    return record


def normalize_existing_collection(
    collection: dict[str, Any], now_iso: str
) -> dict[str, Any] | None:
    act_id = collection_id(collection) or collection.get("id")
    try:
        act_id = int(act_id)
    except (TypeError, ValueError):
        return None

    name = str(collection.get("name") or collection.get("act_name") or "").strip()
    cover_url = str(collection.get("cover_url") or collection.get("act_pic") or "").strip()
    if not name or not cover_url:
        return None

    first_seen_at = normalize_time(str(collection.get("first_seen_at") or ""), now_iso)
    record = {
        "id": act_id,
        "name": name,
        "cover_url": cover_url,
        "sale_price": collection.get("sale_price"),
        "price_label": str(collection.get("price_label") or price_label(collection.get("sale_price"))),
        "status": str(collection.get("status") or collection.get("tag") or "").strip(),
        "description": str(collection.get("description") or collection.get("act_desc") or "").strip(),
        "lottery_id": collection.get("lottery_id"),
        "lottery_type": collection.get("lottery_type"),
        "link": str(collection.get("link") or collection.get("act_link") or "").strip(),
        "first_seen_at": first_seen_at,
        "last_seen_at": normalize_time(str(collection.get("last_seen_at") or ""), now_iso),
        "initial_import": bool(collection.get("initial_import")),
    }
    for field in ENRICHED_FIELDS:
        if field in collection:
            record[field] = collection[field]
    return record


def normalize_index(existing: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    now_iso = now.isoformat().replace("+00:00", "Z")
    records = [
        record
        for collection in existing.get("collections", [])
        if isinstance(collection, dict)
        for record in [normalize_existing_collection(collection, now_iso)]
        if record is not None
    ]
    records.sort(key=lambda item: (item["id"], item["name"]))
    return {
        "schema_version": 1,
        "source": existing.get("source") or API_URL,
        "updated_at": str(existing.get("updated_at") or now_iso),
        "count": len(records),
        "collections": records,
    }


def merge_collections(
    fetched: list[dict[str, Any]], existing: dict[str, Any], now: dt.datetime, timeout: float
) -> dict[str, Any]:
    previous_by_id = {
        int(collection["id"]): collection
        for collection in existing.get("collections", [])
        if isinstance(collection, dict) and isinstance(collection.get("id"), int)
    }
    now_iso = now.isoformat().replace("+00:00", "Z")

    records: list[dict[str, Any]] = []
    initial_import = not previous_by_id
    for collection in fetched:
        act_id = collection_id(collection)
        previous = previous_by_id.get(act_id) if act_id is not None else None
        record = collection_record(collection, previous, now_iso, initial_import)
        if record is not None:
            records.append(record)

    records = enrich_missing_basic_info(records, timeout=timeout, now_iso=now_iso)
    records.sort(key=lambda item: (item["id"], item["name"]))
    updated_at = now_iso
    comparable_existing = [
        normalize_existing_collection(collection, now_iso)
        for collection in existing.get("collections", [])
        if isinstance(collection, dict)
    ]
    comparable_existing = [collection for collection in comparable_existing if collection is not None]
    comparable_existing.sort(key=lambda item: (item["id"], item["name"]))
    if records == comparable_existing:
        updated_at = str(existing.get("updated_at") or now_iso)

    return {
        "schema_version": 1,
        "source": API_URL,
        "updated_at": updated_at,
        "count": len(records),
        "collections": records,
    }


def markdown_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def image_cell(name: str, url: str) -> str:
    escaped_name = html.escape(name, quote=True)
    escaped_url = html.escape(url, quote=True)
    return (
        f'<img src="{escaped_url}" alt="{escaped_name}" '
        f'width="{COVER_PREVIEW_WIDTH}">'
    )


def collection_row(collection: dict[str, Any]) -> str:
    collection_id_value = str(collection["id"])
    name = markdown_text(str(collection["name"]))
    cover = image_cell(
        str(collection["name"]),
        str(collection.get("preview_cover_url") or collection["cover_url"]),
    )
    status = markdown_text(str(collection.get("status") or ""))
    price = markdown_text(str(collection.get("price_label") or ""))
    pre_start = markdown_text(format_unix_shanghai(collection.get("pre_start_time")))
    start = markdown_text(format_unix_shanghai(collection.get("start_time")))
    end = "永久" if collection.get("effective_forever") else markdown_text(format_unix_shanghai(collection.get("end_time")))
    lottery_count = markdown_text(compact_int(collection.get("lottery_count")))
    item_count = markdown_text(compact_int(collection.get("item_total_count")))
    book_count = markdown_text(compact_int(collection.get("total_book_cnt")))
    buy_count = markdown_text(compact_int(collection.get("total_buy_cnt")))
    display_title = markdown_text(str(collection.get("display_title") or ""))
    return (
        f"| `{collection_id_value}` | **{name}** | {cover} | {status} | {price} | "
        f"{pre_start} | {start} | {end} | {lottery_count} | {item_count} | "
        f"{book_count} | {buy_count} | {display_title} |"
    )


def table_for_collections(collections: list[dict[str, Any]]) -> str:
    lines = [
        "| ID | 收藏集名称 | 封面图 | 状态 | 单抽价格 | 预约开始 | 开售时间 | 结束时间 | 卡池 | 卡牌 | 预约数 | 已售数 | 奖励 |",
        "| :---: | --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    lines.extend(collection_row(collection) for collection in collections)
    return "\n".join(lines)


def recent_collections(
    collections: list[dict[str, Any]], end_date: dt.date, recent_days: int
) -> dict[dt.date, list[dict[str, Any]]]:
    earliest = end_date - dt.timedelta(days=recent_days - 1)
    grouped: dict[dt.date, list[dict[str, Any]]] = {}

    for collection in collections:
        date = shanghai_date_from_unix(collection.get("pre_start_time"))
        if date is None or date < earliest or date > end_date:
            continue
        grouped.setdefault(date, []).append(collection)

    for items in grouped.values():
        items.sort(key=lambda item: (-int(item["id"]), item["name"]))

    return dict(sorted(grouped.items(), reverse=True))


def latest_complete_shanghai_date() -> dt.date:
    return dt.datetime.now(SHANGHAI_TZ).date() - dt.timedelta(days=1)


def build_generated_block(index: dict[str, Any], recent_days: int) -> str:
    collections = list(index["collections"])
    end_date = latest_complete_shanghai_date()
    recent = recent_collections(collections, end_date, recent_days)

    lines = [
        README_START,
        "<!-- 下面内容由 scripts/update_collection_index.py 自动生成，请勿手动编辑此区块。 -->",
        "",
        f"## **最近 {recent_days} 天预约开始收藏集（截至北京时间 {end_date.strftime('%Y/%m/%d')}）**",
        "",
    ]

    if recent:
        for date, items in recent.items():
            lines.extend(
                [
                    f"### **{date.strftime('%Y/%m/%d')}**",
                    "",
                    table_for_collections(items),
                    "",
                ]
            )
    else:
        lines.extend([f"暂无最近 {recent_days} 天预约开始的收藏集。", ""])

    lines.extend(
        [
            "---",
            "",
            "## **全部收藏集索引**",
            "",
            f"<details>\n<summary>展开全部 {len(collections)} 个收藏集封面</summary>\n",
            table_for_collections(collections),
            "",
            "</details>",
            "",
            README_END,
        ]
    )
    return "\n".join(lines)


def ensure_bold_title(readme: str) -> str:
    lines = readme.splitlines()
    if not lines:
        return "# **bilibili-collection**\n"

    if lines[0].strip() == "# bilibili-collection":
        lines[0] = "# **bilibili-collection**"

    return "\n".join(lines).rstrip() + "\n"


def update_readme(readme_path: Path, generated_block: str) -> None:
    existing = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
    existing = ensure_bold_title(existing)

    start = existing.find(README_START)
    end = existing.find(README_END)
    if start != -1 and end != -1 and start < end:
        end += len(README_END)
        updated = existing[:start].rstrip() + "\n\n" + generated_block + existing[end:]
    else:
        updated = existing.rstrip() + "\n\n" + generated_block + "\n"

    readme_path.write_text(updated.rstrip() + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    now = utc_now()

    existing = load_existing(args.data)
    if args.offline:
        index = normalize_index(existing, now)
    else:
        fetched = fetch_collections(args.timeout)
        index = merge_collections(fetched, existing, now, args.timeout)

    args.data.parent.mkdir(parents=True, exist_ok=True)
    args.data.write_text(
        json.dumps(index, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    update_readme(args.readme, build_generated_block(index, args.recent_days))

    print(f"Updated {args.data} and {args.readme} with {index['count']} collections")
    return 0


if __name__ == "__main__":
    sys.exit(main())
