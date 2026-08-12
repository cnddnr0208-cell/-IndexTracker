"""
지수 알리미 웹 대시보드
------------------------
FastAPI 기반 웹 서버. 브라우저(PC/모바일)로 접속하면 국내/해외 지수, 코스피200 야간선물 등을
현재값 + 기준점 대비 변동률로 보여줍니다. 인터넷 어디서나 접속 가능하도록 클라우드(Render 등)
배포를 염두에 두고 설정을 전부 환경변수로 받습니다.

실시간 갱신은 WebSocket(/ws)으로 서버가 주기적으로(기본 20초) 새 값을 밀어줍니다.
연결이 끊기면 브라우저가 자동 재연결을 시도하고, 계속 실패하면 REST 폴링(/api/report)으로
자동 전환됩니다. (완전한 틱 단위 실시간은 아니며, 원본 데이터 소스 갱신 주기 + 20초 캐시가
체감 지연입니다. 원본 자체가 실시간이 아닌 항목도 있습니다 - 아래 "확인이 필요한 부분" 참고)

로컬 실행:
  uvicorn app:app --host 0.0.0.0 --port 8000
  (같은 Wi-Fi의 다른 기기에서는 http://<이 PC의 사설IP>:8000 으로 접속)

클라우드 배포(Render 등):
  시작 명령어: uvicorn app:app --host 0.0.0.0 --port $PORT
  환경변수 설정은 README.md의 "웹 대시보드 배포" 섹션 참고.

필요한 환경변수:
  KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK(true/false)
  ECOS_ENABLED(true/false), ECOS_API_KEY
  NIGHT_FUTURES_ENABLED(true/false, 기본 false - 무료 스크래핑 소스가 전부 막혀서 기본 꺼짐)
  DASHBOARD_USERNAME, DASHBOARD_PASSWORD   (설정 안 하면 인증 없이 열림 - 로컬 테스트만 권장)
  UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN  (없으면 로컬 파일로 폴백)
"""

import asyncio
import base64
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Optional

try:
    from dotenv import load_dotenv  # 로컬 테스트 편의용 (선택 설치)

    load_dotenv()
except ImportError:
    pass

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ecos_client import get_kr_3y_bond_yield
from kiwoom_client import KiwoomClient
from night_futures_client import get_kospi200_night_futures
from overseas_client import OverseasClient
from snapshot_store import SnapshotStore
from snapshot_store_redis import RedisSnapshotStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

OVERSEAS_ASSETS = [
    {"key": "USTECH", "label": "US Tech 100 (나스닥100 선물)", "ticker": "NQ=F"},
    {"key": "US500", "label": "S&P 500 선물", "ticker": "ES=F"},
    {"key": "JP225", "label": "닛케이225", "ticker": "^N225"},
    {"key": "SOX", "label": "필라델피아 반도체지수", "ticker": "^SOX"},
    {"key": "VIX", "label": "CBOE 변동성지수(VIX)", "ticker": "^VIX"},
    {"key": "DXY", "label": "달러 인덱스(DXY)", "ticker": "DX-Y.NYB"},
    {"key": "USDKRW", "label": "달러/원 환율", "ticker": "KRW=X"},
    {"key": "WTI", "label": "WTI유 선물", "ticker": "CL=F"},
    {"key": "BRENT", "label": "브렌트유 선물", "ticker": "BZ=F"},
    {"key": "BTCUSD", "label": "비트코인/USD", "ticker": "BTC-USD"},
    {"key": "ETHUSD", "label": "이더리움/USD", "ticker": "ETH-USD"},
    {"key": "US10Y", "label": "미국 10년물 국채금리", "ticker": "^TNX"},
    {"key": "US30Y", "label": "미국 30년물 국채금리", "ticker": "^TYX"},
    {"key": "GOLD", "label": "금 선물", "ticker": "GC=F"},
    {"key": "SILVER", "label": "은 선물", "ticker": "SI=F"},
]

KIWOOM_MARKET_CODES = {"kospi": "001", "kosdaq": "101"}
CACHE_TTL_SECONDS = 20  # 실시간에 가깝게: 이 시간 내 반복 요청은 캐시된 값을 재사용(과도한 API 호출 방지)
WS_PUSH_INTERVAL_SECONDS = 20  # WebSocket으로 새 값을 밀어주는 주기

app = FastAPI(title="지수 알리미")
security = HTTPBasic()

_cache = {"data": None, "fetched_at": None}


# ----------------------------------------------------------------------
# 인증
# ----------------------------------------------------------------------
def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    expected_user = os.environ.get("DASHBOARD_USERNAME")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD")
    if not expected_user or not expected_pass:
        logger.warning("DASHBOARD_USERNAME/PASSWORD 미설정 - 인증 없이 접근 허용됨 (로컬 테스트용으로만 사용하세요)")
        return
    ok_user = secrets.compare_digest(credentials.username, expected_user)
    ok_pass = secrets.compare_digest(credentials.password, expected_pass)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def verify_ws_authorization(websocket: WebSocket) -> bool:
    """
    WebSocket 핸드셰이크는 일반 HTTP 요청이라, 브라우저가 이미 캐시해둔 Basic Auth
    자격증명을 Authorization 헤더에 그대로 실어 보냅니다. 이를 검증합니다.
    """
    expected_user = os.environ.get("DASHBOARD_USERNAME")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD")
    if not expected_user or not expected_pass:
        return True  # 로컬 테스트용: 인증 미설정 시 통과

    auth_header = websocket.headers.get("authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[len("Basic ") :]).decode("utf-8")
        user, _, pw = decoded.partition(":")
    except Exception:
        return False
    return secrets.compare_digest(user, expected_user) and secrets.compare_digest(pw, expected_pass)


# ----------------------------------------------------------------------
# 저장소 (Redis 우선, 없으면 로컬 파일 폴백)
# ----------------------------------------------------------------------
def get_store():
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return RedisSnapshotStore(url, token)
    logger.warning(
        "UPSTASH_REDIS_REST_URL/TOKEN 미설정 - 로컬 파일(snapshots.json)로 폴백합니다. "
        "클라우드 무료 호스팅에서는 재시작 시 기준값이 초기화될 수 있습니다."
    )
    return SnapshotStore("snapshots.json")


def is_placeholder(v: Optional[str]) -> bool:
    return not v


# ----------------------------------------------------------------------
# 데이터 수집
# ----------------------------------------------------------------------
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
    이력 데이터.

    주의: ka10051/ka10086 둘 다 과거 날짜를 지정하면 그 날의 "장 마감(최종)" 수치만
    돌려주고, 과거 특정 시각의 스냅샷은 제공하지 않습니다. 그래서 여기서 만드는
    비교값은 "전일 같은 시각 대비"가 아니라 "전일 마감 대비" 입니다(증권사 HTS에서
    흔히 쓰는 "전일대비"와 동일한 방식). 국내 수급(ka10051)은 시각별 이력을 제공하는
    TR이 아예 없어 진짜 "같은 시각 대비"는 키움 REST API로는 만들 수 없습니다.

    ka10051(업종별투자자순매수)은 하루치씩만 조회되므로 20영업일치를 얻으려면
    반복 호출이 필요하고, ka10086(일별주가)은 종목 하나당 한 번의 호출로 여러
    날짜를 돌려준다. 시장당 약 20~25회 API 호출이 필요해 다소 느릴 수 있어
    (아래 get_domestic_history_cached에서) 자주 호출하지 않고 캐시해서 씁니다.
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


_history_cache = {"data": None, "fetched_at": None}
HISTORY_CACHE_TTL_SECONDS = 6 * 3600  # 20일치 이력은 자주 바뀌지 않으므로 6시간마다만 갱신


def get_domestic_history_cached(current: dict) -> dict:
    now = datetime.now(KST)
    if _history_cache["data"] is not None and _history_cache["fetched_at"] is not None:
        age = (now - _history_cache["fetched_at"]).total_seconds()
        if age < HISTORY_CACHE_TTL_SECONDS:
            return _history_cache["data"]
    try:
        data = collect_domestic_history(current)
    except Exception:
        logger.exception("국내 수급/거래대금 이력(전일·20일) 수집 중 오류")
        data = _history_cache["data"] or {}
    _history_cache["data"] = data
    _history_cache["fetched_at"] = now
    return data


def collect_overseas() -> dict:
    client = OverseasClient()
    prices = client.get_prices(OVERSEAS_ASSETS)
    labels = {a["key"]: a["label"] for a in OVERSEAS_ASSETS}
    return {"overseas_prices": prices, "overseas_labels": labels}


def collect_ecos() -> dict:
    if os.environ.get("ECOS_ENABLED", "false").lower() != "true":
        return {}
    api_key = os.environ.get("ECOS_API_KEY")
    if is_placeholder(api_key):
        return {}
    value = get_kr_3y_bond_yield(api_key)
    return {"kr_3y_bond_yield": value} if value is not None else {}


def collect_night_futures() -> dict:
    # 기본 비활성화: 무료로 쓸 수 있는 스크래핑 소스(sonmul.co.kr, esignal.co.kr,
    # kred.dev 등)가 전부 자바스크립트/웹소켓으로만 값을 채워서, 서버(requests)로는
    # 가져올 수 없는 상태입니다. 자세한 내용은 night_futures_client.py 상단 주석 참고.
    if os.environ.get("NIGHT_FUTURES_ENABLED", "false").lower() != "true":
        return {}
    data = get_kospi200_night_futures()
    return {"kospi200_night_futures": data} if data is not None else {}


def collect_all() -> dict:
    now = datetime.now(KST)
    if _cache["data"] is not None and _cache["fetched_at"] is not None:
        age = (now - _cache["fetched_at"]).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return _cache["data"]

    current = {}
    try:
        domestic = collect_domestic()
        current.update(domestic)
    except Exception:
        logger.exception("국내 지표 수집 중 오류")
        domestic = {}

    if domestic:
        try:
            current.update(get_domestic_history_cached(current))
        except Exception:
            logger.exception("국내 수급/거래대금 이력 수집 중 오류")

    for collector, label in (
        (collect_overseas, "해외"),
        (collect_ecos, "ECOS"),
        (collect_night_futures, "야간선물"),
    ):
        try:
            current.update(collector())
        except Exception:
            logger.exception("%s 지표 수집 중 오류", label)

    _cache["data"] = current
    _cache["fetched_at"] = now
    return current


# ----------------------------------------------------------------------
# 슬롯(시간대) / 기준점 로직 - 한국시간 기준 하루 두 번의 기준 시각(당일 08:00,
# 전일/당일 20:00)을 기준으로 "지금이 어느 구간인지" 를 판단합니다.
#   - 낮 구간(08:00~19:59): 그 날 08:00에 처음 조회된 값이 기준점
#   - 야간 구간(20:00~다음날 07:59): 20:00에 처음 조회된 값이 기준점
#     (자정을 넘어가도 같은 기준점을 계속 사용)
# 해당 구간에 아무도 접속하지 않으면 실제 08:00/20:00 정각 값을 알 수 없으므로,
# 그 구간에 "처음 접속했을 때"의 값을 기준점으로 저장합니다 (근사치).
# ----------------------------------------------------------------------
def resolve_slot(now: datetime) -> str:
    t = now.timetz().replace(tzinfo=None)
    if dtime(8, 0) <= t < dtime(20, 0):
        return "day"
    else:
        return "night"


def anchor_key_for_slot(now: datetime, slot: str) -> str:
    today = now.date()
    t = now.timetz().replace(tzinfo=None)
    if slot == "day":
        return f"{today.isoformat()}__08"
    else:  # night: 20:00 ~ 다음날 07:59
        anchor_date = today if t >= dtime(20, 0) else today - timedelta(days=1)
        return f"{anchor_date.isoformat()}__20"


def baseline_label_for(now: datetime, slot: str) -> str:
    t = now.timetz().replace(tzinfo=None)
    if slot == "day":
        return "당일 08:00"
    return "당일 20:00" if t >= dtime(20, 0) else "전일 20:00"


def get_or_create_anchor(store, key: str, current: dict) -> dict:
    existing = store.get(key)
    if existing is None:
        store.put(key, current)
        return current
    return existing


# ----------------------------------------------------------------------
# 자체 시계열 로그 - "전일 현시각 대비" / "최근 20일 평균 현시각 대비"를 만들려면
# 키움 API의 일별(마감) 데이터만으로는 부족합니다 (과거 특정 시각의 스냅샷을
# 제공하지 않음). 그래서 이 앱이 직접, 접속이 있을 때마다 현재값을 시각과 함께
# 저장해두고, 나중에 "어제 같은 시각"/"최근 20일 같은 시각"에 가장 가까운 값을
# 찾아 비교합니다. 서버가 오래 잠들어 있었거나(무료 호스팅 슬립) 아무도 접속하지
# 않은 시간대는 로그에 구멍이 생길 수 있어, 가장 가까운 기록이 너무 멀면(기본
# 90분 초과) 비교값을 만들지 않습니다.
# ----------------------------------------------------------------------
INTRADAY_LOG_INTERVAL_MINUTES = 15
INTRADAY_LOG_RETENTION_DAYS = 25  # 20일 평균 + 여유분
INTRADAY_LOG_MAX_GAP_MINUTES = 90

_INTRADAY_FIELDS = (
    "kospi_index", "kosdaq_index",
    "kospi_investor_netbuy", "kosdaq_investor_netbuy",
    "kospi_top_trading_value", "kosdaq_top_trading_value",
)


def _round_to_interval(now: datetime, minutes: int = INTRADAY_LOG_INTERVAL_MINUTES) -> str:
    total_min = now.hour * 60 + now.minute
    rounded = (total_min // minutes) * minutes
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def _time_key_to_minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def log_intraday_snapshot(store, current: dict, now: datetime) -> None:
    """국내 지수/수급/거래대금 상위를 시각과 함께 오늘자 로그에 추가 (이미 같은
    15분 구간에 기록했으면 건너뜀)."""
    if not any(current.get(f) for f in _INTRADAY_FIELDS):
        return  # 국내 데이터가 아예 없으면(키 미설정 등) 로그도 의미 없음

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


def _find_closest_snapshot(day_log: list, target_time_key: str, max_gap_minutes: int = INTRADAY_LOG_MAX_GAP_MINUTES):
    if not day_log:
        return None
    target = _time_key_to_minutes(target_time_key)
    best, best_diff = None, None
    for snap in day_log:
        t = snap.get("time")
        if not t:
            continue
        diff = abs(_time_key_to_minutes(t) - target)
        if best_diff is None or diff < best_diff:
            best, best_diff = snap, diff
    if best is not None and best_diff is not None and best_diff <= max_gap_minutes:
        return best
    return None


def get_same_time_yesterday(store, now: datetime) -> Optional[dict]:
    date_key = (now.date() - timedelta(days=1)).isoformat()
    day_log = store.get(f"tslog__{date_key}") or []
    return _find_closest_snapshot(day_log, _round_to_interval(now))


def get_same_time_recent_avg(store, now: datetime, days: int = 20):
    """반환: (평균값 dict 또는 None, 실제로 평균에 사용된 날짜 수)"""
    idx = store.get("tslog_dates") or []
    today_str = now.date().isoformat()
    dates = sorted((d for d in idx if d != today_str), reverse=True)[:days]
    time_key = _round_to_interval(now)

    snaps = []
    for d in dates:
        day_log = store.get(f"tslog__{d}") or []
        s = _find_closest_snapshot(day_log, time_key)
        if s:
            snaps.append(s)
    if not snaps:
        return None, 0

    def avg_scalar(field):
        vals = [s.get(field) for s in snaps if s.get(field) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def avg_netbuy(field):
        out = {}
        for role in ("individual", "institution", "foreign"):
            vals = [
                s[field][role] for s in snaps
                if s.get(field) and s[field].get(role) is not None
            ]
            out[role] = round(sum(vals) / len(vals), 4) if vals else None
        return out

    def avg_top_trading(field):
        by_code = {}
        for s in snaps:
            for t in (s.get(field) or []):
                code = t.get("code")
                v = t.get("trading_value")
                if code and v is not None:
                    by_code.setdefault(code, []).append(v)
        return {code: round(sum(v) / len(v), 4) for code, v in by_code.items()}

    avg = {
        "kospi_index": avg_scalar("kospi_index"),
        "kosdaq_index": avg_scalar("kosdaq_index"),
        "kospi_investor_netbuy": avg_netbuy("kospi_investor_netbuy"),
        "kosdaq_investor_netbuy": avg_netbuy("kosdaq_investor_netbuy"),
        "kospi_top_trading_value": avg_top_trading("kospi_top_trading_value"),
        "kosdaq_top_trading_value": avg_top_trading("kosdaq_top_trading_value"),
    }
    return avg, len(snaps)


# ----------------------------------------------------------------------
# 응답 조립
# ----------------------------------------------------------------------
def pct_change(current_v, baseline_v) -> Optional[float]:
    if current_v is None or baseline_v is None or baseline_v == 0:
        return None
    return round((current_v - baseline_v) / baseline_v * 100, 2)


def change_amt(current_v, baseline_v) -> Optional[float]:
    if current_v is None or baseline_v is None:
        return None
    return round(current_v - baseline_v, 4)


def _with_change(section, label, value, unit, baseline_v, extra=None):
    d = {
        "section": section,
        "label": label,
        "value": value,
        "unit": unit,
        "change_pct": pct_change(value, baseline_v),
        "change_amt": change_amt(value, baseline_v),
    }
    if extra is not None:
        d["extra"] = extra
    return d


def _compare(value, ref) -> Optional[dict]:
    if value is None or ref is None:
        return None
    return {"amt": change_amt(value, ref), "pct": pct_change(value, ref)}


def _is_fresh_enough(date_str: Optional[str], now: datetime, max_age_days: int = 4) -> bool:
    """
    history[0]의 날짜가 진짜 "최근 영업일"인지 대략적으로 검증한다.
    API 호출이 일시적으로 실패해 실제 어제 데이터가 빠지면(휴장일로 오인되어
    건너뛰어짐) history[0]이 그보다 더 옛날 날짜가 되어 "전일"이라는 라벨이
    틀리게 붙을 수 있다. 캘린더 기준 max_age_days(주말/연휴 감안 여유분)보다
    오래된 날짜면 신뢰할 수 없다고 보고 버린다.
    """
    if not date_str:
        return False
    try:
        d = datetime.strptime(str(date_str), "%Y%m%d").date()
    except ValueError:
        return False
    return (now.date() - d).days <= max_age_days


def _investor_netbuy_yesterday_and_avg(history: list, role: str, now: datetime):
    """history: get_investor_net_buy_history() 결과. index 0 = 전일 마감(같은 시각 아님)."""
    if not history or not _is_fresh_enough(history[0].get("date"), now):
        return None, None
    yesterday = history[0].get(role)
    vals = [h.get(role) for h in history if h.get(role) is not None]
    avg20 = round(sum(vals) / len(vals), 4) if vals else None
    return yesterday, avg20


def _trading_value_yesterday_and_avg(history: list, now: datetime):
    """history: get_stock_trading_value_history() 결과. index 0 = 전일 마감(가장 최근, 같은 시각 아님)."""
    if not history or not _is_fresh_enough(history[0].get("date"), now):
        return None, None
    yesterday = history[0].get("trading_value")
    vals = [h.get("trading_value") for h in history if h.get("trading_value") is not None]
    avg20 = round(sum(vals) / len(vals), 4) if vals else None
    return yesterday, avg20


def build_items(
    current: dict,
    baseline: Optional[dict],
    now: Optional[datetime] = None,
    same_time_yesterday: Optional[dict] = None,
    same_time_avg: Optional[dict] = None,
    same_time_avg_days: int = 0,
) -> list:
    items = []
    baseline = baseline or {}
    now = now or datetime.now(KST)
    same_time_yesterday = same_time_yesterday or {}
    same_time_avg = same_time_avg or {}

    for market_name, market_label in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        v = current.get(f"{market_name}_index")
        if v is not None:
            b = baseline.get(f"{market_name}_index")
            item = _with_change("국내 지수", f"{market_label} 지수", v, "", b)
            item["vs_yesterday_st"] = _compare(v, same_time_yesterday.get(f"{market_name}_index"))
            item["vs_20d_avg_st"] = _compare(v, same_time_avg.get(f"{market_name}_index"))
            item["avg_days"] = same_time_avg_days
            items.append(item)

    for market_name, market_label in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        nb = current.get(f"{market_name}_investor_netbuy")
        base_nb = baseline.get(f"{market_name}_investor_netbuy") or {}
        nb_history = current.get(f"{market_name}_investor_netbuy_history") or []
        st_yesterday_nb = same_time_yesterday.get(f"{market_name}_investor_netbuy") or {}
        st_avg_nb = same_time_avg.get(f"{market_name}_investor_netbuy") or {}
        if nb:
            for role, role_label in (("individual", "개인"), ("institution", "기관"), ("foreign", "외국인")):
                v = nb.get(role)
                b = base_nb.get(role)
                item = _with_change("국내 수급", f"{market_label} {role_label} 순매수", v, "억원", b)
                yesterday, avg20 = _investor_netbuy_yesterday_and_avg(nb_history, role, now)
                item["vs_yesterday"] = _compare(v, yesterday)
                item["vs_20d_avg"] = _compare(v, avg20)
                item["vs_yesterday_st"] = _compare(v, st_yesterday_nb.get(role))
                item["vs_20d_avg_st"] = _compare(v, st_avg_nb.get(role))
                item["avg_days"] = same_time_avg_days
                items.append(item)

    for market_name, market_label in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        top = current.get(f"{market_name}_top_trading_value")
        base_top = baseline.get(f"{market_name}_top_trading_value") or []
        base_by_code = {t.get("code"): t.get("trading_value") for t in base_top if t.get("code")}
        stock_history = current.get(f"{market_name}_top_trading_value_history") or {}
        st_yesterday_top = {
            t.get("code"): t.get("trading_value")
            for t in (same_time_yesterday.get(f"{market_name}_top_trading_value") or [])
            if t.get("code")
        }
        st_avg_top = same_time_avg.get(f"{market_name}_top_trading_value") or {}
        if top:
            for i, t in enumerate(top, start=1):
                v = t.get("trading_value")
                b = base_by_code.get(t.get("code"))
                item = _with_change("국내 거래대금 상위", f"{market_label} {i}위 {t['name']}", v, "억원", b)
                yesterday, avg20 = _trading_value_yesterday_and_avg(stock_history.get(t.get("code")) or [], now)
                item["vs_yesterday"] = _compare(v, yesterday)
                item["vs_20d_avg"] = _compare(v, avg20)
                item["vs_yesterday_st"] = _compare(v, st_yesterday_top.get(t.get("code")))
                item["vs_20d_avg_st"] = _compare(v, st_avg_top.get(t.get("code")))
                item["avg_days"] = same_time_avg_days
                items.append(item)

    kr_bond = current.get("kr_3y_bond_yield")
    if kr_bond is not None:
        b = baseline.get("kr_3y_bond_yield")
        items.append(_with_change("금리", "한국 국채 3년물", kr_bond, "%", b))

    night_fut = current.get("kospi200_night_futures")
    if night_fut:
        b = (baseline.get("kospi200_night_futures") or {}).get("price")
        items.append(
            _with_change(
                "선물 (비공식)",
                "코스피200 야간선물",
                night_fut["price"],
                "",
                b,
                extra=f"전일종가대비 {night_fut['change_pct_vs_prev_close']:+.2f}%",
            )
        )

    overseas_current = current.get("overseas_prices", {})
    overseas_labels = current.get("overseas_labels", {})
    overseas_baseline = baseline.get("overseas_prices", {})
    for key, price in overseas_current.items():
        if price is None:
            continue
        b = overseas_baseline.get(key)
        items.append(_with_change("해외 자산", overseas_labels.get(key, key), price, "", b))

    return items


# ----------------------------------------------------------------------
# 리포트 조립 (REST/WebSocket 공용)
# ----------------------------------------------------------------------
def build_report_payload() -> dict:
    now = datetime.now(KST)
    slot = resolve_slot(now)
    current = collect_all()

    store = get_store()
    anchor_key = anchor_key_for_slot(now, slot)
    is_fresh = store.get(anchor_key) is None
    baseline = get_or_create_anchor(store, anchor_key, current)

    try:
        log_intraday_snapshot(store, current, now)
    except Exception:
        logger.exception("자체 시계열 로그 기록 중 오류")

    try:
        same_time_yesterday = get_same_time_yesterday(store, now)
    except Exception:
        logger.exception("전일 동시각 조회 중 오류")
        same_time_yesterday = None

    try:
        same_time_avg, same_time_avg_days = get_same_time_recent_avg(store, now, days=20)
    except Exception:
        logger.exception("최근 20일 동시각 평균 조회 중 오류")
        same_time_avg, same_time_avg_days = None, 0

    return {
        "generated_at": now.isoformat(),
        "slot": slot,
        "baseline_label": baseline_label_for(now, slot),
        "baseline_is_fresh": is_fresh,
        "items": build_items(current, baseline, now, same_time_yesterday, same_time_avg, same_time_avg_days),
    }


# ----------------------------------------------------------------------
# 라우트
# ----------------------------------------------------------------------
@app.get("/api/report")
def api_report(auth: None = Depends(check_auth)):
    return JSONResponse(build_report_payload())


@app.websocket("/ws")
async def ws_report(websocket: WebSocket):
    if not verify_ws_authorization(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        while True:
            try:
                payload = await asyncio.get_event_loop().run_in_executor(None, build_report_payload)
                await websocket.send_json(payload)
            except Exception:
                logger.exception("WebSocket 리포트 생성/전송 중 오류")
            await asyncio.sleep(WS_PUSH_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        logger.info("WebSocket 연결 종료 (클라이언트)")


@app.get("/", response_class=HTMLResponse)
def dashboard(auth: None = Depends(check_auth)):
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
def health():
    return {"status": "ok"}
