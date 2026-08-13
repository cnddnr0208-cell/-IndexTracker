"""
국내 지표(키움 REST API) 수집 - 집 PC 전용 모듈
--------------------------------------------------
키움 계좌에 걸려 있는 "지정단말기(IP)" 보안 설정 때문에, 등록된 집 PC의
IP에서만 로그인이 됩니다. 그래서 Render(클라우드) 서버는 이 모듈을 쓰지 않고,
집 PC에서 주기 실행하는 local_collector.py가 이 모듈로 국내 지수/수급/거래대금을
수집한 뒤 Upstash Redis에 저장해둡니다. app.py(Render)는 저장된 값을 읽기만
합니다 - snapshot_store.DOMESTIC_SNAPSHOT_KEY / app.py의 DOMESTIC_SNAPSHOT_KEY와
반드시 같은 문자열("domestic_snapshot")을 씁니다.

내부 함수(collect_domestic/collect_domestic_history)는 원래 app.py에 있던
로직을 그대로 옮겨온 것입니다.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from kiwoom_client import KiwoomClient

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

KIWOOM_MARKET_CODES = {"kospi": "001", "kosdaq": "101"}

# app.py의 DOMESTIC_SNAPSHOT_KEY와 반드시 동일해야 함
DOMESTIC_SNAPSHOT_KEY = "domestic_snapshot"

# app.py의 INTRADAY_LOG_INTERVAL_MINUTES와 반드시 동일해야 함 (시계열 로그를
# app.py가 읽을 때 같은 15분 단위로 시각을 맞춰야 하기 때문)
INTRADAY_LOG_INTERVAL_MINUTES = 15
INTRADAY_LOG_RETENTION_DAYS = 25  # 20일 평균 + 여유분

_INTRADAY_FIELDS = (
    "kospi_index", "kosdaq_index",
    "kospi_investor_netbuy", "kosdaq_investor_netbuy",
    "kospi_top_trading_value", "kosdaq_top_trading_value",
)


def is_placeholder(v: Optional[str]) -> bool:
    return not v


def collect_domestic() -> dict:
    app_key = os.environ.get("KIWOOM_APP_KEY")
    app_secret = os.environ.get("KIWOOM_APP_SECRET")
    if is_placeholder(app_key) or is_placeholder(app_secret):
        logger.warning("KIWOOM_APP_KEY/SECRET 미설정 - 국내 지표 생략")
        return {}
    is_mock = os.environ.get("KIWOOM_IS_MOCK", "false").lower() == "true"
    client = KiwoomClient(app_key, app_secret, is_mock)
    client.login()
    try:
        data = {}
        for market_name, code in KIWOOM_MARKET_CODES.items():
            data[f"{market_name}_index"] = client.get_index_price(code)
            data[f"{market_name}_investor_netbuy"] = client.get_investor_net_buy(code)
            data[f"{market_name}_top_trading_value"] = client.get_top_trading_value(code, top_n=5)
        return data
    finally:
        client.logout()


def collect_domestic_history(current: dict) -> dict:
    """
    국내 수급/거래대금 상위 항목의 "전일 마감" · "최근 20일 평균(마감 기준)" 비교용
    이력 데이터. (자세한 설명은 kiwoom_client.py / README 참고)
    """
    app_key = os.environ.get("KIWOOM_APP_KEY")
    app_secret = os.environ.get("KIWOOM_APP_SECRET")
    if is_placeholder(app_key) or is_placeholder(app_secret):
        return {}
    is_mock = os.environ.get("KIWOOM_IS_MOCK", "false").lower() == "true"
    client = KiwoomClient(app_key, app_secret, is_mock)
    client.login()
    try:
        data = {}
        today_kst_str = datetime.now(KST).date().strftime("%Y%m%d")
        yesterday_str = (datetime.now(KST).date() - timedelta(days=1)).strftime("%Y%m%d")
        for market_name, code in KIWOOM_MARKET_CODES.items():
            data[f"{market_name}_investor_netbuy_history"] = client.get_investor_net_buy_history(
                code, days=20, end_date=today_kst_str
            )

            stock_hist = {}
            for stock in (current.get(f"{market_name}_top_trading_value") or [])[:5]:
                stk_cd = stock.get("code")
                if not stk_cd:
                    continue
                stock_hist[stk_cd] = client.get_stock_trading_value_history(stk_cd, yesterday_str, days=20)
            data[f"{market_name}_top_trading_value_history"] = stock_hist
        return data
    finally:
        client.logout()


def _round_to_interval(now: datetime, minutes: int = INTRADAY_LOG_INTERVAL_MINUTES) -> str:
    total_min = now.hour * 60 + now.minute
    rounded = (total_min // minutes) * minutes
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def log_intraday_snapshot(store, current: dict, now: datetime) -> None:
    """국내 지수/수급/거래대금 상위를 시각과 함께 오늘자 로그에 추가 (이미 같은
    15분 구간에 기록했으면 건너뜀). app.py가 "전일 동시각"/"최근 20일 동시각
    평균" 비교에 이 로그를 읽어서 씁니다."""
    if not any(current.get(f) for f in _INTRADAY_FIELDS):
        return

    date_key = now.date().isoformat()
    time_key = _round_to_interval(now)
    log_store_key = f"tslog__{date_key}"
    day_log = store.get(log_store_key) or []
    if day_log and day_log[-1].get("time") == time_key:
        return

    snapshot = {"time": time_key}
    for f in _INTRADAY_FIELDS:
        snapshot[f] = current.get(f)
    day_log.append(snapshot)
    store.put(log_store_key, day_log)

    idx = store.get("tslog_dates") or []
    if date_key not in idx:
        idx.append(date_key)
        cutoff = (now.date() - timedelta(days=INTRADAY_LOG_RETENTION_DAYS)).isoformat()
        idx = sorted(d for d in idx if d >= cutoff)
        store.put("tslog_dates", idx)


def collect_and_publish(store) -> Optional[dict]:
    """
    국내 데이터를 키움 API로 수집해서 Redis에 저장하고(app.py가 읽어감),
    시계열 로그도 남긴다. local_collector.py가 주기적으로 호출한다.

    반환: 수집된 current dict (키움 로그인/조회 자체가 실패해 아무 값도 못
    얻었으면 None).
    """
    now = datetime.now(KST)
    current = collect_domestic()
    if not current:
        return None

    try:
        current.update(collect_domestic_history(current))
    except Exception:
        logger.exception("국내 수급/거래대금 이력(전일·20일) 수집 중 오류")

    store.put(DOMESTIC_SNAPSHOT_KEY, {"current": current, "fetched_at": now.isoformat()})

    try:
        log_intraday_snapshot(store, current, now)
    except Exception:
        logger.exception("자체 시계열 로그 기록 중 오류")

    return current
