"""
지정 일자 목록에 대해 삼프로TV 채널의 평일 오전 라이브 VOD 메타데이터를 수집해
data/youtube_video_index.json 으로 인덱싱.

저장 형식:
    {
        "2024-04-09": {"video_id": "...", "title": "...", "broadcast_start_kst": "..."},
        ...
    }

자막은 별도 스크립트(fetch_transcripts.py)에서 수집.

사용법:
    python scripts/build_video_index.py --sample-file data/random_sample_dates.json
    python scripts/build_video_index.py --dates 2026-04-30,2026-04-29,...
    python scripts/build_video_index.py --recent 14
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DATA_DIR = settings.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = DATA_DIR / "youtube_video_index.json"


def _build_yt(api_key: str):
    from googleapiclient.discovery import build  # type: ignore
    return build("youtube", "v3", developerKey=api_key)


def search_videos_in_window(yt, channel_id: str, after_iso: str, before_iso: str) -> list[dict]:
    """publishedAfter / publishedBefore 윈도우로 search.list."""
    videos = []
    next_page = None
    while True:
        resp = yt.search().list(
            part="snippet",
            channelId=channel_id,
            type="video",
            publishedAfter=after_iso,
            publishedBefore=before_iso,
            maxResults=50,
            order="date",
            pageToken=next_page,
        ).execute()
        for it in resp.get("items", []):
            videos.append({
                "video_id": it["id"]["videoId"],
                "title": it["snippet"]["title"],
                "published_at": it["snippet"]["publishedAt"],
            })
        next_page = resp.get("nextPageToken")
        if not next_page:
            break
    return videos


def enrich(yt, videos: list[dict]) -> list[dict]:
    if not videos:
        return videos
    by_id = {v["video_id"]: v for v in videos}
    ids = list(by_id.keys())
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        resp = yt.videos().list(
            part="liveStreamingDetails,contentDetails,snippet",
            id=",".join(chunk),
            maxResults=50,
        ).execute()
        for it in resp.get("items", []):
            target = by_id[it["id"]]
            live = it.get("liveStreamingDetails") or {}
            target["actual_start_time"] = live.get("actualStartTime")
            target["duration_iso"] = (it.get("contentDetails") or {}).get("duration")
    return list(by_id.values())


def parse_iso_utc(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def pick_morning_live(videos: list[dict], target_date) -> dict | None:
    """target_date(KST date) 의 평일 오전 라이브 후보 중 가장 적합한 것."""
    if target_date.weekday() >= 5:
        return None
    candidates = []
    for v in videos:
        ast = v.get("actual_start_time")
        dt_utc = parse_iso_utc(ast) if ast else parse_iso_utc(v.get("published_at"))
        if not dt_utc:
            continue
        dt_kst = dt_utc.astimezone(KST)
        if dt_kst.date() != target_date:
            continue
        if not (4 <= dt_kst.hour < 8):
            continue
        v["broadcast_start_kst"] = dt_kst.isoformat()
        candidates.append(v)
    if not candidates:
        return None
    # actualStartTime 보유 우선, 그 다음 가장 이른 시각
    candidates.sort(key=lambda v: (v.get("actual_start_time") is None, v["broadcast_start_kst"]))
    return candidates[0]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sample-file", type=str)
    p.add_argument("--dates", type=str)
    p.add_argument("--recent", type=int, help="최근 N영업일")
    p.add_argument("--channel-id", default=settings.YOUTUBE_CHANNEL_ID)
    args = p.parse_args()

    if not settings.YOUTUBE_API_KEY or not args.channel_id:
        logger.error(".env YOUTUBE_API_KEY/YOUTUBE_CHANNEL_ID 필요")
        sys.exit(1)

    today = datetime.now(KST).date()
    target_dates: list = []
    if args.sample_file:
        sf = json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
        target_dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in sf["dates"]]
    elif args.dates:
        target_dates = [datetime.strptime(d.strip(), "%Y-%m-%d").date() for d in args.dates.split(",") if d.strip()]
    elif args.recent:
        end = today - timedelta(days=1)
        out = []
        cur = end
        while len(out) < args.recent and cur >= end - timedelta(days=args.recent * 3):
            if cur.weekday() < 5:
                out.append(cur)
            cur -= timedelta(days=1)
        target_dates = sorted(out)
    else:
        logger.error("--sample-file / --dates / --recent 중 하나 필요")
        sys.exit(1)

    yt = _build_yt(settings.YOUTUBE_API_KEY)

    # 기존 인덱스 로드
    idx: dict[str, dict] = {}
    if INDEX_PATH.exists():
        try:
            idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            idx = {}

    new_count = 0
    skip_count = 0
    notfound = []
    api_unit_used = 0

    # 효율: target_dates 를 (year, month) 버킷으로 그룹화, 월별 1회 search.list
    by_month: dict[tuple[int, int], list] = {}
    for target in target_dates:
        ds = target.strftime("%Y-%m-%d")
        if ds in idx and idx[ds].get("video_id"):
            skip_count += 1
            continue
        by_month.setdefault((target.year, target.month), []).append(target)

    logger.info("월 단위 그룹: %d개월", len(by_month))

    # 월별 검색 결과 캐시 (재실행 시 quota 절약)
    month_cache_dir = DATA_DIR / "youtube_month_cache"
    month_cache_dir.mkdir(parents=True, exist_ok=True)

    for (yr, mo), targets in sorted(by_month.items()):
        cache_file = month_cache_dir / f"{yr}-{mo:02d}.json"
        if cache_file.exists():
            try:
                videos = json.loads(cache_file.read_text(encoding="utf-8"))
                logger.info("[%d-%02d] 월 캐시 사용 (%d 영상)", yr, mo, len(videos))
            except Exception:
                videos = []
        else:
            videos = []

        if not videos:
            # KST 해당 월 1일 04:00 ~ 다음달 1일 09:00 (월말 영상 누락 방지)
            from datetime import date as _date
            first = _date(yr, mo, 1)
            if mo == 12:
                next_first = _date(yr + 1, 1, 1)
            else:
                next_first = _date(yr, mo + 1, 1)
            kst_lo = datetime.combine(first, datetime.min.time(), tzinfo=KST).replace(hour=4)
            kst_hi = datetime.combine(next_first, datetime.min.time(), tzinfo=KST).replace(hour=9)
            utc_lo = kst_lo.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            utc_hi = kst_hi.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                videos = search_videos_in_window(yt, args.channel_id, utc_lo, utc_hi)
                api_unit_used += 100  # 한 페이지 search.list ≈ 100 unit (페이지네이션 시 더)
                # 실제 페이지수만큼 100 unit 씩 — search 함수 내부에서 페이지네이션
                # 여기서는 보수적으로 4 페이지 가정 → 보고용 보정은 생략
                if videos:
                    videos = enrich(yt, videos)
                    api_unit_used += 1
                cache_file.write_text(json.dumps(videos, ensure_ascii=False), encoding="utf-8")
                logger.info("[%d-%02d] search.list → %d 영상 (캐시 저장)", yr, mo, len(videos))
            except Exception as exc:
                logger.warning("[%d-%02d] search 실패: %s", yr, mo, exc)
                continue

        # 각 target_date 매칭
        for target in sorted(targets):
            ds = target.strftime("%Y-%m-%d")
            chosen = pick_morning_live(videos, target)
            if chosen:
                idx[ds] = {
                    "video_id": chosen["video_id"],
                    "title": chosen["title"],
                    "broadcast_start_kst": chosen["broadcast_start_kst"],
                    "duration_iso": chosen.get("duration_iso", ""),
                    "is_live": chosen.get("actual_start_time") is not None,
                }
                new_count += 1
                logger.info("  [%s] %s → %s", ds, chosen["video_id"], chosen["title"][:60])
            else:
                notfound.append(ds)
                logger.warning("  [%s] 적합 라이브 VOD 없음", ds)

        # 매월 끝마다 인덱스 저장
        INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"인덱스 갱신 완료: 신규 {new_count}, 기존스킵 {skip_count}, 미발견 {len(notfound)}")
    print(f"YouTube API quota 사용 ≈ {api_unit_used} units")
    if notfound:
        print(f"미발견 일자: {', '.join(notfound[:10])}{'...' if len(notfound) > 10 else ''}")
    print("=" * 60)


if __name__ == "__main__":
    main()
