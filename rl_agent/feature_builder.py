"""
RL Agent 특징 벡터 빌더 (한국주식 특화)
PPO action space: 0=HOLD, 1=BUY, 2=SELL (Adilbai/stock-trading-rl-agent 구조 참고)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


FEATURE_NAMES = [
    # 가격 관련
    "close_norm",          # 정규화된 종가
    "open_norm",
    "high_norm",
    "low_norm",
    "gap_rate",            # 당일 갭 비율
    "bounce_from_low",     # 저점 반등 비율
    # 기술지표
    "rsi14",
    "macd_hist_norm",
    "bb_pct",
    "ma5_ratio",           # close/MA5
    "ma20_ratio",
    "atr_rate",
    # 거래량
    "vol_ratio",
    # 시장 상황
    "market_change_rate",  # 지수 등락률
    "market_breadth",      # 상승 종목 비율
    # 포지션 상태
    "position_ratio",      # 보유 비율 (0~1)
    "unrealized_pnl_rate", # 미실현 손익률
    "holding_days_norm",   # 보유 일수 정규화 (0~1, 최대 10일)
    # 시간
    "time_in_day",         # 0~1 (09:00=0, 15:30=1)
]

N_FEATURES = len(FEATURE_NAMES)
N_ACTIONS = 3   # 0=HOLD, 1=BUY, 2=SELL

# ─── S5 전용 추가 특징 (기존 19개 + 16개 = 35개) ──────────────────
S5_EXTRA_FEATURES = [
    "sector_score",        # 섹터 뉴스 점수 0~1
    "leader_score",        # 대장주 점수 (change_rate×0.4 + vol_ratio×0.3 + tv×0.3)
    "ma5_gap",             # (close - ma5) / ma5
    "three_min_up_ratio",  # 3분봉 우상향 비율 0~1
    "vol_ratio_s5",        # 5일 평균 대비 거래량 / 5.0 (정규화)
    "news_freshness",      # 뉴스 발행 후 경과 분 / 120 (정규화, 최대 2시간)
    "sector_rank",         # 섹터 내 등수 정규화 (0=1위, 1=꼴찌)
    "intraday_change",     # 당일 등락률 (open 대비)
    "bid_ask_ratio",       # 매수잔량 / (매수+매도잔량), 0.5=균형
    "market_cap_norm",     # 시가총액 / 1조 (정규화, 최대 clamp 1.0)
    "foreign_buy_ratio",   # 외국인 순매수 / 거래대금, -1~1
    "inst_buy_ratio",      # 기관 순매수 / 거래대금, -1~1
    "related_sector_avg",  # 동일 섹터 종목 평균 등락률
    "holding_days_s5",     # S5 보유일 수 / 5 (최대 5일 정규화)
    "unrealized_s5",       # S5 포지션 미실현 손익률
    "time_since_open",     # 장 시작 후 경과 분 / 390 (09:00 기준)
]

N_FEATURES_S5 = N_FEATURES + len(S5_EXTRA_FEATURES)  # 35


def build_feature_vector(
    close: float,
    open_price: float,
    high: float,
    low: float,
    indicators: dict,
    market_change: float,
    market_breadth: float,
    position_ratio: float,
    unrealized_pnl_rate: float,
    holding_days: int,
    current_time: str,     # "HHmmss"
    price_scale: float = 1.0,
) -> np.ndarray:
    """
    정규화된 특징 벡터 반환 (N_FEATURES,)
    price_scale: 가격 정규화 기준 (예: 20일 평균 종가)
    """
    scale = price_scale if price_scale > 0 else close

    close_norm = close / scale
    open_norm = open_price / scale
    high_norm = high / scale
    low_norm = low / scale

    ma5 = indicators.get("ma5", close)
    ma5_ratio = close / ma5 if ma5 > 0 else 1.0
    ma20 = indicators.get("ma20", close)
    ma20_ratio = close / ma20 if ma20 > 0 else 1.0

    # 시간 정규화 (09:00~15:30 → 0~1)
    try:
        h, m, s = int(current_time[:2]), int(current_time[2:4]), int(current_time[4:6])
        total_minutes = (h * 60 + m) - 540   # 09:00 = 0
        time_in_day = max(0.0, min(1.0, total_minutes / 390))   # 6.5시간
    except (ValueError, IndexError):
        time_in_day = 0.5

    features = np.array([
        close_norm,
        open_norm,
        high_norm,
        low_norm,
        float(indicators.get("gap_rate", 0)),
        float(indicators.get("bounce_from_low", 0)),
        float(indicators.get("rsi14", 50)) / 100.0,
        float(indicators.get("macd_hist", 0)) / (scale + 1e-9),
        float(indicators.get("bb_pct", 0.5)),
        float(ma5_ratio),
        float(ma20_ratio),
        float(indicators.get("atr_rate", 0)),
        float(indicators.get("vol_ratio", 1)) / 5.0,   # 최대 5배 정규화
        float(market_change),
        float(market_breadth),
        float(position_ratio),
        float(unrealized_pnl_rate),
        float(min(holding_days, 10)) / 10.0,
        float(time_in_day),
    ], dtype=np.float32)

    # NaN/Inf 방어
    features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=-1.0)
    return features


def build_feature_vector_s5(
    close: float,
    open_price: float,
    high: float,
    low: float,
    indicators: dict,
    market_change: float,
    market_breadth: float,
    position_ratio: float,
    unrealized_pnl_rate: float,
    holding_days: int,
    current_time: str,
    price_scale: float = 1.0,
    # S5 전용 추가 파라미터
    sector_score: float = 0.0,
    leader_score: float = 0.0,
    three_min_up_ratio: float = 0.0,
    news_freshness_min: float = 0.0,    # 뉴스 발행 후 경과 분
    sector_rank: float = 0.0,           # 0=1위, 1=꼴찌 (정규화)
    bid_ask_ratio: float = 0.5,         # 매수/(매수+매도) 잔량 비율
    market_cap_trillion: float = 0.0,   # 시가총액 (조원)
    foreign_buy_ratio: float = 0.0,     # 외국인 순매수 비율 -1~1
    inst_buy_ratio: float = 0.0,        # 기관 순매수 비율 -1~1
    related_sector_avg: float = 0.0,    # 동일 섹터 평균 등락률
    holding_days_s5: int = 0,           # S5 포지션 보유일
    unrealized_s5: float = 0.0,         # S5 미실현 손익률
) -> np.ndarray:
    """
    S5 전략용 35-feature 벡터 반환.
    기존 19개 특징 + S5 전용 16개 추가.
    """
    # 기존 19개 특징
    base = build_feature_vector(
        close=close,
        open_price=open_price,
        high=high,
        low=low,
        indicators=indicators,
        market_change=market_change,
        market_breadth=market_breadth,
        position_ratio=position_ratio,
        unrealized_pnl_rate=unrealized_pnl_rate,
        holding_days=holding_days,
        current_time=current_time,
        price_scale=price_scale,
    )

    ma5 = indicators.get("ma5", close) or close
    ma5_gap = (close - ma5) / ma5 if ma5 > 0 else 0.0
    vol_ratio = float(indicators.get("vol_ratio", 1.0))

    # 시간(장 시작 후 경과 분) 계산
    try:
        h, m = int(current_time[:2]), int(current_time[2:4])
        elapsed_min = max(0.0, (h * 60 + m) - 540)   # 09:00 = 0
        time_since_open = min(1.0, elapsed_min / 390)
    except (ValueError, IndexError):
        time_since_open = 0.0

    # 당일 등락률 (close vs open)
    intraday_change = (close - open_price) / open_price if open_price > 0 else 0.0

    s5_extra = np.array([
        float(sector_score),
        float(leader_score),
        float(ma5_gap),
        float(three_min_up_ratio),
        float(vol_ratio) / 5.0,
        float(min(news_freshness_min, 120)) / 120.0,
        float(sector_rank),
        float(intraday_change),
        float(max(0.0, min(1.0, bid_ask_ratio))),
        float(min(1.0, market_cap_trillion / 10.0)),  # 10조 이상 = 1.0
        float(max(-1.0, min(1.0, foreign_buy_ratio))),
        float(max(-1.0, min(1.0, inst_buy_ratio))),
        float(related_sector_avg),
        float(min(holding_days_s5, 5)) / 5.0,
        float(unrealized_s5),
        float(time_since_open),
    ], dtype=np.float32)

    features = np.concatenate([base, s5_extra])
    features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=-1.0)
    return features
