"""
환경변수 기반 설정 로더

KIS Open API 키 구조:
  - 모의투자(mock/paper): KIS_MOCK_APP_KEY + KIS_MOCK_APP_SECRET + KIS_MOCK_ACCOUNT_NO
  - 실전투자(live):       KIS_REAL_APP_KEY + KIS_REAL_APP_SECRET + KIS_REAL_ACCOUNT_NO
  각각 KIS API 포털에서 별도 발급
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"필수 환경변수 누락: {key}")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


class Settings:
    # ─── 운영 모드 ────────────────────────────────────────────────
    TRADING_MODE: str = _optional("TRADING_MODE", "mock")

    # ─── 모의투자 자격증명 ────────────────────────────────────────
    KIS_MOCK_APP_KEY: str     = _optional("KIS_MOCK_APP_KEY")
    KIS_MOCK_APP_SECRET: str  = _optional("KIS_MOCK_APP_SECRET")
    KIS_MOCK_ACCOUNT_NO: str  = _optional("KIS_MOCK_ACCOUNT_NO")

    # ─── 실전투자 자격증명 ────────────────────────────────────────
    KIS_REAL_APP_KEY: str     = _optional("KIS_REAL_APP_KEY")
    KIS_REAL_APP_SECRET: str  = _optional("KIS_REAL_APP_SECRET")
    KIS_REAL_ACCOUNT_NO: str  = _optional("KIS_REAL_ACCOUNT_NO")

    KIS_ACCOUNT_PRODUCT: str  = _optional("KIS_ACCOUNT_PRODUCT", "01")

    # ─── 모드별 자격증명 프로퍼티 ────────────────────────────────
    @property
    def is_mock(self) -> bool:
        return self.TRADING_MODE == "mock"

    @property
    def is_paper(self) -> bool:
        return self.TRADING_MODE == "paper"

    @property
    def is_live(self) -> bool:
        return self.TRADING_MODE == "live"

    @property
    def app_key(self) -> str:
        if self.is_live:
            if not self.KIS_REAL_APP_KEY:
                raise EnvironmentError("실전투자 모드: KIS_REAL_APP_KEY 필요")
            return self.KIS_REAL_APP_KEY
        else:
            if not self.KIS_MOCK_APP_KEY:
                raise EnvironmentError("모의투자 모드: KIS_MOCK_APP_KEY 필요")
            return self.KIS_MOCK_APP_KEY

    @property
    def app_secret(self) -> str:
        if self.is_live:
            if not self.KIS_REAL_APP_SECRET:
                raise EnvironmentError("실전투자 모드: KIS_REAL_APP_SECRET 필요")
            return self.KIS_REAL_APP_SECRET
        else:
            if not self.KIS_MOCK_APP_SECRET:
                raise EnvironmentError("모의투자 모드: KIS_MOCK_APP_SECRET 필요")
            return self.KIS_MOCK_APP_SECRET

    @property
    def account_no(self) -> str:
        if self.is_live:
            if not self.KIS_REAL_ACCOUNT_NO:
                raise EnvironmentError("실전투자 모드: KIS_REAL_ACCOUNT_NO 필요")
            return self.KIS_REAL_ACCOUNT_NO
        else:
            if not self.KIS_MOCK_ACCOUNT_NO:
                raise EnvironmentError("모의투자 모드: KIS_MOCK_ACCOUNT_NO 필요")
            return self.KIS_MOCK_ACCOUNT_NO

    # ─── 알림 ────────────────────────────────────────────────────
    SLACK_WEBHOOK_URL: str    = _optional("SLACK_WEBHOOK_URL")
    TELEGRAM_BOT_TOKEN: str   = _optional("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID: str     = _optional("TELEGRAM_CHAT_ID")

    # ─── 경로 ────────────────────────────────────────────────────
    DATA_DIR: Path            = BASE_DIR / "data"
    LOG_DIR: Path             = BASE_DIR / "logs"
    REPORT_DIR: Path          = BASE_DIR / "data" / "reports"
    KILL_SWITCH_FILE: Path    = BASE_DIR / "KILL_SWITCH"

    # ─── 전략 ON/OFF ─────────────────────────────────────────────
    STRATEGY_S1_ENABLED: bool = _optional("STRATEGY_S1_ENABLED", "true").lower() == "true"
    STRATEGY_S2_ENABLED: bool = _optional("STRATEGY_S2_ENABLED", "true").lower() == "true"
    STRATEGY_S3_ENABLED: bool = _optional("STRATEGY_S3_ENABLED", "true").lower() == "true"
    STRATEGY_S5_ENABLED: bool = _optional("STRATEGY_S5_ENABLED", "true").lower() == "true"
    # 시간대별 신규 전략 (단계적 활성화)
    STRATEGY_S6_ENABLED: bool = _optional("STRATEGY_S6_ENABLED", "true").lower() == "true"
    STRATEGY_S7_ENABLED: bool = _optional("STRATEGY_S7_ENABLED", "true").lower() == "true"
    STRATEGY_S8_ENABLED: bool = _optional("STRATEGY_S8_ENABLED", "true").lower() == "true"
    STRATEGY_S9_ENABLED: bool = _optional("STRATEGY_S9_ENABLED", "true").lower() == "true"

    # ─── Naver Open API (뉴스 수집용) ────────────────────────────
    NAVER_CLIENT_ID: str     = _optional("NAVER_CLIENT_ID")
    NAVER_CLIENT_SECRET: str = _optional("NAVER_CLIENT_SECRET")

    # ─── 초기 투자금 ─────────────────────────────────────────────
    INITIAL_CAPITAL: float    = float(_optional("INITIAL_CAPITAL", "10000000"))


settings = Settings()
