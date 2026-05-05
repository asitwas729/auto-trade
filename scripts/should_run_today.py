"""오늘이 KRX 거래일인지 확인. 휴장일이면 exit 1, 거래일이면 exit 0.

GHA workflow 게이트로 사용:
    python scripts/should_run_today.py && python main.py

추가로 발급받은 KIS 토큰을 data/.kis_token_<mode>.json에 캐시해
바로 이어 실행되는 main.py가 EGW00133(분당 1회 한도)을 회피하도록 함.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

KIS_BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"
KIS_BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"


def _say(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    today = datetime.now()
    today_str = today.strftime("%Y%m%d")

    # 1. 주말 빠른 컷
    if today.weekday() >= 5:
        _say(f"[gate] {today_str} 주말 ({today.strftime('%A')}) - 매매 스킵")
        return 1

    mode = os.getenv("TRADING_MODE", "mock").strip()
    is_mock_or_paper = mode in ("mock", "paper")
    base_url = KIS_BASE_URL_MOCK if is_mock_or_paper else KIS_BASE_URL_REAL

    if is_mock_or_paper:
        app_key = os.getenv("KIS_MOCK_APP_KEY", "").strip()
        app_secret = os.getenv("KIS_MOCK_APP_SECRET", "").strip()
    else:
        app_key = os.getenv("KIS_REAL_APP_KEY", "").strip()
        app_secret = os.getenv("KIS_REAL_APP_SECRET", "").strip()

    if not app_key or not app_secret:
        _say("[gate] KIS 자격증명 미설정 - 안전을 위해 매매 진행 (휴장 체크 스킵)")
        return 0

    # 2. 토큰 발급 + 디스크 캐시 (main.py 재발급 회피)
    try:
        resp = requests.post(
            f"{base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": app_key,
                "appsecret": app_secret,
            },
            timeout=10,
        )
        if resp.status_code == 403:
            _say("[gate] 토큰 분당 한도(EGW00133) - 매매 진행 (캐시 토큰 사용 예정)")
            return 0
        resp.raise_for_status()
        token_data = resp.json()
        access_token = token_data["access_token"]
        expires_at = today + timedelta(seconds=int(token_data["expires_in"]))

        cache_path = Path("data") / f".kis_token_{mode}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "access_token": access_token,
            "token_type": token_data.get("token_type", "Bearer"),
            "expires_at": expires_at.isoformat(),
        }), encoding="utf-8")
        _say(f"[gate] 토큰 캐시 저장: {cache_path}")
    except Exception as exc:
        _say(f"[gate] 토큰 발급 실패: {exc} - 매매 진행")
        return 0

    # 3. KIS 휴장일 조회 (TR: CTCA0903R)
    try:
        resp = requests.get(
            f"{base_url}/uapi/domestic-stock/v1/quotations/chk-holiday",
            headers={
                "authorization": f"Bearer {access_token}",
                "appkey": app_key,
                "appsecret": app_secret,
                "tr_id": "CTCA0903R",
                "custtype": "P",
            },
            params={
                "BASS_DT": today_str,
                "CTX_AREA_NK": "",
                "CTX_AREA_FK": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            _say(f"[gate] 휴장일 조회 실패: {data.get('msg1', data)} - 매매 진행")
            return 0

        rows = data.get("output", []) or []
        for row in rows:
            if row.get("bass_dt") != today_str:
                continue
            opnd_yn = row.get("opnd_yn", "")  # KRX 개장일 여부
            bzdy_yn = row.get("bzdy_yn", "")  # 영업일 여부
            if opnd_yn == "Y":
                _say(f"[gate] {today_str} 거래일 (개장 Y, 영업 {bzdy_yn}) - 매매 진행")
                return 0
            else:
                _say(f"[gate] {today_str} 휴장일 (개장 {opnd_yn}, 영업 {bzdy_yn}) - 매매 스킵")
                return 1

        _say(f"[gate] {today_str} 응답에 정보 없음 - 매매 진행")
        return 0
    except Exception as exc:
        _say(f"[gate] 휴장일 조회 오류: {exc} - 매매 진행")
        return 0


if __name__ == "__main__":
    sys.exit(main())
