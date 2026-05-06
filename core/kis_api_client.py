"""
한국투자증권 Open API 클라이언트
- Access Token 발급/갱신 (만료 24시간 전 자동 갱신)
- REST API: 현재가, 일봉/분봉, 호가, 주문, 취소, 잔고, 체결
- WebSocket: 실시간 체결가, 호가 구독
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import aiohttp
import requests
import websocket  # websocket-client

from config.constants import (
    API_MAX_RETRY,
    API_RETRY_DELAY_SEC,
    API_TIMEOUT_SEC,
    KIS_BASE_URL_MOCK,
    KIS_BASE_URL_REAL,
    KIS_PATH_BALANCE,
    KIS_PATH_DAILY_PRICE,
    KIS_PATH_FILLED,
    KIS_PATH_MINUTE_PRICE,
    KIS_PATH_ORDER_BUY,
    KIS_PATH_ORDER_CANCEL,
    KIS_PATH_ORDER_SELL,
    KIS_PATH_ORDERBOOK,
    KIS_PATH_PRICE,
    KIS_PATH_TOKEN,
    KIS_PATH_UNFILLED,
    KIS_PATH_VOLUME_RANK,
    KIS_RATE_LIMIT_PENALTY_MAX,
    KIS_RATE_LIMIT_PENALTY_SEC,
    KIS_RATE_LIMIT_QPS_MOCK,
    KIS_RATE_LIMIT_QPS_REAL,
    KIS_TR,
    KIS_WS_URL_MOCK,
    KIS_WS_URL_REAL,
    MARKET_KOSPI,
)
from config.settings import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# 데이터 클래스
# ─────────────────────────────────────────────────────────────────

class RateLimitError(Exception):
    """KIS API 초당 거래건수 초과 (EGW00201)"""


class KISBusinessError(Exception):
    """KIS 비즈니스 로직 거부 (잔고부족, 호가단위, 매도가능수량 등).
    인프라 정상이므로 _set_api_error 플래그는 설정하지 않음.
    msg_cd 4xxxxxxx 류가 여기로 분류됨."""

    def __init__(self, msg_cd: str, msg: str) -> None:
        super().__init__(f"[{msg_cd}] {msg}")
        self.msg_cd = msg_cd
        self.msg = msg


@dataclass
class TokenInfo:
    access_token: str
    token_type: str
    expires_at: datetime  # 만료 시각

    def is_expired(self, margin_minutes: int = 30) -> bool:
        return datetime.now() >= self.expires_at - timedelta(minutes=margin_minutes)


@dataclass
class PriceData:
    code: str
    current_price: int
    open_price: int
    high_price: int
    low_price: int
    prev_close: int
    volume: int
    amount: int            # 거래대금 (원)
    change_rate: float     # 등락률 (%)
    is_upper_limit: bool   # 상한가
    is_lower_limit: bool   # 하한가
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class OrderResult:
    success: bool
    order_no: str = ""
    message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class BalanceItem:
    code: str
    name: str
    quantity: int
    avg_price: float
    current_price: int
    eval_amount: int       # 평가금액
    profit_loss: int       # 평가손익
    profit_loss_rate: float


@dataclass
class Balance:
    cash: int              # 주문가능현금
    total_eval: int        # 총 평가금액 (현금 + 주식)
    positions: list[BalanceItem] = field(default_factory=list)


@dataclass
class FilledOrder:
    order_no: str
    code: str
    name: str
    side: str              # "BUY" | "SELL"
    order_qty: int
    filled_qty: int
    order_price: int
    avg_filled_price: float
    filled_at: datetime
    status: str            # "완전체결" | "부분체결" | "취소"


# ─────────────────────────────────────────────────────────────────
# 메인 클라이언트
# ─────────────────────────────────────────────────────────────────

class KISApiClient:
    """
    한국투자증권 Open API 클라이언트 (REST + WebSocket)

    사용법:
        client = KISApiClient()
        client.init()                          # 토큰 발급
        price = client.get_price("005930")     # 삼성전자 현재가
        client.subscribe_realtime(["005930"], callback)  # 실시간 구독
    """

    def __init__(self) -> None:
        self._mode = settings.TRADING_MODE
        self._is_mock = settings.is_mock or settings.is_paper

        self._base_url = KIS_BASE_URL_MOCK if self._is_mock else KIS_BASE_URL_REAL
        self._ws_url = KIS_WS_URL_MOCK if self._is_mock else KIS_WS_URL_REAL
        self._tr = KIS_TR["mock"] if self._is_mock else KIS_TR["real"]

        self._app_key = settings.app_key
        self._app_secret = settings.app_secret
        self._account_no = settings.account_no
        self._account_product = settings.KIS_ACCOUNT_PRODUCT

        self._token: Optional[TokenInfo] = None
        self._token_lock = threading.Lock()

        # WebSocket 상태
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_connected = threading.Event()
        self._ws_subscriptions: dict[str, Callable] = {}  # key → callback
        self._ws_approval_key: Optional[str] = None
        self._subscribed_codes: set[str] = set()  # 현재 구독 중인 종목코드
        self._ws_intentional_close = False         # unsubscribe_all 호출 시 True
        self._ws_reconnect_attempts = 0            # 연속 재연결 시도 횟수

        # API 오류 감지 플래그
        self._api_error = False
        self._last_error_time: Optional[datetime] = None

        # 글로벌 레이트리밋 (KIS 초당 거래건수 한도 준수)
        self._rate_limit_qps = (
            KIS_RATE_LIMIT_QPS_MOCK if self._is_mock else KIS_RATE_LIMIT_QPS_REAL
        )
        self._rate_limit_lock = threading.Lock()
        self._last_request_at = 0.0
        self._rate_limit_penalty = 0.0  # EGW00201 적응형 페널티

        # 토큰 디스크 캐시 (재시작 시 재발급 방지 - EGW00133 분당 1회 제한 회피)
        self._token_cache_path = (
            settings.DATA_DIR / f".kis_token_{self._mode}.json"
        )

        logger.info(
            f"KISApiClient 초기화 | 모드={self._mode} | "
            f"rate_limit={self._rate_limit_qps}req/s"
        )

    # ─────────────────────────────────────────────────────────────
    # 초기화
    # ─────────────────────────────────────────────────────────────

    def init(self) -> None:
        """토큰 발급 및 WebSocket 승인키 획득"""
        # 디스크 캐시에서 우선 로드 (EGW00133 회피)
        if not self._load_cached_token():
            self._issue_token()
        self._get_ws_approval_key()
        logger.info("KISApiClient 초기화 완료")

    # ─────────────────────────────────────────────────────────────
    # 토큰 관리
    # ─────────────────────────────────────────────────────────────

    def _load_cached_token(self) -> bool:
        """디스크 캐시 토큰 로드. 유효하면 True 반환."""
        try:
            if not self._token_cache_path.exists():
                return False
            with open(self._token_cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            expires_at = datetime.fromisoformat(cached["expires_at"])
            token = TokenInfo(
                access_token=cached["access_token"],
                token_type=cached.get("token_type", "Bearer"),
                expires_at=expires_at,
            )
            if token.is_expired():
                logger.info("캐시된 토큰 만료 - 신규 발급 필요")
                return False
            with self._token_lock:
                self._token = token
            logger.info(
                f"캐시된 Access Token 사용 | 만료: {expires_at:%Y-%m-%d %H:%M:%S}"
            )
            return True
        except Exception as exc:
            logger.warning(f"토큰 캐시 로드 실패: {exc}")
            return False

    def _save_cached_token(self) -> None:
        """발급/갱신된 토큰을 디스크에 저장."""
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._token_lock:
                if self._token is None:
                    return
                payload = {
                    "access_token": self._token.access_token,
                    "token_type": self._token.token_type,
                    "expires_at": self._token.expires_at.isoformat(),
                }
            with open(self._token_cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception as exc:
            logger.warning(f"토큰 캐시 저장 실패: {exc}")

    def _issue_token(self) -> None:
        url = f"{self._base_url}{KIS_PATH_TOKEN}"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        logger.debug(f"토큰 요청 → {url} | appkey={self._app_key[:10]}...")
        resp = requests.post(url, json=body, timeout=API_TIMEOUT_SEC)
        if not resp.ok:
            try:
                err_body = resp.json()
                err_code = err_body.get("error_code", "") if isinstance(err_body, dict) else ""
            except Exception:
                err_body = resp.text[:300]
                err_code = ""
            # EGW00133: 분당 1회 제한 - 명확한 안내
            if err_code == "EGW00133":
                raise RuntimeError(
                    f"토큰 발급 분당 1회 제한 (EGW00133): {err_body}\n"
                    f"  → 1분 후 재시도하세요. 또는 이미 발급된 토큰이 "
                    f"{self._token_cache_path}에 캐시되어야 합니다."
                )
            raise RuntimeError(
                f"토큰 발급 실패 HTTP {resp.status_code}: {err_body}\n"
                f"  서버: {url}\n"
                f"  원인 체크: ① 모의투자 서비스 미신청 ② 앱키 만료/오류 ③ IP 차단"
            )
        data = resp.json()

        expires_at = datetime.now() + timedelta(seconds=int(data["expires_in"]))
        with self._token_lock:
            self._token = TokenInfo(
                access_token=data["access_token"],
                token_type=data["token_type"],
                expires_at=expires_at,
            )
        logger.info(f"Access Token 발급 완료 | 만료: {expires_at:%Y-%m-%d %H:%M:%S}")
        self._save_cached_token()

    def _ensure_token(self) -> str:
        with self._token_lock:
            needs_refresh = self._token is None or self._token.is_expired()
        if needs_refresh:
            logger.info("Access Token 갱신 중...")
            self._issue_token()
        with self._token_lock:
            return self._token.access_token

    def _get_ws_approval_key(self) -> None:
        url = f"{self._base_url}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        resp = requests.post(url, json=body, timeout=API_TIMEOUT_SEC)
        resp.raise_for_status()
        self._ws_approval_key = resp.json()["approval_key"]
        logger.debug("WebSocket 승인키 획득 완료")

    # ─────────────────────────────────────────────────────────────
    # 공통 REST 요청
    # ─────────────────────────────────────────────────────────────

    def _headers(self, tr_id: str, extra: dict | None = None) -> dict:
        h = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._ensure_token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }
        if extra:
            h.update(extra)
        return h

    def _throttle(self) -> None:
        """글로벌 요청 간격 강제 + EGW00201 적응형 페널티 (멀티스레드 안전)"""
        min_interval = 1.0 / self._rate_limit_qps
        with self._rate_limit_lock:
            now = time.monotonic()
            wait = self._last_request_at + min_interval + self._rate_limit_penalty - now
            if wait > 0:
                # 락 잡은 채 sleep해야 다른 스레드 동시 통과 방지
                time.sleep(wait)
            # 페널티는 호출 통과할 때마다 절반으로 감쇠
            self._rate_limit_penalty *= 0.5
            if self._rate_limit_penalty < 0.05:
                self._rate_limit_penalty = 0.0
            self._last_request_at = time.monotonic()

    def _add_rate_limit_penalty(self) -> None:
        """EGW00201 발생 시 다음 호출 간격을 늘려 KIS 서버를 진정시킴"""
        with self._rate_limit_lock:
            self._rate_limit_penalty = min(
                self._rate_limit_penalty + KIS_RATE_LIMIT_PENALTY_SEC,
                KIS_RATE_LIMIT_PENALTY_MAX,
            )
        logger.warning(
            f"레이트리밋 페널티 추가 → 현재 +{self._rate_limit_penalty:.1f}s"
        )

    @staticmethod
    def _parse_kis_error(exc: requests.HTTPError) -> tuple[str, str]:
        """HTTP 에러 응답에서 (msg_cd, msg1) 추출"""
        try:
            body = exc.response.json()
            return body.get("msg_cd", ""), (body.get("msg1") or body.get("message") or "")
        except Exception:
            return "", (exc.response.text[:200] if exc.response is not None else "")

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        url = f"{self._base_url}{path}"
        headers = self._headers(tr_id)
        last_exc = None
        for attempt in range(1, API_MAX_RETRY + 1):
            try:
                self._throttle()
                resp = requests.get(
                    url, headers=headers, params=params, timeout=API_TIMEOUT_SEC
                )
                resp.raise_for_status()
                data = resp.json()
                self._check_api_response(data)
                self._api_error = False
                return data
            except KISBusinessError as exc:
                logger.warning(f"GET {path} KIS 거부: {exc}")
                raise
            except RateLimitError as exc:
                last_exc = exc
                self._add_rate_limit_penalty()
                backoff = min(2 ** (attempt - 1), 8)
                logger.warning(
                    f"GET {path} 초당 한도 초과 ({attempt}/{API_MAX_RETRY}) - {backoff}s 대기"
                )
                if attempt < API_MAX_RETRY:
                    time.sleep(backoff)
            except requests.HTTPError as exc:
                last_exc = exc
                msg_cd, kis_msg = self._parse_kis_error(exc)
                # KIS는 EGW00201(초당 한도 초과)을 HTTP 500으로 내려줌
                if msg_cd == "EGW00201":
                    self._add_rate_limit_penalty()
                    backoff = min(2 ** (attempt - 1), 8)
                    logger.warning(
                        f"GET {path} 초당 한도 초과 ({attempt}/{API_MAX_RETRY}) - {backoff}s 대기"
                    )
                    if attempt < API_MAX_RETRY:
                        time.sleep(backoff)
                    continue
                logger.warning(
                    f"GET {path} 실패 ({attempt}/{API_MAX_RETRY}) "
                    f"HTTP {exc.response.status_code} [{msg_cd}]: {kis_msg}"
                )
                if attempt < API_MAX_RETRY:
                    time.sleep(API_RETRY_DELAY_SEC * attempt)
            except Exception as exc:
                last_exc = exc
                logger.warning(f"GET {path} 실패 ({attempt}/{API_MAX_RETRY}): {exc}")
                if attempt < API_MAX_RETRY:
                    time.sleep(API_RETRY_DELAY_SEC * attempt)
        self._set_api_error()
        raise RuntimeError(f"GET {path} 최대 재시도 초과: {last_exc}") from last_exc

    def _post(self, path: str, tr_id: str, body: dict, hash_key: bool = True) -> dict:
        url = f"{self._base_url}{path}"
        extra = {}
        if hash_key:
            extra["hashkey"] = self._make_hashkey(body)
        headers = self._headers(tr_id, extra)
        is_order = "order" in path.lower()
        last_exc = None
        # 레이트리밋/일시적 거부는 5분 전체차단 대상이 아님 (개별 주문만 실패)
        set_api_error_on_exit = True
        for attempt in range(1, API_MAX_RETRY + 1):
            try:
                self._throttle()
                resp = requests.post(
                    url, headers=headers, json=body, timeout=API_TIMEOUT_SEC
                )
                resp.raise_for_status()
                data = resp.json()
                self._check_api_response(data)
                self._api_error = False
                return data
            except KISBusinessError as exc:
                # 잔고부족/호가단위 등 비즈니스 거부 - 인프라 정상이므로
                # api_error 플래그 안 건드리고 즉시 호출자에게 전달.
                logger.warning(f"POST {path} KIS 거부: {exc}")
                raise
            except RateLimitError as exc:
                last_exc = exc
                self._add_rate_limit_penalty()
                # 주문은 재시도 금지 (이중 주문 방지) + 레이트리밋은 일시적이므로
                # 전체 차단(api_error) 안 함 — 다음 tick에서 재시도 가능
                if is_order:
                    logger.error(f"POST {path} 초당 한도 초과 - 주문 재시도 금지")
                    set_api_error_on_exit = False
                    break
                backoff = min(2 ** (attempt - 1), 8)
                logger.warning(
                    f"POST {path} 초당 한도 초과 ({attempt}/{API_MAX_RETRY}) - {backoff}s 대기"
                )
                if attempt < API_MAX_RETRY:
                    time.sleep(backoff)
            except requests.HTTPError as exc:
                last_exc = exc
                msg_cd, kis_msg = self._parse_kis_error(exc)
                if msg_cd == "EGW00201":
                    self._add_rate_limit_penalty()
                    if is_order:
                        logger.error(f"POST {path} 초당 한도 초과 - 주문 재시도 금지")
                        set_api_error_on_exit = False
                        break
                    backoff = min(2 ** (attempt - 1), 8)
                    logger.warning(
                        f"POST {path} 초당 한도 초과 ({attempt}/{API_MAX_RETRY}) - {backoff}s 대기"
                    )
                    if attempt < API_MAX_RETRY:
                        time.sleep(backoff)
                    continue
                logger.warning(
                    f"POST {path} 실패 ({attempt}/{API_MAX_RETRY}) "
                    f"HTTP {exc.response.status_code} [{msg_cd}]: {kis_msg}"
                )
                if is_order:
                    logger.error("주문 API 재시도 금지 - 이중 주문 위험")
                    break
                if attempt < API_MAX_RETRY:
                    time.sleep(API_RETRY_DELAY_SEC * attempt)
            except Exception as exc:
                last_exc = exc
                logger.warning(f"POST {path} 실패 ({attempt}/{API_MAX_RETRY}): {exc}")
                if is_order:
                    logger.error("주문 API 재시도 금지 - 이중 주문 위험")
                    break
                if attempt < API_MAX_RETRY:
                    time.sleep(API_RETRY_DELAY_SEC * attempt)
        if set_api_error_on_exit:
            self._set_api_error()
        raise RuntimeError(f"POST {path} 실패: {last_exc}") from last_exc

    def _check_api_response(self, data: dict) -> None:
        rt_cd = data.get("rt_cd", "")
        if rt_cd != "0":
            msg_cd = data.get("msg_cd", "")
            msg    = data.get("msg1", "알 수 없는 오류")
            if msg_cd == "EGW00201":
                raise RateLimitError(f"초당 거래건수 초과 [{msg_cd}]: {msg}")
            # KIS 비즈니스 거부 (잔고/한도/호가 등) - 인프라 정상
            if msg_cd.startswith("4"):
                raise KISBusinessError(msg_cd, msg)
            raise RuntimeError(f"KIS API 오류 [{msg_cd}]: {msg}")

    def _make_hashkey(self, body: dict) -> str:
        url = f"{self._base_url}/uapi/hashkey"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        resp = requests.post(url, headers=headers, json=body, timeout=API_TIMEOUT_SEC)
        resp.raise_for_status()
        return resp.json()["HASH"]

    def _set_api_error(self) -> None:
        self._api_error = True
        self._last_error_time = datetime.now()
        logger.error("API 오류 플래그 설정 - 주문 중단")

    def is_api_healthy(self) -> bool:
        """API 오류 없음 여부 (주문 전 확인 필수)"""
        if not self._api_error:
            return True
        # 5분 경과 후 자동 복구 시도
        if self._last_error_time and (datetime.now() - self._last_error_time).seconds > 300:
            self._api_error = False
            logger.info("API 오류 플래그 해제 (5분 경과)")
            return True
        return False

    # ─────────────────────────────────────────────────────────────
    # 시세 조회
    # ─────────────────────────────────────────────────────────────

    def get_price(self, code: str, market: str = MARKET_KOSPI) -> PriceData:
        """국내주식 현재가 조회"""
        data = self._get(
            KIS_PATH_PRICE,
            self._tr["price"],
            {"fid_cond_mrkt_div_code": market, "fid_input_iscd": code},
        )
        o = data["output"]
        return PriceData(
            code=code,
            current_price=int(o["stck_prpr"]),
            open_price=int(o["stck_oprc"]),
            high_price=int(o["stck_hgpr"]),
            low_price=int(o["stck_lwpr"]),
            prev_close=int(o["stck_sdpr"]),
            volume=int(o["acml_vol"]),
            amount=int(o["acml_tr_pbmn"]),
            change_rate=float(o["prdy_ctrt"]),
            is_upper_limit=o["stck_prpr"] == o["stck_mxpr"],
            is_lower_limit=o["stck_prpr"] == o["stck_llam"],
        )

    def get_orderbook(self, code: str, market: str = MARKET_KOSPI) -> dict:
        """호가 조회 (매수/매도 10호가)"""
        data = self._get(
            KIS_PATH_ORDERBOOK,
            self._tr["orderbook"],
            {"fid_cond_mrkt_div_code": market, "fid_input_iscd": code},
        )
        return data.get("output1", {})

    def get_daily_ohlcv(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str = "D",
        market: str = MARKET_KOSPI,
        adj_price: bool = True,
    ) -> list[dict]:
        """
        일봉/주봉/월봉 조회
        period: D(일) | W(주) | M(월)
        start_date, end_date: YYYYMMDD 형식
        """
        data = self._get(
            KIS_PATH_DAILY_PRICE,
            self._tr["daily_price"],
            {
                "fid_cond_mrkt_div_code": market,
                "fid_input_iscd": code,
                "fid_input_date_1": start_date,
                "fid_input_date_2": end_date,
                "fid_period_div_code": period,
                "fid_org_adj_prc": "0" if adj_price else "1",
            },
        )
        rows = data.get("output2", [])
        result = []
        for r in rows:
            if not r.get("stck_bsop_date"):
                continue
            result.append({
                "date": r["stck_bsop_date"],
                "open": int(r["stck_oprc"]),
                "high": int(r["stck_hgpr"]),
                "low": int(r["stck_lwpr"]),
                "close": int(r["stck_clpr"]),
                "volume": int(r["acml_vol"]),
                "amount": int(r["acml_tr_pbmn"]),
            })
        return result

    def get_minute_ohlcv(
        self,
        code: str,
        time_str: str = "090000",
        market: str = MARKET_KOSPI,
    ) -> list[dict]:
        """분봉 조회 (당일)"""
        data = self._get(
            KIS_PATH_MINUTE_PRICE,
            self._tr["minute_price"],
            {
                "fid_etc_cls_code": "",
                "fid_cond_mrkt_div_code": market,
                "fid_input_iscd": code,
                "fid_input_hour_1": time_str,
                "fid_pw_data_incu_yn": "Y",
            },
        )
        rows = data.get("output2", [])
        result = []
        for r in rows:
            result.append({
                "time": r.get("stck_cntg_hour", ""),
                "open": int(r.get("stck_oprc", 0)),
                "high": int(r.get("stck_hgpr", 0)),
                "low": int(r.get("stck_lwpr", 0)),
                "close": int(r.get("stck_prpr", 0)),
                "volume": int(r.get("cntg_vol", 0)),
            })
        return result

    # ─────────────────────────────────────────────────────────────
    # 주문
    # ─────────────────────────────────────────────────────────────

    def buy(
        self,
        code: str,
        qty: int,
        price: int = 0,
        order_type: str = "00",
    ) -> OrderResult:
        """
        매수 주문
        order_type: 00=지정가, 01=시장가
        price=0 이면 시장가로 자동 전환
        """
        if not self.is_api_healthy():
            return OrderResult(success=False, message="API 오류 상태 - 주문 불가")

        if price == 0:
            order_type = "01"

        body = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._account_product,
            "PDNO": code,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        try:
            data = self._post(KIS_PATH_ORDER_BUY, self._tr["buy"], body)
            order_no = data.get("output", {}).get("ODNO", "")
            logger.info(f"매수 주문 완료 | {code} {qty}주 {price}원 | 주문번호={order_no}")
            return OrderResult(success=True, order_no=order_no, raw=data)
        except Exception as exc:
            logger.error(f"매수 주문 실패 | {code}: {exc}")
            return OrderResult(success=False, message=str(exc))

    def sell(
        self,
        code: str,
        qty: int,
        price: int = 0,
        order_type: str = "00",
        is_forced_close: bool = False,
    ) -> OrderResult:
        """
        매도 주문
        order_type: 00=지정가, 01=시장가
        is_forced_close: True면 시장가 강제 (price 무시, order_type 무시)
            forced_close 시그널은 호가 멀어진 상황도 즉시 청산해야 함.
        """
        if not self.is_api_healthy():
            return OrderResult(success=False, message="API 오류 상태 - 주문 불가")

        if is_forced_close:
            order_type = "01"  # 시장가
            price = 0
        elif price == 0:
            order_type = "01"

        body = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._account_product,
            "PDNO": code,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        try:
            data = self._post(KIS_PATH_ORDER_SELL, self._tr["sell"], body)
            order_no = data.get("output", {}).get("ODNO", "")
            tag = " (시장가 강제)" if is_forced_close else ""
            logger.info(
                f"매도 주문 완료{tag} | {code} {qty}주 {price}원 | 주문번호={order_no}"
            )
            return OrderResult(success=True, order_no=order_no, raw=data)
        except Exception as exc:
            logger.error(f"매도 주문 실패 | {code}: {exc}")
            return OrderResult(success=False, message=str(exc))

    def get_volume_rank(
        self,
        market: str = MARKET_KOSPI,
        rank_sort_class: str = "0",  # 0=전체, 1=관리종목제외 등
        top_n: int = 30,
    ) -> list[dict]:
        """
        거래대금/거래량 급증 종목 조회 (KIS volume-rank API).

        Returns:
            list of dict: [{"code", "name", "price", "prdy_vrss_rate",
                            "acml_tr_pbmn", "upbnd_yn", "lwbnd_yn"}, ...]
            prdy_vrss_rate: 전일 대비 거래량 비율 (%)
            acml_tr_pbmn: 누적 거래대금
        """
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",  # 0000=전체
            "FID_DIV_CLS_CODE": rank_sort_class,
            "FID_BLNG_CLS_CODE": "1",  # 1=거래대금 상위
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_INPUT_DATE_1": "",
        }
        try:
            data = self._get(
                KIS_PATH_VOLUME_RANK,
                self._tr["volume_rank"],
                params,
            )
        except Exception as exc:
            logger.warning(f"volume-rank 조회 실패: {exc}")
            return []

        rows = data.get("output", []) or []
        result = []
        for r in rows[:top_n]:
            try:
                result.append({
                    "code": r.get("mksc_shrn_iscd", "").strip(),
                    "name": r.get("hts_kor_isnm", "").strip(),
                    "price": int(r.get("stck_prpr", 0) or 0),
                    "prdy_vrss_rate": float(r.get("vol_inrt", 0) or 0),
                    "acml_tr_pbmn": int(r.get("acml_tr_pbmn", 0) or 0),
                    "upbnd_yn": r.get("uplmt_sign", "") in ("1", "Y"),
                    "lwbnd_yn": r.get("lslmt_sign", "") in ("1", "Y"),
                })
            except (ValueError, TypeError):
                continue
        return result

    def cancel_order(
        self,
        order_no: str,
        code: str,
        qty: int,
        price: int,
        order_type: str = "00",
    ) -> OrderResult:
        """주문 취소"""
        if not self.is_api_healthy():
            return OrderResult(success=False, message="API 오류 상태")

        body = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._account_product,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_no,
            "ORD_DVSN": order_type,
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "QTY_ALL_ORD_YN": "Y",
        }
        try:
            data = self._post(KIS_PATH_ORDER_CANCEL, self._tr["cancel"], body)
            logger.info(f"주문 취소 완료 | {order_no}")
            return OrderResult(success=True, order_no=order_no, raw=data)
        except Exception as exc:
            logger.error(f"주문 취소 실패 | {order_no}: {exc}")
            return OrderResult(success=False, message=str(exc))

    # ─────────────────────────────────────────────────────────────
    # 잔고 / 체결 조회
    # ─────────────────────────────────────────────────────────────

    def get_balance(self) -> Balance:
        """잔고 조회 (현금 + 보유 종목)"""
        data = self._get(
            KIS_PATH_BALANCE,
            self._tr["balance"],
            {
                "CANO": self._account_no,
                "ACNT_PRDT_CD": self._account_product,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "N",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        positions = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty == 0:
                continue
            positions.append(
                BalanceItem(
                    code=item.get("pdno", ""),
                    name=item.get("prdt_name", ""),
                    quantity=qty,
                    avg_price=float(item.get("pchs_avg_pric", 0)),
                    current_price=int(item.get("prpr", 0)),
                    eval_amount=int(item.get("evlu_amt", 0)),
                    profit_loss=int(item.get("evlu_pfls_amt", 0)),
                    profit_loss_rate=float(item.get("evlu_pfls_rt", 0)),
                )
            )

        summary = data.get("output2", [{}])[0]
        # 가용 현금 산정 우선순위:
        #   1) ord_psbl_cash (주문가능현금) — 일부 응답에 포함, 가장 정확
        #   2) prvs_rcdl_excc_amt (T+2 정산금액) — 정산 후 가용
        #   3) dnca_tot_amt - thdt_buy_amt + thdt_sll_amt — 예수금에서 당일
        #      매수/매도 차감 후 (모의 마진 매수 반영)
        # 모의투자는 dnca_tot_amt가 시작자본 그대로 유지되는 경우가 있어
        # 그것만 보면 과대 추정됨.
        ord_psbl = int(summary.get("ord_psbl_cash", 0) or 0)
        prvs_rcdl = int(summary.get("prvs_rcdl_excc_amt", 0) or 0)
        dnca = int(summary.get("dnca_tot_amt", 0) or 0)
        today_buy = int(summary.get("thdt_buy_amt", 0) or 0)
        today_sell = int(summary.get("thdt_sll_amt", 0) or 0)

        if ord_psbl > 0:
            cash = ord_psbl
        elif prvs_rcdl > 0:
            cash = prvs_rcdl
        else:
            cash = max(0, dnca - today_buy + today_sell)

        return Balance(
            cash=cash,
            total_eval=int(summary.get("tot_evlu_amt", 0) or 0),
            positions=positions,
        )

    def get_filled_orders(self, date: str = "") -> list[FilledOrder]:
        """당일 체결 조회 (date: YYYYMMDD, 기본=오늘)"""
        if not date:
            date = datetime.now().strftime("%Y%m%d")
        data = self._get(
            KIS_PATH_FILLED,
            self._tr["filled"],
            {
                "CANO": self._account_no,
                "ACNT_PRDT_CD": self._account_product,
                "INQR_STRT_DT": date,
                "INQR_END_DT": date,
                "SLL_BUY_DVSN_CD": "00",  # 00=전체
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "1",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        result = []
        for item in data.get("output1", []):
            filled_qty = int(item.get("tot_ccld_qty", 0))
            if filled_qty == 0:
                continue
            side = "BUY" if item.get("sll_buy_dvsn_cd", "") == "02" else "SELL"
            dt_str = item.get("ord_dt", "") + item.get("ord_tmd", "")
            try:
                filled_at = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
            except ValueError:
                filled_at = datetime.now()
            result.append(
                FilledOrder(
                    order_no=item.get("odno", ""),
                    code=item.get("pdno", ""),
                    name=item.get("prdt_name", ""),
                    side=side,
                    order_qty=int(item.get("ord_qty", 0)),
                    filled_qty=filled_qty,
                    order_price=int(item.get("ord_unpr", 0)),
                    avg_filled_price=float(item.get("avg_prvs", 0)),
                    filled_at=filled_at,
                    status=item.get("ord_stts", ""),
                )
            )
        return result

    def get_unfilled_orders(self) -> list[dict]:
        """미체결 주문 조회"""
        if self._is_mock:
            logger.warning("Mock mode does not support unfilled-order inquiry; returning an empty list.")
            return []

        data = self._get(
            KIS_PATH_UNFILLED,
            self._tr["unfilled"],
            {
                "CANO": self._account_no,
                "ACNT_PRDT_CD": self._account_product,
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "INQR_DVSN_1": "1",
                "INQR_DVSN_2": "0",
            },
        )
        result = []
        for item in data.get("output", []):
            remain_qty = int(item.get("psbl_qty", 0))
            if remain_qty == 0:
                continue
            result.append({
                "order_no": item.get("odno", ""),
                "code": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "side": "BUY" if item.get("sll_buy_dvsn_cd") == "02" else "SELL",
                "order_qty": int(item.get("ord_qty", 0)),
                "remain_qty": remain_qty,
                "order_price": int(item.get("ord_unpr", 0)),
                "order_type": item.get("ord_dvsn_name", ""),
                "order_time": item.get("ord_tmd", ""),
            })
        return result

    # ─────────────────────────────────────────────────────────────
    # WebSocket 실시간 구독
    # ─────────────────────────────────────────────────────────────

    def subscribe_realtime(
        self,
        codes: list[str],
        on_price: Callable[[str, dict], None],
        on_orderbook: Callable[[str, dict], None] | None = None,
    ) -> None:
        """
        실시간 체결가 (+ 선택적으로 호가) 구독 시작.

        이미 WebSocket이 연결돼 있으면 신규 종목만 추가 구독한다
        (KIS는 동일 approval_key 중복 연결을 강제 종료하므로 절대 새 WS를 열면 안 됨).
        """
        self._realtime_price_cb = on_price
        self._realtime_orderbook_cb = on_orderbook

        # 기존 연결 살아있으면 신규 종목만 추가 구독
        if (
            self._ws is not None
            and self._ws_thread is not None
            and self._ws_thread.is_alive()
            and self._ws_connected.is_set()
        ):
            new_codes = [c for c in codes if c not in self._subscribed_codes]
            if not new_codes:
                logger.debug(f"실시간 구독 변경 없음 (이미 구독 중): {codes}")
                return
            for code in new_codes:
                self._send_subscribe(code, self._tr["ws_real_price"])
                if on_orderbook:
                    self._send_subscribe(code, self._tr["ws_orderbook"])
                self._subscribed_codes.add(code)
            logger.info(f"실시간 구독 추가: {new_codes}")
            return

        # 신규 연결
        self._realtime_codes = list(codes)
        self._subscribed_codes = set()

        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self._ws_thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        self._ws_thread.start()
        connected = self._ws_connected.wait(timeout=15)
        if not connected:
            raise RuntimeError("WebSocket 연결 타임아웃")
        logger.info(f"실시간 구독 시작: {codes}")

    def unsubscribe_all(self) -> None:
        self._ws_intentional_close = True
        if self._ws:
            self._ws.close()

    def _on_ws_open(self, ws) -> None:
        self._ws_connected.set()
        logger.info("WebSocket 연결됨")
        for code in self._realtime_codes:
            self._send_subscribe(code, self._tr["ws_real_price"])
            if self._realtime_orderbook_cb:
                self._send_subscribe(code, self._tr["ws_orderbook"])
            self._subscribed_codes.add(code)

    def _send_subscribe(self, code: str, tr_id: str) -> None:
        payload = {
            "header": {
                "approval_key": self._ws_approval_key,
                "custtype": "P",
                "tr_type": "1",  # 1=등록
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": code,
                }
            },
        }
        self._ws.send(json.dumps(payload))

    def _on_ws_message(self, ws, message: str) -> None:
        try:
            # PINGPONG 처리
            if message.startswith("0|") or message.startswith("1|"):
                self._parse_realtime_data(message)
                return
            data = json.loads(message)
            # 구독 응답 처리
            if data.get("header", {}).get("tr_id") in ("PINGPONG",):
                ws.send(message)
        except Exception as exc:
            logger.warning(f"WebSocket 메시지 파싱 오류: {exc}")

    def _parse_realtime_data(self, raw: str) -> None:
        """
        실시간 데이터 파싱
        형식: "0|{TR_ID}|{COUNT}|{DATA}"
        """
        try:
            parts = raw.split("|")
            if len(parts) < 4:
                return
            tr_id = parts[1]
            data_str = parts[3]

            if tr_id == self._tr["ws_real_price"]:
                self._parse_price_data(data_str)
            elif tr_id == self._tr["ws_orderbook"]:
                self._parse_orderbook_data(data_str)
        except Exception as exc:
            logger.warning(f"실시간 데이터 파싱 오류: {exc}")

    @staticmethod
    def _to_int(s: str, default: int = 0) -> int:
        """KIS 실시간 필드 안전 정수 변환 (장전/시간외에 '0.00' 같은 소수 문자열로 올 수 있음)"""
        try:
            return int(s)
        except (ValueError, TypeError):
            try:
                return int(float(s))
            except (ValueError, TypeError):
                return default

    @staticmethod
    def _to_float(s: str, default: float = 0.0) -> float:
        try:
            return float(s)
        except (ValueError, TypeError):
            return default

    def _parse_price_data(self, data_str: str) -> None:
        """H0STCNT0 체결가 파싱 (^구분자)"""
        fields = data_str.split("^")
        if len(fields) < 13:
            return
        code = fields[0]
        parsed = {
            "code": code,
            "current_price": self._to_int(fields[2]),
            "change_sign": fields[3],           # 1=상한, 2=상승, 3=보합, 4=하한, 5=하락
            "change_price": self._to_int(fields[4]),
            "change_rate": self._to_float(fields[5]),
            "volume": self._to_int(fields[8]),
            "total_volume": self._to_int(fields[9]),
            "high": self._to_int(fields[6]),
            "low": self._to_int(fields[7]),
            "open": self._to_int(fields[10]),
            "timestamp": fields[1],
        }
        if hasattr(self, "_realtime_price_cb") and self._realtime_price_cb:
            self._realtime_price_cb(code, parsed)

    def _parse_orderbook_data(self, data_str: str) -> None:
        """
        H0STASP0 호가 파싱 (^구분자, KIS 명세).
        Layout:
          0: 종목코드, 1: 영업시간, 2: 시간구분
          3~12: 매도호가 1~10, 13~22: 매수호가 1~10
          23~32: 매도호가잔량, 33~42: 매수호가잔량
          43: 총매도호가잔량, 44: 총매수호가잔량
        """
        fields = data_str.split("^")
        if len(fields) < 45:
            return
        code = fields[0]
        parsed = {
            "code": code,
            "ask1": self._to_int(fields[3]),
            "bid1": self._to_int(fields[13]),
            "ask_qty_sum": self._to_int(fields[43]),
            "bid_qty_sum": self._to_int(fields[44]),
            "timestamp": fields[1],
        }
        if hasattr(self, "_realtime_orderbook_cb") and self._realtime_orderbook_cb:
            self._realtime_orderbook_cb(code, parsed)

    def _on_ws_error(self, ws, error) -> None:
        # WS 오류는 REST 주문과 독립적 인프라이므로 _api_error 플래그를
        # 건드리지 않는다. 자동 재연결 로직이 별도로 처리.
        logger.error(f"WebSocket 오류: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg) -> None:
        logger.warning(f"WebSocket 연결 종료: {close_status_code} {close_msg}")
        self._ws_connected.clear()
        self._subscribed_codes.clear()
        # 의도적 종료가 아니면 재연결 스케줄
        if not self._ws_intentional_close:
            threading.Thread(
                target=self._reconnect_loop,
                daemon=True,
                name="ws-reconnect",
            ).start()

    def _reconnect_loop(self) -> None:
        """WebSocket 끊김 시 지수 백오프 재연결 (최대 5회)."""
        max_attempts = 5
        while self._ws_reconnect_attempts < max_attempts and not self._ws_intentional_close:
            self._ws_reconnect_attempts += 1
            backoff = min(2 ** self._ws_reconnect_attempts, 60)
            logger.info(
                f"WebSocket 재연결 시도 {self._ws_reconnect_attempts}/{max_attempts} "
                f"({backoff}s 후)"
            )
            time.sleep(backoff)
            if self._ws_intentional_close:
                return
            try:
                # 토큰/승인키 만료 가능성 → 갱신
                self._ensure_token()
                if self._ws_approval_key is None:
                    self._get_ws_approval_key()
                # 신규 WebSocketApp 생성하여 재구독
                codes = list(self._realtime_codes)
                self._ws_connected.clear()
                self._subscribed_codes.clear()
                self._ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                )
                self._ws_thread = threading.Thread(
                    target=self._ws.run_forever,
                    kwargs={"ping_interval": 30, "ping_timeout": 10},
                    daemon=True,
                    name="ws-thread",
                )
                self._ws_thread.start()
                if self._ws_connected.wait(timeout=15):
                    logger.info(f"WebSocket 재연결 성공 ({len(codes)}종목 재구독)")
                    self._ws_reconnect_attempts = 0
                    # 이전 버전에서 WS 오류로 _api_error가 설정됐다면 해제
                    self._api_error = False
                    return
                logger.warning("WebSocket 재연결 타임아웃")
            except Exception as exc:
                logger.error(f"WebSocket 재연결 오류: {exc}")
        logger.error(
            f"WebSocket 재연결 실패 ({max_attempts}회 초과) - 실시간 시세 중단"
        )
