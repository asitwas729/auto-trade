"""
youtube_video_index.json 의 자막을 천천히 받기 + IP 차단 시 자동 대기.

기본 사이클:
  - 영상당 자막 수집 (8±2초 간격)
  - IpBlocked 발생 → wait_minutes 만큼 대기 → 재시도
  - max_block_count 회 연속 차단 → 종료 (다음 실행에서 재개)

사용법:
    python scripts/fetch_transcripts_with_wait.py --wait-minutes 30 --max-blocks 3
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = settings.DATA_DIR
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = DATA_DIR / "youtube_video_index.json"


def _normalize(raw):
    out = []
    for s in raw:
        if isinstance(s, dict):
            out.append({"text": s.get("text", ""),
                        "start": float(s.get("start", 0.0)),
                        "duration": float(s.get("duration", 0.0))})
        else:
            out.append({"text": getattr(s, "text", ""),
                        "start": float(getattr(s, "start", 0.0)),
                        "duration": float(getattr(s, "duration", 0.0))})
    return out


def fetch_one(video_id: str):
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    api = YouTubeTranscriptApi()
    try:
        if hasattr(YouTubeTranscriptApi, "fetch") and not hasattr(YouTubeTranscriptApi, "get_transcript"):
            fetched = api.fetch(video_id, languages=["ko", "ko-KR"])
            return True, "", _normalize(fetched)
        else:
            raw = YouTubeTranscriptApi.get_transcript(video_id, languages=["ko", "ko-KR"])  # type: ignore[attr-defined]
            return True, "", _normalize(raw)
    except Exception as exc:
        s = str(exc)
        kind = type(exc).__name__
        if "IpBlocked" in kind or "429" in s or "Too Many" in s:
            return False, "blocked", []
        if "TranscriptsDisabled" in kind or "NoTranscript" in kind:
            return False, "no_transcript", []
        return False, f"other:{kind}", []


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sleep", type=float, default=8.0)
    p.add_argument("--jitter", type=float, default=2.0)
    p.add_argument("--wait-minutes", type=int, default=30,
                   help="IpBlocked 시 다음 시도까지 대기 분")
    p.add_argument("--max-blocks", type=int, default=3,
                   help="연속 차단 N회면 종료")
    args = p.parse_args()

    if not INDEX_PATH.exists():
        logger.error("youtube_video_index.json 없음")
        sys.exit(1)

    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    todo = []
    for date_str, rec in sorted(idx.items()):
        vid = rec.get("video_id")
        if not vid:
            continue
        cache = TRANSCRIPTS_DIR / f"{vid}.json"
        if cache.exists():
            continue
        todo.append((date_str, vid, rec.get("title", "")))

    logger.info("자막 수집 대상: %d개 (총 인덱스 %d)", len(todo), len(idx))

    ok = 0
    fail = {"blocked": 0, "no_transcript": 0, "other": 0}
    consecutive_blocks = 0
    i = 0
    while i < len(todo):
        date_str, vid, title = todo[i]
        logger.info("[%d/%d] %s %s : %s", i + 1, len(todo), date_str, vid, title[:50])
        success, err, segs = fetch_one(vid)
        if success:
            (TRANSCRIPTS_DIR / f"{vid}.json").write_text(
                json.dumps(segs, ensure_ascii=False), encoding="utf-8")
            logger.info("  → %d segments 저장", len(segs))
            ok += 1
            consecutive_blocks = 0
            i += 1
            time.sleep(max(0.0, args.sleep + random.uniform(-args.jitter, args.jitter)))
        elif err == "blocked":
            consecutive_blocks += 1
            fail["blocked"] += 1
            logger.warning("  → IpBlocked (%d회 연속)", consecutive_blocks)
            if consecutive_blocks >= args.max_blocks:
                logger.error("연속 차단 한계 도달 → 종료")
                break
            wait_s = args.wait_minutes * 60
            logger.info("  → %d분 대기 후 재시도...", args.wait_minutes)
            time.sleep(wait_s)
            # 인덱스 i 그대로 재시도
        elif err == "no_transcript":
            fail["no_transcript"] += 1
            (TRANSCRIPTS_DIR / f"{vid}.json").write_text("[]", encoding="utf-8")
            consecutive_blocks = 0
            i += 1
        else:
            fail["other"] += 1
            consecutive_blocks = 0
            i += 1

    print("\n" + "=" * 60)
    print(f"자막: 성공 {ok}, 차단 {fail['blocked']}, 자막없음 {fail['no_transcript']}, 기타 {fail['other']}")
    print(f"미완료: {len(todo) - i}개 (다음 실행에서 재개)")
    print("=" * 60)


if __name__ == "__main__":
    main()
