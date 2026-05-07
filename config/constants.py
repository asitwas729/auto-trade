"""
한국주식 고정 상수
- 호가단위, 수수료, 세금, 상하한가, 거래시간 등
"""

# ─── 거래 시간 ────────────────────────────────────────────────────
MARKET_OPEN_TIME = "090000"        # 장 시작
MARKET_CLOSE_TIME = "153000"       # 장 종료
PRE_MARKET_START = "080000"        # 시간외 단일가 시작
AFTER_MARKET_END = "180000"        # 시간외 단일가 종료
STRATEGY_START_TIME = "091500"     # S1 전략 진입 시작 (09:15)
S2_CUTOFF_TIME = "092000"          # S2 청산 기한 (09:20)

# ─── 수수료 / 세금 ────────────────────────────────────────────────
COMMISSION_RATE = 0.00015          # 매수/매도 수수료 0.015%
TRANSACTION_TAX_RATE = 0.002       # 증권거래세 0.20% (코스피)
TRANSACTION_TAX_RATE_KOSDAQ = 0.002  # 코스닥 동일
SLIPPAGE_RATE = 0.0003             # 슬리피지 추정 0.03%

# ─── 상하한가 ─────────────────────────────────────────────────────
PRICE_LIMIT_RATE = 0.30            # 상하한가 ±30%

# ─── 호가단위 (KRX 기준) ─────────────────────────────────────────
# (가격 하한, 가격 상한, 호가단위)
TICK_SIZE_TABLE = [
    (0,      1_000,    1),
    (1_000,  5_000,    5),
    (5_000,  10_000,   10),
    (10_000, 50_000,   50),
    (50_000, 100_000,  100),
    (100_000, 500_000, 500),
    (500_000, float("inf"), 1_000),
]

# ─── 리스크 파라미터 (모의투자 공격형) ───────────────────────────
# 실거래 전환 시 반드시 보수값으로 복원 필요
DAILY_LOSS_LIMIT_RATE = -0.05       # -2% → -5%
WEEKLY_LOSS_LIMIT_RATE = -0.15      # -5% → -15%
WEEKLY_PAUSE_DAYS = 1               # 3 → 1 (빠른 복귀)
CONSECUTIVE_LOSS_LIMIT = 10         # 3 → 10 (연속손실 허용 확대)
MAX_POSITION_RATIO = 0.30           # 0.20 → 0.30 (종목 집중도 ↑)
MAX_ORDER_AMOUNT = 10_000_000       # 1회 주문 최대 금액 1천만원
MIN_CASH_RATIO = 0.05               # 0.10 → 0.05 (현금 비중 축소)
UNFILLED_ORDER_TIMEOUT_SEC = 300    # 미체결 주문 자동 취소 (5분)

# ─── 전략 기본 비중 ───────────────────────────────────────────────
DEFAULT_STRATEGY_WEIGHTS = {
    "S1": 0.22,
    "S2": 0.18,
    "S3": 0.22,
    "S4": 0.08,
    "S5": 0.15,
    "CASH": 0.15,
}
WEIGHT_ADJUST_STEP = 0.05           # 월간 리밸런싱 조정 단위
REBALANCE_LOOKBACK_TRADES = 20      # 최근 N 트레이드 기준

# ─── 전략별 파라미터 ──────────────────────────────────────────────
S1_PARAMS = {
    "entry_after":  "091500",       # 09:15 이후 진입 (시초 5분 노이즈 회피)
    "entry_before": "092500",       # 09:25 이전까지만 신규 진입
    "forced_close": "093000",       # 09:30 강제청산 (시초 변동성 끝나는 시점)
    "profit_target_1": 0.015,       # +1.5% 50% 익절 (R:R 1.0)
    "profit_target_2": 0.030,       # +3.0% 전량 익절 (R:R 2.0, 큰 추세)
    "stop_loss_low": True,
    "stop_loss_rate": -0.015,       # -1.5% 빠른 컷
    "intraday_close": True,
}

S2_PARAMS = {
    "after_hours_gap": 0.03,        # 시간외 +3% 이상
    "profit_target": 0.012,         # +1.2%
    "stop_loss_rate": -0.015,       # -1.5%
    "cutoff_time": "092000",        # 09:20 이전 청산
}

S3_PARAMS = {
    "target_discount_from_report": -0.20,  # 리포트 목표가 대비 -20%
    "hold_days_min": 3,
    "hold_days_max": 10,
    "profit_target_min": 0.05,
    "profit_target_max": 0.15,
    "trailing_stop": -0.03,         # 트레일링 스탑 -3%
    "stop_loss_rate": -0.05,        # -5%
}

S4_PARAMS = {
    "atr_high_threshold": 0.025,    # ATR 기준 고변동성
    "market_advance_decline_min": 0.4,  # 상승 종목 비율 최소
    "volume_threshold_ratio": 0.7,  # 거래대금 평균 대비 비율
    "circuit_breaker_drop": -0.02,  # 급락장 기준 -2%
}

S5_PARAMS = {
    # 모의투자 공격형 (실거래 전 백테스트로 재검증 필수)
    "entry_time": "090000",         # 09:20 → 09:00 (장 개장 즉시)
    "min_sector_score": 0.1,        # 0.3 → 0.1 (사실상 모든 섹터 통과)
    "min_vol_ratio": 0.1,           # 2.0 → 0.1 (장중 누적 거래량 부정합 회피)
    "three_min_up_ratio": 0.3,      # 0.6 → 0.3 (1/3 우상향만 요구)
    "take_profit": 0.08,            # 0.15 → 0.08 (빠른 익절)
    "stop_loss": -0.08,             # -0.05 → -0.08 (드로다운 허용)
    "overheat_rate": 0.30,          # 0.20 → 0.30
    "weak_hold_days": 5,            # 2 → 5
    "weak_min_profit": 0.01,        # 0.03 → 0.01
    "position_ratio": 0.30,         # 0.15 → 0.30 (1회 비중 ↑)
}

# ─── 시간대별 신규 전략 (S6/S7/S8/S9) ────────────────────────────
# 임계값은 백테스트 결과로 재조정 필요 (docs/backtest_results.md 참조)

S6_PARAMS = {
    "entry_start": "093000",        # 09:30 (시초 변동성 정리 후)
    "entry_end": "110000",          # 11:00
    "min_vol_ratio": 1.5,           # 거래량 1.5배 이상
    "min_three_min_up_ratio": 0.5,
    "stop_below_or_high_pct": 0.985,    # opening_range_high × 0.985 이탈 손절
    "stop_loss": -0.02,             # -2% 하드
    "trail_pullback": 0.02,         # high × 0.98 트레일
    "take_profit_1": 0.015,         # +1.5% 50% 매도
    "take_profit_2": 0.035,         # +3.5% 전량
    "forced_close": "151500",       # 15:15 시장가 강제
    "position_ratio": 0.20,
}

S7_PARAMS = {
    "entry_start": "100000",        # 10:00
    "entry_end": "140000",          # 14:00
    "lunch_start": "113000",        # 점심 신규 진입 차단
    "lunch_end":   "130000",
    "uptrend_min_intraday": 0.01,   # today_high > today_open × 1.01
    "pullback_min_pct": -0.04,      # 너무 깊은 하락은 추세 깨짐
    "pullback_max_pct": -0.015,     # 너무 얕은 하락은 아직 눌림목 X
    "min_three_min_up_ratio": 0.5,
    "min_vol_ratio": 1.0,
    "stop_from_high": -0.05,        # today_high × 0.95 이탈
    "stop_loss": -0.02,
    "take_profit_1": 0.01,          # +1% 50%
    "take_profit_2": 0.025,         # +2.5% 전량
    "time_stop_min": 90,            # 90분 보유 + 수익<0.3% → 청산
    "weak_min_profit": 0.003,
    "forced_close": "151500",
    "position_ratio": 0.20,
}

S8_PARAMS = {
    "entry_start": "130000",        # 13:00
    "entry_end":   "143000",        # 14:30
    "min_change_rate": 0.02,        # 당일 +2% 이상
    "max_change_rate": 0.12,        # 너무 과열된 종목 회피
    "min_three_min_up_ratio": 0.6,
    "min_vol_ratio": 1.5,
    "trail_pullback": 0.015,        # high × 0.985 트레일
    "stop_loss": -0.015,
    "take_profit": 0.02,            # +2% 전량
    "forced_close": "145000",       # 14:50 강제 (S9 베팅 전 마감)
    "position_ratio": 0.20,
}

S9_PARAMS = {
    "entry_start": "143000",        # 14:30
    "entry_end":   "151500",        # 15:15
    "min_change_rate": 0.01,        # 당일 +1% 이상
    "min_vol_ratio": 1.2,
    "next_day_exit_time": "090100", # 익일 09:01 시장가 매도
    "position_ratio": 0.10,         # 오버나이트 보수적
}

# ─── 우선순위 락 (한 종목당 1포지션 — Position 분리 트래킹은 별도 PR) ──
STRATEGY_PRIORITY = {
    "S6": 5,
    "S7": 4,
    "S8": 3,
    "S1": 2,
    "S9": 1,
    "S5": 0,
}

# ─── 매매 빈도 제한 ──────────────────────────────────────────────
REBUY_COOLDOWN_SEC = 1800           # 30분 종목별 재매수 쿨다운
MAX_BUYS_PER_CODE_PER_DAY = 3       # 단타 전략(S1/S6/S7/S8) 종목당 일일 매수 한도
MAX_TOTAL_BUYS_PER_DAY = 20

# 전략별 종목당 일일 매수 한도 (없으면 MAX_BUYS_PER_CODE_PER_DAY 사용).
# S5는 멀티데이 보유 전략이라 같은 종목 반복 진입 차단.
MAX_BUYS_PER_CODE_PER_STRATEGY = {
    "S5": 1,    # 종목당 1회/일 (반복 매매 방지)
    "S9": 1,    # 종가베팅도 1회/일
}

# ─── 마감 안전망 ─────────────────────────────────────────────────
CLOSEOUT_SWEEP_TIME = "152500"      # 15:25 잔여 단타 강제 시장가 청산
LUNCH_QUIET_START = "113000"        # 점심시간 (S7 신규 진입 차단)
LUNCH_QUIET_END   = "130000"

# ─── 단타 동적 워치리스트 (KIS volume-rank, 거래대금 상위) ────────
# 모든 단타 전략(S1/S6/S7/S8)이 공유하는 워치리스트. S5(섹터 멀티데이)는 별도.
# WebSocket 한도(~21) 고려: 20 단타 + S5 ~4 (중복 시 dedupe됨) + 보유 호가 ~3
INTRADAY_DYNAMIC_REFRESH_SEC = 300        # 5분 갱신
INTRADAY_DYNAMIC_INITIAL_SLOTS = 20       # 거래대금 상위 20종목
INTRADAY_DYNAMIC_MAX_SLOTS = 20
INTRADAY_DYNAMIC_MIN_PRICE = 50000   # 5만원 이상 (1주 단위 분할 효과↑, 동전주 회피)
INTRADAY_DYNAMIC_MAX_PRICE = 300000  # 30만원 이하 (1억 종목 1주 단위 회피)
INTRADAY_DYNAMIC_MIN_AVG_AMOUNT = 100_000_000  # 1억원

# 하위호환 alias (기존 import 깨지지 않도록)
S1_DYNAMIC_REFRESH_SEC = INTRADAY_DYNAMIC_REFRESH_SEC
S1_DYNAMIC_INITIAL_SLOTS = INTRADAY_DYNAMIC_INITIAL_SLOTS
S1_DYNAMIC_MAX_SLOTS = INTRADAY_DYNAMIC_MAX_SLOTS
S1_DYNAMIC_MIN_PRICE = INTRADAY_DYNAMIC_MIN_PRICE
S1_DYNAMIC_MAX_PRICE = INTRADAY_DYNAMIC_MAX_PRICE
S1_DYNAMIC_MIN_AVG_AMOUNT = INTRADAY_DYNAMIC_MIN_AVG_AMOUNT

# ─── 강제청산 부분체결 재시도 ────────────────────────────────────
FORCED_CLOSE_RETRY_MAX = 3
FORCED_CLOSE_RETRY_DELAY_SEC = 5

# ─── 섹터 키워드 (뉴스 매칭용) ───────────────────────────────────
# v2 (2026-05): 일반 단어 제거, 종목명/구체 용어 위주로 강화
#   - "AI", "플랫폼", "인터넷" 같은 일반어 제거 (GN 백필에서 IT플랫폼 점수가
#     100일 중 73일을 차지하던 편향 해소)
#   - "AI"는 실제 시장에서 반도체 테마(엔비디아·HBM)와 더 강하게 연결되어
#     반도체로 이동. "삼성전자"는 반도체+가전 모호성으로 "메모리반도체"로 대체.
SECTOR_KEYWORDS = {
    # 트레이딩 1순위 섹터
    "반도체":       ["반도체", "HBM", "파운드리", "DRAM", "낸드", "SK하이닉스",
                     "메모리반도체", "AI반도체", "AI 반도체", "엔비디아", "TSMC", "마이크론"],
    "2차전지":      ["2차전지", "양극재", "음극재", "에코프로", "LG에너지솔루션",
                     "전해액", "분리막", "리튬이온", "배터리셀"],
    "방산":         ["방산", "K방산", "한화에어로스페이스", "LIG넥스원", "현대로템",
                     "방위산업", "무기수출", "지정학", "K-9"],
    "친환경에너지": ["태양광", "풍력", "수소경제", "신재생에너지", "ESS", "그린에너지",
                     "원전", "SMR", "전력기기"],
    "자동차":       ["완성차", "현대차", "기아", "전기차", "자율주행", "현대모비스",
                     "수소차", "EV"],
    "바이오":       ["바이오시밀러", "신약", "임상시험", "제약사", "셀트리온",
                     "삼성바이오로직스", "유한양행", "FDA 승인"],
    # 보조 섹터
    "금융":         ["기준금리", "은행주", "보험주", "증권주", "배당주", "KB금융",
                     "신한지주", "하나금융"],
    "IT플랫폼":     ["네이버", "카카오", "게임주", "넷마블", "엔씨소프트", "크래프톤",
                     "웹툰엔터", "쿠팡"],
    "소비재":       ["화장품", "K뷰티", "면세점", "아모레퍼시픽", "LG생활건강",
                     "유통업", "F&B"],
    "조선기계":     ["조선업", "LNG선", "삼성중공업", "한화오션", "HD현대중공업",
                     "현대미포조선", "수주잔고"],
}

# ─── 섹터별 후보 종목 (시가총액·거래량 기준 분기 캐시 갱신) ────────
SECTOR_UNIVERSE = {
    "반도체":       ["005930", "000660", "042700", "086520", "099800"],
    "2차전지":      ["373220", "247540", "051910", "096770", "006400"],
    "방산":         ["012450", "079550", "047810", "272210", "003570"],
    "친환경에너지": ["009830", "263750", "285690", "950130", "009970"],
    "자동차":       ["005380", "000270", "012330", "064350", "204320"],
    "바이오":       ["068270", "207940", "326030", "091990", "145020"],
    "금융":         ["105560", "055550", "086790", "316140", "032830"],
    "IT플랫폼":     ["035420", "035720", "036570", "251270", "293490"],
    "소비재":       ["090430", "139480", "097950", "271560", "004370"],
    "조선기계":     ["009540", "010140", "042660", "329180", "017800"],
}

# ─── 섹터 로테이션 역상관 쌍 ─────────────────────────────────────
# 경쟁 쌍 중 하나가 선택되면 다른 쪽 score × 0.5 감산
ROTATION_COMPETING = [
    ("반도체",  "IT플랫폼"),       # 외국인 수급 핵심 vs 성장주
    ("반도체",  "2차전지"),        # 코스피 대형주 자금 경쟁
    ("방산",    "친환경에너지"),   # 지정학 리스크 ↑ vs 친환경 정책 ↑
    ("방산",    "조선기계"),       # 중공업 자금 경쟁
    ("2차전지", "바이오"),         # 개인 수급 경쟁 테마
    ("2차전지", "소비재"),         # 성장 테마 vs 중국 소비 회복
    ("금융",    "바이오"),         # 방어주 금리 수혜 vs 성장주
    ("금융",    "반도체"),         # 경기민감 대형주 내부 순환
    ("자동차",  "2차전지"),        # 완성차 실적 vs 배터리 소재 테마
    ("소비재",  "IT플랫폼"),       # 중국 소비 회복 기대 vs 국내 플랫폼
]

# ─── 뉴스 수집 URL ────────────────────────────────────────────────
NAVER_RSS_URL = "https://finance.naver.com/news/news_list.naver?mode=LSS2D&section_id=101&section_id2=258"
NAVER_SEARCH_API_URL = "https://openapi.naver.com/v1/search/news.json"

# ─── KIS API 엔드포인트 ───────────────────────────────────────────
KIS_BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
KIS_BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"
KIS_WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
KIS_WS_URL_MOCK = "ws://ops.koreainvestment.com:31000"

# REST API 경로
KIS_PATH_TOKEN = "/oauth2/tokenP"
KIS_PATH_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
KIS_PATH_ORDERBOOK = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
KIS_PATH_DAILY_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-daily-price"
KIS_PATH_MINUTE_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
KIS_PATH_ORDER_BUY = "/uapi/domestic-stock/v1/trading/order-cash"
KIS_PATH_ORDER_SELL = "/uapi/domestic-stock/v1/trading/order-cash"
KIS_PATH_ORDER_CANCEL = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
KIS_PATH_BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"
KIS_PATH_FILLED = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
KIS_PATH_UNFILLED = "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
KIS_PATH_VOLUME_RANK = "/uapi/domestic-stock/v1/quotations/volume-rank"

# TR ID (실전/모의 구분)
KIS_TR = {
    "real": {
        "buy": "TTTC0802U",
        "sell": "TTTC0801U",
        "cancel": "TTTC0803U",
        "balance": "TTTC8434R",
        "filled": "TTTC8001R",
        "unfilled": "TTTC8036R",
        "price": "FHKST01010100",
        "orderbook": "FHKST01010200",
        "daily_price": "FHKST03010100",
        "minute_price": "FHKST03010200",
        "volume_rank": "FHPST01710000",
        "ws_real_price": "H0STCNT0",
        "ws_orderbook": "H0STASP0",
    },
    "mock": {
        "buy": "VTTC0802U",
        "sell": "VTTC0801U",
        "cancel": "VTTC0803U",
        "balance": "VTTC8434R",
        "filled": "VTTC8001R",
        "unfilled": "VTTC8036R",
        "price": "FHKST01010100",
        "orderbook": "FHKST01010200",
        "daily_price": "FHKST03010100",
        "minute_price": "FHKST03010200",
        "volume_rank": "FHPST01710000",
        "ws_real_price": "H0STCNT0",
        "ws_orderbook": "H0STASP0",
    },
}

# ─── 시장 구분 ────────────────────────────────────────────────────
MARKET_KOSPI = "J"
MARKET_KOSDAQ = "Q"

# ─── API 재시도 ───────────────────────────────────────────────────
API_MAX_RETRY = 3
API_RETRY_DELAY_SEC = 1.0
API_TIMEOUT_SEC = 10

# ─── KIS API 레이트리밋 (req/s) ───────────────────────────────────
# 모의투자: 공식 2req/s지만 실측상 1req/s가 안정적 → 0.9로 보수 설정
# 실전투자: 공식 한도 약 20req/s, 안전 마진 두고 18
KIS_RATE_LIMIT_QPS_MOCK = 0.9
KIS_RATE_LIMIT_QPS_REAL = 18.0

# EGW00201(초당 한도 초과) 감지 시 페널티 추가 대기시간(초)
# 다음 호출 간격에 더해져서 점진 감쇠 (×0.5)
KIS_RATE_LIMIT_PENALTY_SEC = 2.0
KIS_RATE_LIMIT_PENALTY_MAX = 10.0

# ─── 기타 ─────────────────────────────────────────────────────────
KOSPI_CODE = "0001"
KOSDAQ_CODE = "1001"
