"""
KIS API connection tests

Run:
    python -m pytest tests/test_kis_api.py -v -s

Notes:
    - Credentials are selected by config.settings.settings.TRADING_MODE.
    - Account-dependent tests require the active mode account number.
"""

from datetime import datetime, timedelta

import pytest

from config.settings import settings


def _has_active_api_credentials() -> bool:
    if settings.is_live:
        return bool(settings.KIS_REAL_APP_KEY and settings.KIS_REAL_APP_SECRET)
    return bool(settings.KIS_MOCK_APP_KEY and settings.KIS_MOCK_APP_SECRET)


def _has_active_account() -> bool:
    if settings.is_live:
        return bool(settings.KIS_REAL_ACCOUNT_NO)
    return bool(settings.KIS_MOCK_ACCOUNT_NO)


pytestmark = pytest.mark.skipif(
    not _has_active_api_credentials(),
    reason="현재 TRADING_MODE 기준 KIS 앱키/시크릿이 없음 - .env 설정 필요",
)


@pytest.fixture(scope="module")
def client():
    from core.kis_api_client import KISApiClient

    c = KISApiClient()
    c.init()
    return c


def test_token_issued(client):
    """토큰 발급 확인"""
    assert client._token is not None
    assert client._token.access_token != ""
    print(f"\n토큰 만료: {client._token.expires_at}")


def test_get_price_samsung(client):
    """삼성전자 현재가 조회"""
    price = client.get_price("005930")
    assert price.current_price > 0
    assert price.code == "005930"
    print(f"\n삼성전자: 현재가={price.current_price:,}원 | 등락률={price.change_rate:+.2f}%")


def test_get_daily_ohlcv(client):
    """삼성전자 일봉 조회"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    rows = client.get_daily_ohlcv("005930", start, end)
    assert len(rows) > 0
    print(f"\n일봉 {len(rows)}건 | 최근: {rows[-1]}")


def test_get_orderbook(client):
    """삼성전자 호가 조회"""
    orderbook = client.get_orderbook("005930")
    assert isinstance(orderbook, dict)
    print(f"\n호가 수신 키: {list(orderbook.keys())[:5]}")


@pytest.mark.skipif(
    not _has_active_account(),
    reason="현재 TRADING_MODE 기준 계좌번호가 없음 - .env 설정 필요",
)
def test_get_balance(client):
    """잔고 조회"""
    client._api_error = False

    balance = client.get_balance()
    assert balance.cash >= 0
    assert balance.total_eval >= 0
    print(f"\n예수금: {balance.cash:,}원 | 총 평가: {balance.total_eval:,}원")
    print(f"보유 종목: {len(balance.positions)}개")


@pytest.mark.skipif(
    not _has_active_account(),
    reason="현재 TRADING_MODE 기준 계좌번호가 없음 - .env 설정 필요",
)
def test_get_unfilled_orders(client):
    """미체결 주문 조회"""
    client._api_error = False

    orders = client.get_unfilled_orders()
    assert isinstance(orders, list)
    print(f"\n미체결 주문: {len(orders)}건")


def test_api_health_independent(client):
    """API 상태 체크"""
    client._api_error = False
    client._last_error_time = None
    assert client.is_api_healthy() is True
    print("\nAPI 상태: 정상")


def test_print_account_info(client):
    """현재 연결 계좌 정보 출력"""
    print(f"\n계좌번호: {client._account_no}")
    print(f"모드: {'모의투자' if client._is_mock else '실전투자'}")
    print(f"서버: {client._base_url}")
    assert client._account_no != ""
