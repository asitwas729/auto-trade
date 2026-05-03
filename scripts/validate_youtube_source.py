"""
삼프로TV 평일 오전 라이브 VOD 자막 ↔ Naver RSS 섹터 유사도 검증 스크립트

특징:
  - 라이브 송출 시각 기준 KST 06:05~07:25 구간만 잘라내어 분석 (자막 타임스탬프 활용)
  - 평일 오전 라이브 VOD만 필터링 (요일 + liveStreamingDetails.actualStartTime)
  - SECTOR_KEYWORDS 매칭 → 섹터 점수 → Naver RSS 점수와 코사인 유사도/상위3 일치율 비교

사용법:
    python scripts/validate_youtube_source.py --days 30
    python scripts/validate_youtube_source.py --days 60 --time-start 06:05 --time-end 07:25
    python scripts/validate_youtube_source.py --days 30 --no-weekday-only --no-require-live

동작 순서:
  1. YouTube Data API search.list 로 채널의 최근 N일 영상 후보 조회
  2. videos.list 로 liveStreamingDetails / contentDetails 일괄 조회 (배치 50개)
  3. 평일 + 라이브(actualStartTime 보유) 영상만 필터
  4. youtube-transcript-api 로 한국어 자막을 타임스탬프 포함 추출
  5. 자막 segment.start 를 actualStartTime(KST) 에 더해 송출 시각으로 환산
  6. 06:05~07:25 KST 구간 segment 만 추려 텍스트 결합
  7. SECTOR_KEYWORDS 로 youtube_sectors 산출
  8. data/news_cache_YYYYMMDD.json 로 naver_sectors 산출
  9. 코사인 유사도 / 상위3 일치율 계산 후 data/youtube_vs_naver_YYYYMMDD.json 저장

판단 기준 (Naver RSS 비교):
  - 평균 상위3 일치율 >= 60% → YouTube 1순위 소스 추가 고려
  - 40~60%             → 2순위 보조 소스로 활용 가능
  - < 40%              → Naver RSS 단독 유지

필요 패키지:
    pip install youtube-transcript-api google-api-python-client
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import SECTOR_KEYWORDS
from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DATA_DIR = settings.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ─── YouTube Data API ────────────────────────────────────────────

def _build_youtube(api_key: str):
    try:
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:
        raise SystemExit("google-api-python-client 필요: pip install google-api-python-client") from exc
    return build("youtube", "v3", developerKey=api_key)


def fetch_channel_video_ids(youtube, channel_id: str, days: int) -> list[dict]:
    """search.list 로 최근 days 일 영상 ID/snippet 후보 반환."""
    published_after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    videos: list[dict] = []
    next_page = None
    while True:
        resp = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            type="video",
            publishedAfter=published_after,
            maxResults=50,
            order="date",
            pageToken=next_page,
        ).execute()
        for item in resp.get("items", []):
            videos.append({
                "video_id": item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "published_at": item["snippet"]["publishedAt"],
            })
        next_page = resp.get("nextPageToken")
        if not next_page:
            break

    logger.info("YouTube search.list: %d개 (최근 %d일)", len(videos), days)
    return videos


def enrich_with_live_details(youtube, videos: list[dict]) -> list[dict]:
    """videos.list 로 liveStreamingDetails / contentDetails 채워넣기 (배치 50개)."""
    if not videos:
        return videos

    by_id = {v["video_id"]: v for v in videos}
    ids = list(by_id.keys())

    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        resp = youtube.videos().list(
            part="liveStreamingDetails,contentDetails,snippet",
            id=",".join(chunk),
            maxResults=50,
        ).execute()
        for item in resp.get("items", []):
            vid = item["id"]
            target = by_id.get(vid)
            if not target:
                continue
            live = item.get("liveStreamingDetails") or {}
            target["actual_start_time"] = live.get("actualStartTime")  # ISO UTC
            target["actual_end_time"] = live.get("actualEndTime")
            target["duration_iso"] = (item.get("contentDetails") or {}).get("duration")

    return list(by_id.values())


def filter_morning_live(
    videos: list[dict],
    weekday_only: bool,
    require_live: bool,
    expected_hour_window: tuple[int, int] = (4, 8),
) -> list[dict]:
    """라이브 시작시각이 KST 평일 새벽 시간대인 영상만 추림."""
    keep = []
    for v in videos:
        ast = v.get("actual_start_time")
        if not ast:
            if require_live:
                continue
            # 라이브가 아니면 publishedAt 으로 폴백
            ast = v.get("published_at")
            if not ast:
                continue
        try:
            dt_utc = datetime.strptime(ast, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                dt_utc = datetime.strptime(ast, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        dt_kst = dt_utc.astimezone(KST)

        if weekday_only and dt_kst.weekday() >= 5:
            continue
        lo, hi = expected_hour_window
        if not (lo <= dt_kst.hour < hi):
            continue

        v["broadcast_start_kst"] = dt_kst.isoformat()
        v["broadcast_date_kst"] = dt_kst.strftime("%Y-%m-%d")
        keep.append(v)

    logger.info(
        "필터 후 영상: %d개 (평일=%s, 라이브필수=%s, 시간창=%s)",
        len(keep), weekday_only, require_live, expected_hour_window,
    )
    return keep


# ─── 자막 (타임스탬프 보존) ──────────────────────────────────────

_TRANSCRIPT_CACHE_DIR = DATA_DIR / "transcripts"
_TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_transcript_segments(video_id: str) -> list[dict]:
    """
    한국어 자막을 타임스탬프 포함하여 반환. 한 번 받은 자막은
    data/transcripts/<video_id>.json 으로 캐시하여 IP 차단(429) 회피.

    youtube-transcript-api 1.x (인스턴스 메서드 fetch/list) 와
    구버전 0.x (클래스 메서드 get_transcript/list_transcripts) 양쪽 지원.
    """
    # 캐시 우선
    cache_path = _TRANSCRIPT_CACHE_DIR / f"{video_id}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        raise SystemExit("youtube-transcript-api 필요: pip install youtube-transcript-api")

    def _normalize(raw) -> list[dict]:
        out = []
        for s in raw:
            if isinstance(s, dict):
                out.append({
                    "text": s.get("text", ""),
                    "start": float(s.get("start", 0.0)),
                    "duration": float(s.get("duration", 0.0)),
                })
            else:
                out.append({
                    "text": getattr(s, "text", ""),
                    "start": float(getattr(s, "start", 0.0)),
                    "duration": float(getattr(s, "duration", 0.0)),
                })
        return out

    def _save_cache(segs: list[dict]) -> None:
        try:
            cache_path.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # 1.x 신 API (인스턴스 메서드)
    if hasattr(YouTubeTranscriptApi, "fetch") and not hasattr(YouTubeTranscriptApi, "get_transcript"):
        api = YouTubeTranscriptApi()
        try:
            fetched = api.fetch(video_id, languages=["ko", "ko-KR"])
            segs = _normalize(fetched)
            _save_cache(segs)
            return segs
        except Exception as exc:
            logger.debug("자막 오류 1.x (video_id=%s): %s", video_id, exc)
            return []

    # 0.x 구 API (클래스 메서드)
    for lang in (["ko"], ["ko-KR"], None):
        try:
            if lang is not None:
                raw = YouTubeTranscriptApi.get_transcript(video_id, languages=lang)  # type: ignore[attr-defined]
            else:
                tlist = YouTubeTranscriptApi.list_transcripts(video_id)  # type: ignore[attr-defined]
                raw = tlist.find_transcript(["ko", "ko-KR"]).fetch()
            segs = _normalize(raw)
            _save_cache(segs)
            return segs
        except Exception as exc:
            logger.debug("자막 오류 0.x (video_id=%s, lang=%s): %s", video_id, lang, exc)
            continue
    return []


def filter_segments_by_clock(
    segments: list[dict],
    broadcast_start_kst: datetime,
    window_start: time,
    window_end: time,
) -> list[dict]:
    """
    각 segment 를 송출 시각(KST)으로 환산해 [window_start, window_end] 구간만 남김.
    window 가 자정을 넘기면 동작 보장 안 함 (오전 시간대 가정).
    """
    base_date = broadcast_start_kst.date()
    win_lo = datetime.combine(base_date, window_start, tzinfo=KST)
    win_hi = datetime.combine(base_date, window_end, tzinfo=KST)

    kept = []
    for seg in segments:
        seg_start = broadcast_start_kst + timedelta(seconds=seg["start"])
        seg_end = seg_start + timedelta(seconds=seg["duration"])
        # segment 의 어느 부분이라도 윈도우와 겹치면 포함
        if seg_end < win_lo or seg_start > win_hi:
            continue
        kept.append(seg)
    return kept


# ─── 섹터 점수 ───────────────────────────────────────────────────

def score_sectors_from_text(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        if not keywords:
            scores[sector] = 0.0
            continue
        hit = sum(1 for kw in keywords if kw in text)
        scores[sector] = min(hit / len(keywords), 1.0)
    return scores


def top_n_sectors(scores: dict[str, float], n: int = 3) -> list[str]:
    return sorted(scores, key=lambda k: scores.get(k, 0.0), reverse=True)[:n]


def load_volume_scores(date_str_yyyymmdd: str) -> dict[str, float]:
    """
    data/sector_volume_YYYYMMDD.json 로드 → max 정규화로 0~1 점수.
    """
    path = DATA_DIR / f"sector_volume_{date_str_yyyymmdd}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not raw:
        return {}
    max_v = max(raw.values()) or 1
    # SECTOR_KEYWORDS 의 키들도 모두 0 으로 채워 길이 정합
    scores = {sec: 0.0 for sec in SECTOR_KEYWORDS}
    for sec, val in raw.items():
        scores[sec] = float(val) / float(max_v)
    return scores


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    keys = list(SECTOR_KEYWORDS.keys())
    try:
        import numpy as np
        va = np.array([a.get(k, 0.0) for k in keys])
        vb = np.array([b.get(k, 0.0) for k in keys])
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        if denom < 1e-9:
            return 0.0
        return float(np.dot(va, vb) / denom)
    except ImportError:
        dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
        na = sum(v * v for v in a.values()) ** 0.5
        nb = sum(v * v for v in b.values()) ** 0.5
        return dot / (na * nb) if na * nb > 1e-9 else 0.0


def top3_match_rate(a: dict[str, float], b: dict[str, float]) -> float:
    return len(set(top_n_sectors(a, 3)) & set(top_n_sectors(b, 3))) / 3.0


# ─── 메인 ─────────────────────────────────────────────────────────

def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def main() -> None:
    parser = argparse.ArgumentParser(description="삼프로TV 오전 라이브 VOD vs Naver RSS 섹터 유사도 검증")
    parser.add_argument("--days", type=int, default=30, help="검증할 과거 일수 (기본 30일)")
    parser.add_argument("--channel-id", type=str, default=settings.YOUTUBE_CHANNEL_ID,
                        help="YouTube 채널 ID")
    parser.add_argument("--time-start", type=str, default="06:05",
                        help="구간 시작 시각 KST HH:MM (기본 06:05)")
    parser.add_argument("--time-end", type=str, default="07:25",
                        help="구간 종료 시각 KST HH:MM (기본 07:25)")
    parser.add_argument("--weekday-only", dest="weekday_only", action="store_true", default=True,
                        help="평일만 (기본 ON)")
    parser.add_argument("--no-weekday-only", dest="weekday_only", action="store_false")
    parser.add_argument("--require-live", dest="require_live", action="store_true", default=True,
                        help="라이브 VOD만 (기본 ON)")
    parser.add_argument("--no-require-live", dest="require_live", action="store_false")
    parser.add_argument("--start-hour-lo", type=int, default=4,
                        help="라이브 시작 허용 시간대 하한 KST (기본 4)")
    parser.add_argument("--start-hour-hi", type=int, default=8,
                        help="라이브 시작 허용 시간대 상한 KST, 미만 (기본 8)")
    args = parser.parse_args()

    api_key = settings.YOUTUBE_API_KEY
    channel_id = args.channel_id
    if not api_key:
        logger.error(".env 에 YOUTUBE_API_KEY 설정 필요")
        sys.exit(1)
    if not channel_id:
        logger.error(".env 에 YOUTUBE_CHANNEL_ID 설정 필요 (또는 --channel-id 인자 사용)")
        sys.exit(1)

    win_start = _parse_hhmm(args.time_start)
    win_end = _parse_hhmm(args.time_end)
    if win_end <= win_start:
        logger.error("--time-end 가 --time-start 보다 커야 함")
        sys.exit(1)

    youtube = _build_youtube(api_key)

    # 1) 영상 후보 조회
    videos = fetch_channel_video_ids(youtube, channel_id, args.days)
    if not videos:
        logger.error("VOD 목록 없음")
        sys.exit(1)

    # 2) 라이브 메타 보강
    videos = enrich_with_live_details(youtube, videos)

    # 3) 평일 + 오전 라이브 필터
    videos = filter_morning_live(
        videos,
        weekday_only=args.weekday_only,
        require_live=args.require_live,
        expected_hour_window=(args.start_hour_lo, args.start_hour_hi),
    )
    if not videos:
        logger.error("필터 통과 영상 없음")
        sys.exit(1)

    results = []
    # 3쌍 비교: YT-Naver, YT-Volume, Naver-Volume
    pair_keys = ("yt_naver", "yt_vol", "naver_vol")
    cos_acc: dict[str, list[float]] = {k: [] for k in pair_keys}
    match_acc: dict[str, list[float]] = {k: [] for k in pair_keys}
    no_caption = 0
    no_naver_cache = 0
    no_volume = 0
    empty_window = 0

    for v in videos:
        video_id = v["video_id"]
        date_str = v["broadcast_date_kst"]
        title = v["title"]
        broadcast_start = datetime.fromisoformat(v["broadcast_start_kst"])

        logger.info("[%s] %s (%s) 처리...", date_str, title[:40], video_id)

        # 4) 자막 (타임스탬프 보존)
        segments = get_transcript_segments(video_id)
        if not segments:
            logger.warning("  자막 없음 → 스킵")
            no_caption += 1
            continue

        # 5,6) 송출시각 환산 후 윈도우 슬라이싱
        windowed = filter_segments_by_clock(segments, broadcast_start, win_start, win_end)
        if not windowed:
            logger.warning(
                "  %s~%s 구간 자막 없음 (전체 %d segments) → 스킵",
                args.time_start, args.time_end, len(segments),
            )
            empty_window += 1
            continue

        text = " ".join(seg["text"] for seg in windowed)

        # 7) 섹터 점수
        yt_scores = score_sectors_from_text(text)

        # 8) Naver RSS 캐시
        cache_file = DATA_DIR / f"news_cache_{date_str.replace('-', '')}.json"
        if not cache_file.exists():
            logger.warning("  Naver 캐시 없음: %s → 스킵", cache_file.name)
            no_naver_cache += 1
            continue
        try:
            raw_news = json.loads(cache_file.read_text(encoding="utf-8"))
            naver_text = " ".join(
                f"{item.get('title', '')} {item.get('summary', '')}" for item in raw_news
            )
            naver_scores = score_sectors_from_text(naver_text)
        except Exception as exc:
            logger.warning("  Naver 캐시 로드 실패: %s → 스킵", exc)
            no_naver_cache += 1
            continue

        # 9) 거래대금 점수 (실제 시장)
        date_yyyymmdd = date_str.replace("-", "")
        vol_scores = load_volume_scores(date_yyyymmdd)
        if not vol_scores:
            logger.warning("  거래대금 캐시 없음: sector_volume_%s.json", date_yyyymmdd)
            no_volume += 1
            # 거래대금 없어도 YT-Naver 비교는 계속

        # 10) 3쌍 유사도
        c_yn = cosine_similarity(yt_scores, naver_scores)
        m_yn = top3_match_rate(yt_scores, naver_scores)
        cos_acc["yt_naver"].append(c_yn)
        match_acc["yt_naver"].append(m_yn)

        c_yv = m_yv = c_nv = m_nv = None
        if vol_scores:
            c_yv = cosine_similarity(yt_scores, vol_scores)
            m_yv = top3_match_rate(yt_scores, vol_scores)
            c_nv = cosine_similarity(naver_scores, vol_scores)
            m_nv = top3_match_rate(naver_scores, vol_scores)
            cos_acc["yt_vol"].append(c_yv)
            match_acc["yt_vol"].append(m_yv)
            cos_acc["naver_vol"].append(c_nv)
            match_acc["naver_vol"].append(m_nv)

        entry = {
            "date": date_str,
            "video_id": video_id,
            "title": title,
            "broadcast_start_kst": v["broadcast_start_kst"],
            "window": f"{args.time_start}-{args.time_end} KST",
            "windowed_segment_count": len(windowed),
            "total_segment_count": len(segments),
            "yt_top3": top_n_sectors(yt_scores, 3),
            "naver_top3": top_n_sectors(naver_scores, 3),
            "volume_top3": top_n_sectors(vol_scores, 3) if vol_scores else None,
            "yt_vs_naver": {"cosine": round(c_yn, 4), "match": round(m_yn, 4)},
            "yt_vs_volume": {"cosine": round(c_yv, 4), "match": round(m_yv, 4)} if vol_scores else None,
            "naver_vs_volume": {"cosine": round(c_nv, 4), "match": round(m_nv, 4)} if vol_scores else None,
            "yt_scores": {k: round(val, 3) for k, val in yt_scores.items()},
            "naver_scores": {k: round(val, 3) for k, val in naver_scores.items()},
            "vol_scores": {k: round(val, 3) for k, val in vol_scores.items()} if vol_scores else None,
        }
        results.append(entry)
        if vol_scores:
            logger.info(
                "  YT %s | Naver %s | Vol %s | YT-Nv cos=%.2f m=%.0f%% | YT-Vl cos=%.2f m=%.0f%% | Nv-Vl cos=%.2f m=%.0f%%",
                entry["yt_top3"], entry["naver_top3"], entry["volume_top3"],
                c_yn, m_yn * 100, c_yv, m_yv * 100, c_nv, m_nv * 100,
            )
        else:
            logger.info(
                "  YT %s | Naver %s | (Vol 없음) | YT-Nv cos=%.2f match=%.0f%%",
                entry["yt_top3"], entry["naver_top3"], c_yn, m_yn * 100,
            )

    # ─── 결과 ─────────────────────────────────────────────────────
    if not results:
        logger.warning(
            "유효한 비교 데이터 없음 (자막없음=%d, 윈도우비어있음=%d, Naver캐시없음=%d, 거래대금없음=%d)",
            no_caption, empty_window, no_naver_cache, no_volume,
        )
        return

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    pair_avgs = {
        k: {"cosine": _avg(cos_acc[k]), "match": _avg(match_acc[k]), "n": len(cos_acc[k])}
        for k in pair_keys
    }

    print("\n" + "=" * 70)
    print(f"3자 비교 결과 요약 ({len(results)}일치, {args.time_start}~{args.time_end} KST)")
    print("=" * 70)
    pair_label = {
        "yt_naver":  "삼프로 ↔ Naver RSS  ",
        "yt_vol":    "삼프로 ↔ 실제거래대금",
        "naver_vol": "Naver  ↔ 실제거래대금",
    }
    print(f"  {'쌍':<22s} {'n':>3s}  {'평균 코사인':>10s}  {'평균 상위3 일치':>14s}")
    for k in pair_keys:
        a = pair_avgs[k]
        print(f"  {pair_label[k]:<22s} {a['n']:>3d}  {a['cosine']:>10.4f}  {a['match']*100:>13.1f}%")
    print(f"\n  스킵: 자막없음={no_caption}, 윈도우비어있음={empty_window}, "
          f"Naver캐시없음={no_naver_cache}, 거래대금없음={no_volume}")

    # 어느 소스가 실제 거래대금에 더 가까운지 판단
    if pair_avgs["yt_vol"]["n"] > 0 and pair_avgs["naver_vol"]["n"] > 0:
        yt_v = pair_avgs["yt_vol"]
        nv_v = pair_avgs["naver_vol"]
        print()
        if yt_v["match"] > nv_v["match"]:
            print(f"  [권고] 삼프로TV 가 실제 시장(거래대금) 적중률이 더 높음 "
                  f"({yt_v['match']*100:.1f}% vs {nv_v['match']*100:.1f}%)")
        elif nv_v["match"] > yt_v["match"]:
            print(f"  [권고] Naver RSS 가 실제 시장(거래대금) 적중률이 더 높음 "
                  f"({nv_v['match']*100:.1f}% vs {yt_v['match']*100:.1f}%)")
        else:
            print(f"  [동률] 두 소스의 실제 시장 적중률이 같음 ({yt_v['match']*100:.1f}%)")
    print("=" * 70)

    out_path = DATA_DIR / f"youtube_vs_naver_{datetime.now().strftime('%Y%m%d')}.json"
    summary = {
        "generated_at": datetime.now().isoformat(),
        "days": args.days,
        "channel_id": channel_id,
        "window_kst": f"{args.time_start}-{args.time_end}",
        "weekday_only": args.weekday_only,
        "require_live": args.require_live,
        "total_compared": len(results),
        "pair_averages": {
            k: {"avg_cosine": round(pair_avgs[k]["cosine"], 4),
                "avg_top3_match_rate": round(pair_avgs[k]["match"], 4),
                "n": pair_avgs[k]["n"]}
            for k in pair_keys
        },
        "skipped": {
            "no_caption": no_caption,
            "empty_window": empty_window,
            "no_naver_cache": no_naver_cache,
            "no_volume": no_volume,
        },
        "results": results,
    }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("결과 저장: %s", out_path)


if __name__ == "__main__":
    main()
