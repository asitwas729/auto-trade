"""
캐시들을 종합해 6개 분석 한꺼번에 실행:
  D. SECTOR_KEYWORDS v1 vs v2 효과 (sanity check)
  A. 선행성 검증 (T+0, T+1, T+2, T+3 적중률)
  B. 알파 검증 (뉴스 1순위 섹터의 T+1 수익률 vs 시장 평균)
  C. 간단 백테스트 (뉴스 1순위 대장주 매수 → T+1 매도)
  E. Confusion matrix (1순위 매트릭스)
  F. 메트릭 안정성 (amount/turnover/value_turnover 1순위 분포)

사전 준비:
  - data/stock_daily/<code>.csv (50종목 일봉)
  - data/sector_*.json (회전율/거래대금)
  - data/news_cache_*.json (Naver)
  - data/google_news_cache_*.json (GN)

사용법:
    python scripts/analyze_all.py
"""

from __future__ import annotations

import csv
import json
import logging
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import (
    COMMISSION_RATE,
    SECTOR_KEYWORDS,
    SECTOR_UNIVERSE,
    SLIPPAGE_RATE,
    TRANSACTION_TAX_RATE,
)
from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA = settings.DATA_DIR

# v1 (옛 키워드) — D 분석용
SECTOR_KEYWORDS_V1 = {
    "반도체":       ["반도체", "HBM", "파운드리", "DRAM", "낸드", "SK하이닉스", "삼성전자"],
    "2차전지":      ["배터리", "2차전지", "리튬", "양극재", "음극재", "에코프로", "LG에너지"],
    "방산":         ["방산", "무기", "K방산", "전쟁", "지정학", "한화에어로", "LIG넥스원"],
    "친환경에너지": ["태양광", "풍력", "수소", "신재생", "ESS", "그린에너지"],
    "자동차":       ["자동차", "완성차", "현대차", "기아", "전기차", "자율주행"],
    "바이오":       ["바이오", "신약", "임상", "제약", "바이오시밀러", "셀트리온"],
    "금융":         ["금리", "은행", "보험", "증권", "금융주", "배당"],
    "IT플랫폼":     ["플랫폼", "네이버", "카카오", "게임", "인터넷", "AI"],
    "소비재":       ["화장품", "K뷰티", "유통", "식품", "면세점", "아모레"],
    "조선기계":     ["조선", "중공업", "선박", "LNG선", "현대중공업"],
}


# ─── 공통 유틸 ──────────────────────────────────────────────────────

def score_text(text: str, keywords: dict) -> dict[str, float]:
    out = {}
    for sec, kws in keywords.items():
        if not kws:
            out[sec] = 0.0
            continue
        hit = sum(1 for kw in kws if kw in text)
        out[sec] = min(hit / len(kws), 1.0)
    return out


def topn(s: dict[str, float], n: int = 3) -> list[str]:
    return sorted(s, key=lambda k: s.get(k, 0.0), reverse=True)[:n]


def cosine(a: dict, b: dict) -> float:
    keys = list(SECTOR_KEYWORDS.keys())
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na * nb > 1e-9 else 0.0


def match_rate(a: dict, b: dict, n: int = 3) -> float:
    return len(set(topn(a, n)) & set(topn(b, n))) / n


def load_news_text(p: Path) -> str:
    if not p.exists():
        return ""
    return " ".join(f"{it.get('title','')} {it.get('summary','')}"
                    for it in json.loads(p.read_text(encoding="utf-8")))


def load_volume(prefix: str, date_yyyymmdd: str) -> dict[str, float]:
    p = DATA / f"{prefix}_{date_yyyymmdd}.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not raw:
        return {}
    mx = max(raw.values()) or 1
    out = {sec: 0.0 for sec in SECTOR_KEYWORDS}
    for sec, v in raw.items():
        out[sec] = float(v) / float(mx)
    return out


def load_naver_score(date_str: str, kw=SECTOR_KEYWORDS) -> dict:
    return score_text(load_news_text(DATA / f"news_cache_{date_str.replace('-','')}.json"), kw)


def load_gn_score(date_str: str, kw=SECTOR_KEYWORDS) -> dict:
    return score_text(load_news_text(DATA / f"google_news_cache_{date_str.replace('-','')}.json"), kw)


# ─── 종목별 일봉 캐시 로더 ──────────────────────────────────────────

_STOCK_CACHE: dict[str, dict[str, dict]] = {}  # code -> date_yyyymmdd -> {close, volume, ...}


def load_stock_csv(code: str) -> dict[str, dict]:
    if code in _STOCK_CACHE:
        return _STOCK_CACHE[code]
    p = DATA / "stock_daily" / f"{code}.csv"
    if not p.exists():
        _STOCK_CACHE[code] = {}
        return {}
    out = {}
    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            out[row["date"]] = {
                "open": int(row["open"]),
                "high": int(row["high"]),
                "low": int(row["low"]),
                "close": int(row["close"]),
                "volume": int(row["volume"]),
                "amount": int(row["amount"]),
            }
    _STOCK_CACHE[code] = out
    return out


def get_close(code: str, date_yyyymmdd: str) -> int | None:
    rec = load_stock_csv(code).get(date_yyyymmdd)
    return rec["close"] if rec else None


def list_trading_dates() -> list[str]:
    """KOSPI 영업일 = 005930 일봉 캐시의 날짜들."""
    return sorted(load_stock_csv("005930").keys())


def next_trading_day(date_yyyymmdd: str, k: int = 1) -> str | None:
    all_dates = list_trading_dates()
    if date_yyyymmdd not in all_dates:
        # 가장 가까운 다음 영업일 찾기
        later = [d for d in all_dates if d > date_yyyymmdd]
        if not later:
            return None
        anchor = later[0]
        idx = all_dates.index(anchor) + (k - 1)
    else:
        idx = all_dates.index(date_yyyymmdd) + k
    if idx < 0 or idx >= len(all_dates):
        return None
    return all_dates[idx]


# ─── 분석 D: v1 vs v2 키워드 ────────────────────────────────────────

def analysis_D(dates: list[str]) -> None:
    print("\n" + "=" * 78)
    print("[D] SECTOR_KEYWORDS v1 vs v2 — GN/Naver 1순위 분포 변화")
    print("=" * 78)

    for source, loader_path_fn in [
        ("Naver", lambda d: DATA / f"news_cache_{d.replace('-','')}.json"),
        ("GN", lambda d: DATA / f"google_news_cache_{d.replace('-','')}.json"),
    ]:
        v1_top1: dict[str, int] = {}
        v2_top1: dict[str, int] = {}
        n = 0
        for d in dates:
            text = load_news_text(loader_path_fn(d))
            if not text:
                continue
            s1 = score_text(text, SECTOR_KEYWORDS_V1)
            s2 = score_text(text, SECTOR_KEYWORDS)
            v1_top1[topn(s1, 1)[0]] = v1_top1.get(topn(s1, 1)[0], 0) + 1
            v2_top1[topn(s2, 1)[0]] = v2_top1.get(topn(s2, 1)[0], 0) + 1
            n += 1
        if n == 0:
            continue
        print(f"\n  [{source}] (n={n})")
        all_secs = sorted(set(v1_top1) | set(v2_top1),
                          key=lambda s: v2_top1.get(s, 0) + v1_top1.get(s, 0), reverse=True)
        print(f"    {'섹터':<14s} {'v1':>6s}  {'v2':>6s}  변화")
        for sec in all_secs:
            a = v1_top1.get(sec, 0)
            b = v2_top1.get(sec, 0)
            arrow = "→" if a == b else ("↑" if b > a else "↓")
            print(f"    {sec:<14s} {a:>3d}일 {a*100/n:>5.1f}%  {b:>3d}일 {b*100/n:>5.1f}%  {arrow}")


# ─── 분석 A: 선행성 검증 ────────────────────────────────────────────

def analysis_A(date_pairs: list[tuple[str, dict, str]]) -> None:
    """date_pairs: (date_str_iso, news_score, source_label)"""
    print("\n" + "=" * 78)
    print("[A] 선행성 검증 (뉴스 T일 → 거래데이터 T+k일 1순위 적중률)")
    print("=" * 78)

    by_source: dict[str, list[tuple[str, dict]]] = {}
    for d, s, src in date_pairs:
        if not s:
            continue
        by_source.setdefault(src, []).append((d, s))

    print(f"  {'소스':<12s}  {'n':>3s}  {'T+0 hit1':>8s}  {'T+1 hit1':>8s}  {'T+2 hit1':>8s}  {'T+3 hit1':>8s}")
    for src, items in by_source.items():
        cnt = [0, 0, 0, 0]
        used = [0, 0, 0, 0]
        for d, s in items:
            d8 = d.replace("-", "")
            news_top1 = topn(s, 1)[0]
            for k in range(4):
                target = d8 if k == 0 else next_trading_day(d8, k)
                if not target:
                    continue
                vol = load_volume("sector_value_turnover", target)
                if not vol:
                    continue
                used[k] += 1
                if topn(vol, 1)[0] == news_top1:
                    cnt[k] += 1
        rates = [cnt[i] / used[i] * 100 if used[i] else 0 for i in range(4)]
        print(f"  {src:<12s}  {len(items):>3d}  "
              f"{rates[0]:>7.1f}%  {rates[1]:>7.1f}%  {rates[2]:>7.1f}%  {rates[3]:>7.1f}%")


# ─── 분석 B: 알파 검증 ─────────────────────────────────────────────

def sector_return(sector: str, date_yyyymmdd: str) -> float | None:
    """섹터 5종목 T+1일 수익률 평균 (T일 종가→T+1일 종가)."""
    next_d = next_trading_day(date_yyyymmdd, 1)
    if not next_d:
        return None
    rets = []
    for code in SECTOR_UNIVERSE.get(sector, []):
        c0 = get_close(code, date_yyyymmdd)
        c1 = get_close(code, next_d)
        if c0 and c1 and c0 > 0:
            rets.append((c1 - c0) / c0)
    return sum(rets) / len(rets) if rets else None


def market_return(date_yyyymmdd: str) -> float | None:
    """50종목 평균 수익률을 시장 대용."""
    next_d = next_trading_day(date_yyyymmdd, 1)
    if not next_d:
        return None
    rets = []
    for codes in SECTOR_UNIVERSE.values():
        for code in codes:
            c0 = get_close(code, date_yyyymmdd)
            c1 = get_close(code, next_d)
            if c0 and c1 and c0 > 0:
                rets.append((c1 - c0) / c0)
    return sum(rets) / len(rets) if rets else None


def analysis_B(date_pairs: list[tuple[str, dict, str]]) -> None:
    print("\n" + "=" * 78)
    print("[B] 알파 검증 (뉴스 1순위 섹터의 T+1 수익률 vs 50종목 평균)")
    print("=" * 78)
    by_source: dict[str, list[tuple[str, dict]]] = {}
    for d, s, src in date_pairs:
        if s:
            by_source.setdefault(src, []).append((d, s))

    print(f"  {'소스':<12s}  {'n':>3s}  {'평균 섹터수익률':>13s}  {'평균 시장수익률':>13s}  {'평균 알파':>10s}  {'승률':>7s}")
    for src, items in by_source.items():
        secs, mkts, alphas, wins = [], [], [], []
        for d, s in items:
            d8 = d.replace("-", "")
            top1 = topn(s, 1)[0]
            sr = sector_return(top1, d8)
            mr = market_return(d8)
            if sr is None or mr is None:
                continue
            secs.append(sr)
            mkts.append(mr)
            alphas.append(sr - mr)
            wins.append(1 if (sr - mr) > 0 else 0)
        if not alphas:
            continue
        n = len(alphas)
        avg_sec = sum(secs) / n * 100
        avg_mkt = sum(mkts) / n * 100
        avg_alpha = sum(alphas) / n * 100
        win_rate = sum(wins) / n * 100
        print(f"  {src:<12s}  {n:>3d}  {avg_sec:>12.3f}%  {avg_mkt:>12.3f}%  {avg_alpha:>+9.3f}%  {win_rate:>6.1f}%")


# ─── 분석 C: 백테스트 ───────────────────────────────────────────────

def analysis_C(date_pairs: list[tuple[str, dict, str]]) -> None:
    print("\n" + "=" * 78)
    print("[C] 백테스트 — 뉴스 1순위 섹터 대장주 T 매수 → T+1 매도")
    print("    비용: 매수/매도 슬리피지 0.03%×2 + 수수료 0.015%×2 + 거래세 0.20%")
    print("=" * 78)
    cost_round = SLIPPAGE_RATE * 2 + COMMISSION_RATE * 2 + TRANSACTION_TAX_RATE

    by_source: dict[str, list[tuple[str, dict]]] = {}
    for d, s, src in date_pairs:
        if s:
            by_source.setdefault(src, []).append((d, s))

    print(f"  {'소스':<12s}  {'n':>3s}  {'평균 PnL/거래':>13s}  {'누적 PnL':>10s}  {'승률':>7s}  {'샤프':>6s}")
    for src, items in by_source.items():
        pnls = []
        for d, s in items:
            d8 = d.replace("-", "")
            top1 = topn(s, 1)[0]
            codes = SECTOR_UNIVERSE.get(top1, [])
            if not codes:
                continue
            leader = codes[0]  # 대장주 = 첫 번째 (시총 상위)
            next_d = next_trading_day(d8, 1)
            if not next_d:
                continue
            c0 = get_close(leader, d8)
            c1 = get_close(leader, next_d)
            if not (c0 and c1 and c0 > 0):
                continue
            ret = (c1 - c0) / c0 - cost_round
            pnls.append(ret)
        if not pnls:
            continue
        n = len(pnls)
        avg = sum(pnls) / n
        cum = 1.0
        for r in pnls:
            cum *= (1 + r)
        cum_pnl = (cum - 1) * 100
        win = sum(1 for r in pnls if r > 0) / n * 100
        std = math.sqrt(sum((r - avg) ** 2 for r in pnls) / n) if n > 1 else 0
        sharpe = avg / std * math.sqrt(252) if std > 0 else 0
        print(f"  {src:<12s}  {n:>3d}  {avg*100:>+12.3f}%  {cum_pnl:>+9.2f}%  {win:>6.1f}%  {sharpe:>5.2f}")


# ─── 분석 E: Confusion Matrix ──────────────────────────────────────

def analysis_E(date_pairs: list[tuple[str, dict, str]]) -> None:
    print("\n" + "=" * 78)
    print("[E] Confusion Matrix — 뉴스 1순위 vs 거래데이터(value_turnover) 1순위")
    print("=" * 78)
    by_source: dict[str, list[tuple[str, dict]]] = {}
    for d, s, src in date_pairs:
        if s:
            by_source.setdefault(src, []).append((d, s))

    for src, items in by_source.items():
        cm: dict[tuple[str, str], int] = {}
        for d, s in items:
            d8 = d.replace("-", "")
            vol = load_volume("sector_value_turnover", d8)
            if not vol:
                continue
            news_top = topn(s, 1)[0]
            vol_top = topn(vol, 1)[0]
            cm[(news_top, vol_top)] = cm.get((news_top, vol_top), 0) + 1
        if not cm:
            continue
        # 표 형태로 (행=뉴스 1순위, 열=실제 1순위)
        print(f"\n  [{src}] (행=뉴스 1순위, 열=실제 1순위, 빈 칸=0)")
        rows = sorted({k[0] for k in cm}, key=lambda x: -sum(v for k, v in cm.items() if k[0] == x))
        cols = sorted({k[1] for k in cm}, key=lambda x: -sum(v for k, v in cm.items() if k[1] == x))
        # 짧은 헤더
        col_short = [c[:5] for c in cols]
        print("    " + f"{'':<14s}" + " ".join(f"{c:>6s}" for c in col_short))
        for r in rows:
            cells = []
            for c in cols:
                v = cm.get((r, c), 0)
                cells.append(f"{v:>6d}" if v else f"{'·':>6s}")
            print("    " + f"{r:<14s}" + " ".join(cells))


# ─── 분석 F: 메트릭 안정성 ─────────────────────────────────────────

def analysis_F(dates: list[str]) -> None:
    print("\n" + "=" * 78)
    print("[F] 메트릭 안정성 — 거래데이터 메트릭별 1순위 분포")
    print("=" * 78)
    for prefix, label in [("sector_volume", "거래대금(원시)"),
                          ("sector_turnover", "단순 회전율"),
                          ("sector_value_turnover", "시총가중 회전율")]:
        cnt: dict[str, int] = {}
        n = 0
        for d in dates:
            v = load_volume(prefix, d.replace("-", ""))
            if not v:
                continue
            top = topn(v, 1)[0]
            cnt[top] = cnt.get(top, 0) + 1
            n += 1
        if n == 0:
            continue
        print(f"\n  [{label}] (n={n})")
        for sec, c in sorted(cnt.items(), key=lambda kv: -kv[1]):
            bar = "█" * int(c * 40 / n)
            print(f"    {sec:<14s} {c:>3d} ({c*100/n:>5.1f}%) {bar}")


# ─── 메인 ───────────────────────────────────────────────────────────

def main() -> None:
    # 영업일 list 확보 (005930 캐시 기반)
    all_trading = list_trading_dates()
    if not all_trading:
        logger.error("data/stock_daily/005930.csv 없음. fetch_stock_daily_cache.py 먼저 실행")
        sys.exit(1)
    logger.info("영업일 캐시: %d일 (%s ~ %s)", len(all_trading), all_trading[0], all_trading[-1])

    # 분석 대상 일자: 100일 random sample 사용
    sample = json.loads((DATA / "random_sample_dates.json").read_text(encoding="utf-8"))["dates"]
    sample_iso = [d for d in sample if d.replace("-", "") in set(all_trading)]

    # 뉴스 source x 일자 페어 만들기
    pairs: list[tuple[str, dict, str]] = []
    for d in sample_iso:
        nv = load_naver_score(d)
        if nv:
            pairs.append((d, nv, "Naver"))
        gn = load_gn_score(d)
        if gn:
            pairs.append((d, gn, "GN"))

    logger.info("분석 페어: %d (소스×일자)", len(pairs))

    # D: v1 vs v2 (모든 100일)
    analysis_D(sample_iso)

    # A: 선행성
    analysis_A(pairs)

    # B: 알파
    analysis_B(pairs)

    # C: 백테스트
    analysis_C(pairs)

    # E: Confusion matrix
    analysis_E(pairs)

    # F: 메트릭 분포 (영업일 전체)
    f_dates = [d.replace("-", "") for d in sample_iso]
    analysis_F(sample_iso)


if __name__ == "__main__":
    main()
