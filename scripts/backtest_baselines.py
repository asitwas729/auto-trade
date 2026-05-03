"""
베이스라인 백테스트: '거래데이터 1순위 섹터 매수' 전략의 알파 여부.

전략들:
  S1. 거래대금(원시) 1순위 섹터 대장주 → T 매수, T+1 매도
  S2. 단순 회전율 1순위 ──── 동일 ──── (소형주 편향 검증용)
  S3. 시총가중 회전율 1순위 ─ 동일 ─── (★ 가장 균형 잡힌 메트릭)
  S4. 무작위 섹터 (control)
  S5. 50종목 균등 매수 → 매일 T+1 매도 (시장 baseline)

비용: 슬리피지 0.03%×2 + 수수료 0.015%×2 + 거래세 0.20% = 0.26%/round

사용법:
    python scripts/backtest_baselines.py
    python scripts/backtest_baselines.py --start 2024-05-22 --end 2026-04-30
    python scripts/backtest_baselines.py --sample-file data/random_sample_dates.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import random
import sys
from datetime import datetime
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
COST_ROUND = SLIPPAGE_RATE * 2 + COMMISSION_RATE * 2 + TRANSACTION_TAX_RATE  # 0.26%


# ─── 캐시 로더 ───────────────────────────────────────────────────────

_STOCK_CACHE: dict[str, dict[str, dict]] = {}


def load_stock(code: str) -> dict[str, dict]:
    if code in _STOCK_CACHE:
        return _STOCK_CACHE[code]
    p = DATA / "stock_daily" / f"{code}.csv"
    out: dict[str, dict] = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["date"]] = {"close": int(row["close"]),
                                    "open": int(row["open"])}
    _STOCK_CACHE[code] = out
    return out


def get_close(code: str, d: str) -> int | None:
    rec = load_stock(code).get(d)
    return rec["close"] if rec else None


def list_trading_dates() -> list[str]:
    return sorted(load_stock("005930").keys())


def load_metric_top1(prefix: str, d: str) -> str | None:
    p = DATA / f"{prefix}_{d}.json"
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not raw:
        return None
    return max(raw, key=raw.get)


# ─── 매매 시뮬레이션 ────────────────────────────────────────────────

def trade_return(sector: str, t_date: str, t1_date: str) -> float | None:
    codes = SECTOR_UNIVERSE.get(sector, [])
    if not codes:
        return None
    leader = codes[0]
    c0 = get_close(leader, t_date)
    c1 = get_close(leader, t1_date)
    if not (c0 and c1 and c0 > 0):
        return None
    return (c1 - c0) / c0 - COST_ROUND


def market_avg_return(t_date: str, t1_date: str) -> float | None:
    rets = []
    for codes in SECTOR_UNIVERSE.values():
        for code in codes:
            c0 = get_close(code, t_date)
            c1 = get_close(code, t1_date)
            if c0 and c1 and c0 > 0:
                rets.append((c1 - c0) / c0)
    return sum(rets) / len(rets) if rets else None


def stat(pnls: list[float]) -> dict:
    if not pnls:
        return {"n": 0}
    n = len(pnls)
    avg = sum(pnls) / n
    var = sum((r - avg) ** 2 for r in pnls) / n if n > 1 else 0
    std = math.sqrt(var)
    cum = 1.0
    for r in pnls:
        cum *= (1 + r)
    win = sum(1 for r in pnls if r > 0) / n
    return {
        "n": n,
        "avg_pct": avg * 100,
        "cum_pct": (cum - 1) * 100,
        "win_pct": win * 100,
        "sharpe": (avg / std * math.sqrt(252)) if std > 0 else 0.0,
    }


# ─── 메인 ───────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="YYYY-MM-DD (없으면 stock_daily 캐시 첫날)")
    p.add_argument("--end", help="YYYY-MM-DD (없으면 stock_daily 캐시 마지막날)")
    p.add_argument("--sample-file", help="random_sample_dates.json 경로 (있으면 우선)")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    all_d = list_trading_dates()
    if not all_d:
        logger.error("data/stock_daily/005930.csv 없음")
        sys.exit(1)

    if args.sample_file:
        sample = json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
        target = [d.replace("-", "") for d in sample["dates"] if d.replace("-", "") in set(all_d)]
        label = f"random sample {len(target)}일"
    else:
        s = args.start.replace("-", "") if args.start else all_d[0]
        e = args.end.replace("-", "") if args.end else all_d[-1]
        target = [d for d in all_d if s <= d <= e]
        label = f"{s} ~ {e} 영업일 {len(target)}일"

    logger.info("백테스트 범위: %s (%d일)", label, len(target))

    # T+1 매핑
    next_day_map: dict[str, str] = {}
    for i, d in enumerate(target):
        if d not in all_d:
            continue
        full_idx = all_d.index(d)
        if full_idx + 1 < len(all_d):
            next_day_map[d] = all_d[full_idx + 1]

    # 전략별 PnL
    strategies: dict[str, list[float]] = {
        "S1: 거래대금 1순위":       [],
        "S2: 단순 회전율 1순위":     [],
        "S3: 시총가중 회전율 1순위": [],
        "S4: 무작위 섹터 (control)": [],
        "S5: 50종목 균등 (시장)":    [],
    }
    rng = random.Random(args.seed)
    sector_list = list(SECTOR_UNIVERSE.keys())

    debug_top: dict[str, dict[str, int]] = {k: {} for k in strategies}

    for d in target:
        t1 = next_day_map.get(d)
        if not t1:
            continue

        # S1 거래대금
        sec = load_metric_top1("sector_volume", d)
        if sec:
            r = trade_return(sec, d, t1)
            if r is not None:
                strategies["S1: 거래대금 1순위"].append(r)
                debug_top["S1: 거래대금 1순위"][sec] = debug_top["S1: 거래대금 1순위"].get(sec, 0) + 1

        # S2 단순 회전율
        sec = load_metric_top1("sector_turnover", d)
        if sec:
            r = trade_return(sec, d, t1)
            if r is not None:
                strategies["S2: 단순 회전율 1순위"].append(r)
                debug_top["S2: 단순 회전율 1순위"][sec] = debug_top["S2: 단순 회전율 1순위"].get(sec, 0) + 1

        # S3 시총가중 회전율
        sec = load_metric_top1("sector_value_turnover", d)
        if sec:
            r = trade_return(sec, d, t1)
            if r is not None:
                strategies["S3: 시총가중 회전율 1순위"].append(r)
                debug_top["S3: 시총가중 회전율 1순위"][sec] = debug_top["S3: 시총가중 회전율 1순위"].get(sec, 0) + 1

        # S4 무작위 (같은 일자에 무작위 섹터 1개)
        sec = rng.choice(sector_list)
        r = trade_return(sec, d, t1)
        if r is not None:
            strategies["S4: 무작위 섹터 (control)"].append(r)
            debug_top["S4: 무작위 섹터 (control)"][sec] = debug_top["S4: 무작위 섹터 (control)"].get(sec, 0) + 1

        # S5 시장 평균 (50종목 균등 - 비용은 매번 적용한다고 가정하면 비현실적 → 비용 미적용 raw 시장지수로)
        mr = market_avg_return(d, t1)
        if mr is not None:
            strategies["S5: 50종목 균등 (시장)"].append(mr)  # 비용 미적용 baseline

    # 결과 출력
    print("\n" + "=" * 80)
    print(f"베이스라인 백테스트 결과 ({label})")
    print(f"비용: round-trip {COST_ROUND*100:.2f}% (S5 제외, S5는 비용 미적용 시장지수)")
    print("=" * 80)
    print(f"  {'전략':<28s}  {'n':>3s}  {'평균/거래':>10s}  {'누적':>10s}  {'승률':>7s}  {'샤프':>6s}")
    for name, pnls in strategies.items():
        s = stat(pnls)
        if s["n"] == 0:
            print(f"  {name:<28s}  데이터 없음")
            continue
        print(f"  {name:<28s}  {s['n']:>3d}  {s['avg_pct']:>+9.3f}%  "
              f"{s['cum_pct']:>+9.2f}%  {s['win_pct']:>6.1f}%  {s['sharpe']:>5.2f}")

    print("\n[전략별 1순위 섹터 분포]")
    for name, dist in debug_top.items():
        if not dist:
            continue
        total = sum(dist.values())
        top5 = sorted(dist.items(), key=lambda x: -x[1])[:5]
        print(f"  {name}:")
        for sec, c in top5:
            print(f"    {sec:<14s} {c:>3d}일 ({c*100/total:>5.1f}%)")

    # 핵심 알파 분석: S3 vs S5
    s3 = strategies["S3: 시총가중 회전율 1순위"]
    s5 = strategies["S5: 50종목 균등 (시장)"]
    if s3 and s5:
        # 일자 정렬해서 알파 계산
        s3_avg = sum(s3) / len(s3) * 100
        s5_avg = sum(s5) / len(s5) * 100
        print("\n" + "=" * 80)
        print("알파 분석:")
        print(f"  S3 (시총가중 회전율 1순위) 평균:  {s3_avg:+.4f}%")
        print(f"  S5 (시장 평균, 비용 미적용)  :  {s5_avg:+.4f}%")
        print(f"  차이 (알파 추정):                {s3_avg - s5_avg:+.4f}%")
        print(f"  ※ S3 는 비용 차감 / S5 는 비용 미적용 → S3 가 시장 비용 미적용보다 좋아야 진짜 알파")
        print("=" * 80)


if __name__ == "__main__":
    main()
