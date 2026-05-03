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

# ─── 리스크 파라미터 ──────────────────────────────────────────────
DAILY_LOSS_LIMIT_RATE = -0.02       # 일일 손실 한도 -2%
WEEKLY_LOSS_LIMIT_RATE = -0.05      # 주간 손실 한도 -5%
WEEKLY_PAUSE_DAYS = 3               # 주간 손실 한도 도달 시 매매 중단 일수
CONSECUTIVE_LOSS_LIMIT = 3          # 연속 손실 허용 횟수
MAX_POSITION_RATIO = 0.20           # 종목당 최대 비중 20%
MAX_ORDER_AMOUNT = 10_000_000       # 1회 주문 최대 금액 1천만원
MIN_CASH_RATIO = 0.10               # 최소 현금 비중 10%
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
    "entry_after": "091500",
    "profit_target_1": 0.01,        # +1% → 50% 매도
    "profit_target_2": 0.02,        # +2% → 전량 매도
    "stop_loss_low": True,          # 당일 저가 이탈 시 손절
    "stop_loss_rate": -0.015,       # -1.5% 손절 (중간값)
    "intraday_close": True,         # 당일 청산 원칙
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
    "entry_time": "092000",         # 진입 허용 시각 09:20
    "min_sector_score": 0.3,        # 섹터 뉴스 점수 최소값
    "min_vol_ratio": 2.0,           # 5일 평균 대비 거래량 배수
    "three_min_up_ratio": 0.6,      # 최근 3분봉 중 우상향 비율
    "take_profit": 0.15,            # 익절 +15%
    "stop_loss": -0.05,             # 손절 -5%
    "overheat_rate": 0.20,          # 급등과열 기준 +20%
    "weak_hold_days": 2,            # 수익 약함 판단 최소 보유일
    "weak_min_profit": 0.03,        # 수익 약함 기준 최소 수익률
    "position_ratio": 0.15,         # 1회 최대 투입 비중 15%
}

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

# ─── 기타 ─────────────────────────────────────────────────────────
KOSPI_CODE = "0001"
KOSDAQ_CODE = "1001"
