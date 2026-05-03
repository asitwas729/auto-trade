"""
youtube_video_index.json 의 video_id 들에 대해 한국어 자막을 받아
data/transcripts/<video_id>.json 으로 캐시.

IP 차단(429) 회피를 위해 각 자막 사이 5~10초 throttle 기본.
이미 캐시 있으면 스킵, 차단 발생 시 즉시 중단(다음 실행에서 재개).

사용법:
    python scripts/fetch_transcripts.py
    python scripts/fetch_transcripts.py --sleep 8 --max 30
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = settings.DATA_DIR
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = DATA_DIR / "youtube_video_index.json"


def _normalize(raw) -> list[dict]:
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


def fetch_one(video_id: str) -> tuple[bool, str, list[dict]]:
    """(success, error_kind, segments). error_kind: 'blocked' | 'no_transcript' | 'other' | ''"""
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    try:
        api = YouTubeTranscriptApi()
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
        return False, "other:" + kind[:40], []


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sleep", type=float, default=8.0, help="자막 간 대기(초). 차단 회피용")
    p.add_argument("--jitter", type=float, default=2.0, help="대기 jitter ±초")
    p.add_argument("--max", type=int, default=0, help="이번 실행에서 최대 N개만 (0=무제한)")
    p.add_argument("--stop-on-block", action="store_true", default=True,
                   help="IpBlocked 발생 시 즉시 중단 (기본 ON)")
    args = p.parse_args()

    if not INDEX_PATH.exists():
        logger.error("youtube_video_index.json 없음. build_video_index.py 먼저 실행")
        sys.exit(1)

    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    todo = []
    for date_str, rec in idx.items():
        vid = rec.get("video_id")
        if not vid:
            continue
        cache = TRANSCRIPTS_DIR / f"{vid}.json"
        if cache.exists():
            continue
        todo.append((date_str, vid, rec.get("title", "")))

    logger.info("자막 수집 대상 %d개 (총 인덱스 %d)", len(todo), len(idx))
    if args.max > 0:
        todo = todo[:args.max]
        logger.info("이번 실행: %d개로 제한", len(todo))

    ok = 0
    fail = {"blocked": 0, "no_transcript": 0, "other": 0}
    for i, (date_str, vid, title) in enumerate(todo, 1):
        logger.info("[%d/%d] %s %s : %s", i, len(todo), date_str, vid, title[:50])
        success, err, segs = fetch_one(vid)
        if success:
            (TRANSCRIPTS_DIR / f"{vid}.json").write_text(
                json.dumps(segs, ensure_ascii=False), encoding="utf-8")
            logger.info("  → %d segments 저장", len(segs))
            ok += 1
        else:
            logger.warning("  → 실패: %s", err)
            if err == "blocked":
                fail["blocked"] += 1
                if args.stop_on_block:
                    logger.error("IP 차단 감지 → 중단. 시간 두고 재실행하세요.")
                    break
            elif err == "no_transcript":
                fail["no_transcript"] += 1
                # 빈 파일 저장으로 향후 재시도 방지
                (TRANSCRIPTS_DIR / f"{vid}.json").write_text("[]", encoding="utf-8")
            else:
                fail["other"] += 1

        sleep_s = max(0.0, args.sleep + random.uniform(-args.jitter, args.jitter))
        time.sleep(sleep_s)

    print("\n" + "=" * 60)
    print(f"자막 수집: 성공 {ok}, 차단 {fail['blocked']}, 자막없음 {fail['no_transcript']}, 기타 {fail['other']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
