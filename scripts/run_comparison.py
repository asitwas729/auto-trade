"""
캐시들을 모아 섹터 시그널 N자 비교 실행.

소스:
  - YT      : data/transcripts/<video_id>.json + data/youtube_video_index.json
  - Naver   : data/news_cache_YYYYMMDD.json
  - GN      : data/google_news_cache_YYYYMMDD.json
  - Vol/Amt : data/sector_volume_YYYYMMDD.json (시총미정규화 거래대금)
  - Vol/Trn : data/sector_turnover_YYYYMMDD.json (회전율, 시총정규화)

모드:
  --mode recent --days N        : 최근 N 영업일, 4소스 (YT, Naver, GN, Turnover)
  --mode sample --sample-file F : 사전 생성된 일자 목록 (random_sample_dates.json), 3소스 (YT, GN, Turnover)

사용법:
    python scripts/run_comparison.py --mode recent --days 14
    python scripts/run_comparison.py --mode sample --sample-file data/random_sample_dates.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, time, timedelta, timezone
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import SECTOR_KEYWORDS
from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DATA_DIR = settings.DATA_DIR


# ─── 점수 계산 ──────────────────────────────────────────────────────

def score_from_text(text: str) -> dict[str, float]:
    scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        if not keywords:
            scores[sector] = 0.0
            continue
        hit = sum(1 for kw in keywords if kw in text)
        scores[sector] = min(hit / len(keywords), 1.0)
    return scores


def score_from_volume(date_yyyymmdd: str, prefix: str) -> dict[str, float]:
    """sector_volume_*.json 또는 sector_turnover_*.json 로드 후 max 정규화."""
    path = DATA_DIR / f"{prefix}_{date_yyyymmdd}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not raw:
        return {}
    mx = max(raw.values()) or 1
    out = {sec: 0.0 for sec in SECTOR_KEYWORDS}
    for sec, v in raw.items():
        out[sec] = float(v) / float(mx)
    return out


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    keys = list(SECTOR_KEYWORDS.keys())
    import math
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na * nb > 1e-9 else 0.0


def topn(s: dict[str, float], n: int = 3) -> list[str]:
    return sorted(s, key=lambda k: s.get(k, 0.0), reverse=True)[:n]


def match_rate(a: dict[str, float], b: dict[str, float]) -> float:
    return len(set(topn(a)) & set(topn(b))) / 3.0


# ─── 소스별 스코어 로더 ─────────────────────────────────────────────

def _load_news_text(path: Path) -> str:
    if not path.exists():
        return ""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return " ".join(f"{it.get('title','')} {it.get('summary','')}" for it in raw)


def load_naver(date_str: str) -> dict[str, float]:
    text = _load_news_text(DATA_DIR / f"news_cache_{date_str.replace('-','')}.json")
    return score_from_text(text) if text else {}


def load_gn(date_str: str) -> dict[str, float]:
    text = _load_news_text(DATA_DIR / f"google_news_cache_{date_str.replace('-','')}.json")
    return score_from_text(text) if text else {}


def load_yt_from_window(video_id: str, broadcast_start_iso: str,
                        win_start: time, win_end: time) -> dict[str, float]:
    """data/transcripts/<id>.json 캐시에서 윈도우 슬라이싱 후 점수."""
    path = DATA_DIR / "transcripts" / f"{video_id}.json"
    if not path.exists():
        return {}
    segs = json.loads(path.read_text(encoding="utf-8"))
    if not segs:
        return {}
    bc = datetime.fromisoformat(broadcast_start_iso)
    base = bc.date()
    lo = datetime.combine(base, win_start, tzinfo=KST)
    hi = datetime.combine(base, win_end, tzinfo=KST)
    kept = []
    for s in segs:
        seg_start = bc + timedelta(seconds=s["start"])
        seg_end = seg_start + timedelta(seconds=s["duration"])
        if seg_end < lo or seg_start > hi:
            continue
        kept.append(s["text"])
    if not kept:
        return {}
    return score_from_text(" ".join(kept))


def load_video_index() -> dict:
    """data/youtube_video_index.json 로드. 없으면 빈 dict."""
    p = DATA_DIR / "youtube_video_index.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def load_yt(date_str: str, video_index: dict, win_start: time, win_end: time) -> dict[str, float]:
    """date_str -> video_id, broadcast_start 매핑 후 자막 캐시에서 윈도우 점수."""
    rec = video_index.get(date_str)
    if not rec:
        return {}
    return load_yt_from_window(rec["video_id"], rec["broadcast_start_kst"], win_start, win_end)


def load_yt_scores_legacy(date_str: str, legacy: dict) -> dict[str, float]:
    """이전 youtube_vs_naver_*.json 결과의 yt_scores 재활용 (자막 캐시 없을 때 폴백)."""
    return legacy.get(date_str, {})


# ─── 메인 ───────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["recent", "sample"], required=True)
    p.add_argument("--days", type=int, default=14, help="recent 모드에서 lookback 영업일 수")
    p.add_argument("--sample-file", type=str, help="sample 모드: random_sample_dates.json 경로")
    p.add_argument("--time-start", type=str, default="06:05")
    p.add_argument("--time-end", type=str, default="07:25")
    p.add_argument("--vol-prefix", choices=["sector_volume", "sector_turnover"],
                   default="sector_turnover", help="거래대금 vs 회전율(시총정규화)")
    p.add_argument("--legacy-yt", type=str, default="",
                   help="자막 캐시 없을 때 fallback 으로 쓸 youtube_vs_naver_*.json (yt_scores 추출)")
    p.add_argument("--out", type=str, default="")
    args = p.parse_args()

    win_start = time(*map(int, args.time_start.split(":")))
    win_end = time(*map(int, args.time_end.split(":")))

    # 일자 목록
    if args.mode == "recent":
        today = datetime.now(KST).date()
        end = today - timedelta(days=1)
        dates = []
        cur = end
        while len(dates) < args.days * 2 and cur >= end - timedelta(days=args.days * 3):
            if cur.weekday() < 5:
                dates.append(cur)
            cur -= timedelta(days=1)
        dates = sorted(dates[:args.days])
    else:
        if not args.sample_file:
            logger.error("--sample-file 필요")
            sys.exit(1)
        sf = json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
        dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in sf["dates"]]

    logger.info("비교 대상 일자: %d개 (%s ~ %s)", len(dates), dates[0], dates[-1])

    # 소스 정의
    if args.mode == "recent":
        source_names = ["YT", "Naver", "GN", "Vol"]
    else:
        source_names = ["YT", "GN", "Vol"]

    video_index = load_video_index()
    legacy_map: dict[str, dict[str, float]] = {}
    if args.legacy_yt:
        try:
            legacy_raw = json.loads(Path(args.legacy_yt).read_text(encoding="utf-8"))
            for r in legacy_raw.get("results", []):
                if "yt_scores" in r:
                    legacy_map[r["date"]] = r["yt_scores"]
            logger.info("legacy YT 점수 로드: %d일", len(legacy_map))
        except Exception as exc:
            logger.warning("legacy YT 로드 실패: %s", exc)

    results: list[dict] = []
    miss = {n: 0 for n in source_names}

    for d in dates:
        date_str = d.strftime("%Y-%m-%d")
        scores: dict[str, dict[str, float]] = {}

        # YT
        yt = load_yt(date_str, video_index, win_start, win_end)
        if not yt and date_str in legacy_map:
            yt = legacy_map[date_str]
        if yt:
            scores["YT"] = yt
        else:
            miss["YT"] += 1

        # Naver (recent only)
        if "Naver" in source_names:
            nv = load_naver(date_str)
            if nv:
                scores["Naver"] = nv
            else:
                miss["Naver"] += 1

        # GN
        gn = load_gn(date_str)
        if gn:
            scores["GN"] = gn
        else:
            miss["GN"] += 1

        # Vol
        vol = score_from_volume(date_str.replace("-", ""), args.vol_prefix)
        if vol:
            scores["Vol"] = vol
        else:
            miss["Vol"] += 1

        if not scores:
            continue

        # 모든 페어 비교
        pairs = {}
        for a, b in combinations(source_names, 2):
            if a in scores and b in scores:
                pairs[f"{a}_vs_{b}"] = {
                    "cosine": round(cosine(scores[a], scores[b]), 4),
                    "match": round(match_rate(scores[a], scores[b]), 4),
                }
            else:
                pairs[f"{a}_vs_{b}"] = None

        results.append({
            "date": date_str,
            "sources_present": list(scores.keys()),
            "top3": {n: topn(s) for n, s in scores.items()},
            "pairs": pairs,
            "scores": {n: {k: round(v, 3) for k, v in s.items()} for n, s in scores.items()},
        })

    if not results:
        logger.error("비교 결과 없음")
        sys.exit(1)

    # 페어 평균
    pair_keys = [f"{a}_vs_{b}" for a, b in combinations(source_names, 2)]
    pair_avgs = {}
    for pk in pair_keys:
        cosines = [r["pairs"][pk]["cosine"] for r in results if r["pairs"].get(pk)]
        matches = [r["pairs"][pk]["match"] for r in results if r["pairs"].get(pk)]
        pair_avgs[pk] = {
            "n": len(cosines),
            "avg_cosine": round(sum(cosines) / len(cosines), 4) if cosines else None,
            "avg_match": round(sum(matches) / len(matches), 4) if matches else None,
        }

    # 출력
    print("\n" + "=" * 78)
    print(f"{args.mode.upper()} 비교 결과 ({len(results)}일치, vol_prefix={args.vol_prefix})")
    print("=" * 78)
    label = {
        "YT_vs_Naver": "삼프로 ↔ Naver  ",
        "YT_vs_GN":    "삼프로 ↔ GoogleNews",
        "YT_vs_Vol":   "삼프로 ↔ 거래데이터",
        "Naver_vs_GN": "Naver  ↔ GoogleNews",
        "Naver_vs_Vol":"Naver  ↔ 거래데이터",
        "GN_vs_Vol":   "GoogleNews ↔ 거래데이터",
    }
    print(f"  {'쌍':<24s} {'n':>3s}  {'평균코사인':>10s}  {'상위3 일치':>10s}")
    for pk in pair_keys:
        a = pair_avgs[pk]
        if a["n"] == 0:
            continue
        print(f"  {label.get(pk, pk):<24s} {a['n']:>3d}  {a['avg_cosine']:>10.4f}  "
              f"{a['avg_match']*100:>9.1f}%")
    print(f"\n  소스 미보유: {miss}")
    print("=" * 78)

    # 저장
    suffix = args.mode + ("_" + Path(args.sample_file).stem if args.mode == "sample" and args.sample_file else f"_{args.days}d")
    out_path = Path(args.out) if args.out else DATA_DIR / f"comparison_{suffix}_{datetime.now().strftime('%Y%m%d')}.json"
    summary = {
        "generated_at": datetime.now().isoformat(),
        "mode": args.mode,
        "vol_prefix": args.vol_prefix,
        "window_kst": f"{args.time_start}-{args.time_end}",
        "source_names": source_names,
        "total_compared": len(results),
        "missing_counts": miss,
        "pair_averages": pair_avgs,
        "results": results,
    }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s", out_path)


if __name__ == "__main__":
    main()
