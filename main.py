"""
bilibili 收藏集列表 API: https://socialsisteryi.github.io/bilibili-API-collect/docs/garb/lottery.html#%E6%94%B6%E8%97%8F%E9%9B%86%E5%88%97%E8%A1%A8api
bilibili 收藏集信息 API: https://socialsisteryi.github.io/bilibili-API-collect/docs/garb/lottery.html#%E6%94%B6%E8%97%8F%E9%9B%86%E4%BF%A1%E6%81%AFapi

- 收藏集链接：https://www.bilibili.com/h5/mall/digital-card/home?from_id=&act_id=101221
"""

#!爬取 b 站所有收藏集并下载

import logging
import os
import time

import aiofiles
from aiofiles import os as aioos
from aiofiles import tempfile as aiotempfile
import orjson
from spdl.pipeline import PipelineBuilder
from waifuboard import Booru
from waifuboard.utils import normalize_filepath

client = Booru(
    logger_level=logging.WARNING,
    base_url=(base_url := (referer := "https://www.bilibili.com")),
    multiplexed=False,
    proxies=None,
    trust_env=False,
    max_attempt_number=3,
    retries=3,
    rate_limit=None,
    timeout=60.0 * 5,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 要提取该仓库下某一个目录的文件路径
DEST_DIR = "./bilibili-collection"
IMAGE_PATH = f"{DEST_DIR}/images"
VIDEO_PATH = f"{DEST_DIR}/videos"
JSON_PATH = f"{DEST_DIR}/jsons"
for d in [IMAGE_PATH, VIDEO_PATH, JSON_PATH]:
    os.makedirs(d, exist_ok=True)


async def card_index():
    site = 0
    url = "https://api.bilibili.com/x/vas/dlc_act/act/list"  # 收藏集列表 API

    params = {
        "csrf": "",  # 用户 csrf，不必要
        "scene": 1,  # 不必要，作用尚不明确，默认为 1，不填则获取到空数据
        "site": site,  # 位置，不必要，不填为 0，但建议填上，会影响到后面的 json 数据
    }

    while True:
        response = await client.get(
            url=url,
            params=params,
            referer=referer,
        )

        json_data: dict = response.json()["data"]
        data_list: list[dict] = json_data["list"]

        logger.info(f"[{site = }] Fetched {len(data_list)} items")

        for data in data_list:
            act_id = data["act_id"]  # 收藏集活动 id
            act_name = normalize_filepath(data["act_name"])  # 收藏集名称

            json_path = f"{JSON_PATH}/{act_id}_{act_name}.json"

            if await aioos.path.exists(json_path):
                continue

            yield act_id, act_name, data, json_path

        is_more, site = json_data["is_more"], json_data["site"]

        if not is_more:
            break

        params["site"] = site


async def card_detail(t: tuple[int, str, dict, str]):
    act_id, act_name, data, json_path = t

    url = "https://api.bilibili.com/x/vas/dlc_act/asset_bag"  # 收藏集信息 API（详尽版，包含已结束的卡池内容）
    params = {
        "act_id": act_id,  # 收藏集活动 id，必要
        # "lottery_id": 0,  # 收藏集抽奖 id，不必要，若缺失该参数则默认填充为 0，表示返回全部卡池，也可以为 lottery_simple_list 中的任意一个 lottery_id（除了 0），以获取更精细的某一个卡池
    }

    response = await client.get(
        url=url,
        params=params,
        referer=referer,
    )

    json_data: dict = response.json()["data"]

    async with aiofiles.open(json_path, "wb") as f:
        await f.write(orjson.dumps(data | json_data))
    logger.info(f"[{act_id = }] Saved {act_name} metadata to {json_path}")

    #!封面图
    await aioos.makedirs(
        os.path.join(IMAGE_PATH, f"{act_id}_{act_name}", "cover"),
        exist_ok=True,
    )

    cover_img_url: str | None = json_data.get("act_y_img")
    if cover_img_url:
        cover_img_name = act_name + os.path.splitext(cover_img_url)[-1]
        cover_img_path = os.path.join(
            IMAGE_PATH, f"{act_id}_{act_name}", "cover", cover_img_name
        )
        yield act_id, act_name, cover_img_url, cover_img_path

    #!收藏集
    await aioos.makedirs(
        os.path.join(IMAGE_PATH, f"{act_id}_{act_name}", "collection"),
        exist_ok=True,
    )
    await aioos.makedirs(
        os.path.join(VIDEO_PATH, f"{act_id}_{act_name}", "collection"),
        exist_ok=True,
    )

    item_list: list[dict] = json_data["item_list"]
    for item in item_list:
        #!收藏卡
        card_item: dict = item["card_item"]

        # 名称
        card_name = normalize_filepath(card_item["card_name"])

        # 图片链接
        img_url: str | None = card_item.get("card_img")
        if img_url:
            img_name = card_name + os.path.splitext(img_url)[-1]
            img_path = os.path.join(
                IMAGE_PATH, f"{act_id}_{act_name}", "collection", img_name
            )
            yield act_id, act_name, img_url, img_path

        # 视频链接
        video_list: list | None = card_item.get("video_list")
        if video_list:
            video_url = video_list[-1]
            video_name = card_name + ".mp4"
            video_path = os.path.join(
                VIDEO_PATH, f"{act_id}_{act_name}", "collection", video_name
            )
            yield act_id, act_name, video_url, video_path

    #!兑换物列表
    await aioos.makedirs(
        os.path.join(IMAGE_PATH, f"{act_id}_{act_name}", "curation"),
        exist_ok=True,
    )
    await aioos.makedirs(
        os.path.join(VIDEO_PATH, f"{act_id}_{act_name}", "curation"),
        exist_ok=True,
    )

    collect_list: list[dict] = json_data["collect_list"]
    for collect in collect_list:
        #!仅提取典藏卡，不提取徽章、头像框、表情包等其他兑换物
        if collect["redeem_item_type"] != 1:
            continue

        #!典藏卡
        card_item = collect["card_item"]["card_asset_info"]["card_item"]

        # 名称
        card_name = normalize_filepath(card_item["card_name"])

        # 图片链接
        img_url: str | None = card_item.get("card_img")
        if img_url:
            img_name = card_name + os.path.splitext(img_url)[-1]
            img_path = os.path.join(
                IMAGE_PATH, f"{act_id}_{act_name}", "curation", img_name
            )
            yield act_id, act_name, img_url, img_path

        # 视频链接
        video_list: list | None = card_item.get("video_list")
        if video_list:
            video_url = video_list[-1]
            video_name = card_name + ".mp4"
            video_path = os.path.join(
                VIDEO_PATH, f"{act_id}_{act_name}", "curation", video_name
            )
            yield act_id, act_name, video_url, video_path


async def save_media(t: tuple[int, str, str, str]):
    act_id, act_name, url, media_path = t

    try:
        response = await client.get(
            url,
            referer=referer,
        )

        async with aiofiles.open(media_path, mode="wb") as f:
            await f.write(response.content)

        logger.info(f"[{act_id = }] Saved {act_name} {url} to {media_path}")
        yield True

    except Exception as exc:
        logger.error(
            f"[{act_id = }] Failed to save {act_name} {url} to {media_path}, because the following exception was raised: {exc.__class__.__name__}: {exc}",
        )
        yield False


if __name__ == "__main__":
    pipeline = (
        PipelineBuilder()
        .add_source(card_index())
        .pipe(card_detail, concurrency=16)
        .pipe(save_media, concurrency=16)
        .add_sink(1)
        .build(num_threads=8)
    )

    start = time.perf_counter()
    succeed_counts, failure_counts = 0, 0

    for flag in pipeline:
        if flag:
            succeed_counts += 1
        else:
            failure_counts += 1

    end = time.perf_counter()
    logger.info(
        f"Time taken: {end - start} seconds, {succeed_counts = }, {failure_counts = }"
    )
