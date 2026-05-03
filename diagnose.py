"""
KIS API 연결 진단 스크립트
python diagnose.py
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# settings를 통해 모드에 맞는 키 자동 선택
from config.settings import settings

APP_KEY    = settings.app_key
APP_SECRET = settings.app_secret
ACCOUNT    = settings.account_no
MODE       = settings.TRADING_MODE

MOCK_URL = "https://openapivts.koreainvestment.com:29443"
REAL_URL = "https://openapi.koreainvestment.com:9443"
BASE_URL  = MOCK_URL if not settings.is_live else REAL_URL

OK   = "[OK]"
ERR  = "[FAIL]"
WARN = "[WARN]"

print("=" * 60)
print("KIS API 연결 진단")
print("=" * 60)
print(f"  TRADING_MODE : {MODE}")
print(f"  BASE_URL     : {BASE_URL}")
print(f"  APP_KEY      : {APP_KEY[:10]}...")
print(f"  ACCOUNT_NO   : {ACCOUNT}")
print()


def test_token(base_url: str, app_key: str, app_secret: str, label: str) -> dict | None:
    url = f"{base_url}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }
    print(f"[{label}] 토큰 요청 -> {url}")
    try:
        resp = requests.post(url, json=body, timeout=10)
        print(f"  HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:200]}

        if resp.ok:
            print(f"  {OK} 토큰 발급 성공 | 만료: {data.get('access_token_token_expired', '?')}")
            return data
        else:
            print(f"  {ERR} 토큰 발급 실패: {data}")
            return None
    except Exception as e:
        print(f"  {ERR} 연결 오류: {e}")
        return None


print("-" * 60)
print(f"1. 현재 모드({MODE}) 토큰 테스트")
token_data = test_token(BASE_URL, APP_KEY, APP_SECRET, MODE)

if not token_data:
    print()
    print(f"{ERR} 토큰 발급 실패 -> 아래를 확인하세요")
    if not settings.is_live:
        print("   - .env의 KIS_MOCK_APP_KEY / KIS_MOCK_APP_SECRET 확인")
        print("   - KIS API 포털에서 모의투자 앱키로 등록된 키인지 확인")
    else:
        print("   - .env의 KIS_REAL_APP_KEY / KIS_REAL_APP_SECRET 확인")
    sys.exit(1)

token_str = token_data.get("access_token", "")
headers = {
    "authorization": f"Bearer {token_str}",
    "appkey": APP_KEY,
    "appsecret": APP_SECRET,
    "content-type": "application/json",
}

time.sleep(1)
print()
print("-" * 60)
print("2. 시세 조회 테스트 (삼성전자 005930)")
headers["tr_id"] = "FHKST01010100"
params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
try:
    r = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=headers, params=params, timeout=10
    )
    d = r.json()
    if d.get("rt_cd") == "0":
        out = d["output"]
        print(f"  {OK} 현재가: {int(out['stck_prpr']):,}원 | 등락률: {out['prdy_ctrt']}%")
    else:
        print(f"  {ERR} 시세 오류: {d.get('msg1')}")
except Exception as e:
    print(f"  {ERR} 오류: {e}")

time.sleep(1)
print()
print("-" * 60)
print(f"3. 잔고 조회 테스트 (계좌: {ACCOUNT})")
tr_id = "VTTC8434R" if not settings.is_live else "TTTC8434R"
headers["tr_id"] = tr_id
params2 = {
    "CANO": ACCOUNT,
    "ACNT_PRDT_CD": settings.KIS_ACCOUNT_PRODUCT,
    "AFHR_FLPR_YN": "N",
    "OFL_YN": "N",
    "INQR_DVSN": "02",
    "UNPR_DVSN": "01",
    "FUND_STTL_ICLD_YN": "N",
    "FNCG_AMT_AUTO_RDPT_YN": "N",
    "PRCS_DVSN": "00",
    "CTX_AREA_FK100": "",
    "CTX_AREA_NK100": "",
}
try:
    r = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers=headers, params=params2, timeout=10
    )
    print(f"  HTTP {r.status_code}")
    d = r.json()
    if d.get("rt_cd") == "0":
        summary = d.get("output2", [{}])[0]
        cash  = int(summary.get("ord_psbl_cash", 0))
        total = int(summary.get("tot_evlu_amt", 0))
        positions = [p for p in d.get("output1", []) if int(p.get("hldg_qty", 0)) > 0]
        print(f"  {OK} 잔고 조회 성공")
        print(f"     주문가능현금: {cash:,}원")
        print(f"     총 평가금액: {total:,}원")
        print(f"     보유 종목:   {len(positions)}개")
    else:
        print(f"  {ERR} 잔고 오류: {d.get('msg1')} (rt_cd={d.get('rt_cd')})")
        print(f"     msg_cd: {d.get('msg_cd')}")
except Exception as e:
    print(f"  {ERR} 오류: {e}")

time.sleep(1)
print()
print("-" * 60)
print("4. 미체결 주문 조회 테스트")
tr_id2 = "VTTC8036R" if not settings.is_live else "TTTC8036R"
headers["tr_id"] = tr_id2
params3 = {
    "CANO": ACCOUNT,
    "ACNT_PRDT_CD": settings.KIS_ACCOUNT_PRODUCT,
    "CTX_AREA_FK100": "",
    "CTX_AREA_NK100": "",
    "INQR_DVSN_1": "1",
    "INQR_DVSN_2": "0",
}
try:
    r = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
        headers=headers, params=params3, timeout=10
    )
    print(f"  HTTP {r.status_code}")
    d = r.json()
    if d.get("rt_cd") == "0":
        orders = d.get("output", [])
        print(f"  {OK} 미체결 조회 성공 | {len(orders)}건")
    else:
        print(f"  {ERR} 미체결 오류: {d.get('msg1')} (rt_cd={d.get('rt_cd')})")
except Exception as e:
    print(f"  {ERR} 오류: {e}")

print()
print("=" * 60)
print("진단 완료")
