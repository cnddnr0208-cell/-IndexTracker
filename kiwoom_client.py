"""
키움증권 REST API 클라이언트 (국내 지표 수집)
--------------------------------------------
비공식 파이썬 래퍼 `kiwoom-rest-api` (GitHub: younghwan91/kiwoom-rest-api, MIT,
import 이름 `kiwoom_rest_api`)를 사용합니다.

** 중요: PyPI 패키지 이름에 대해 **
PyPI에 올라와 있는 `kiwoom-rest-api` 배포본(0.1.x)은 실제로 설치해보면 내용이
비어있는(아무 이름도 export하지 않는) 문제가 확인되었습니다 (`dir(kiwoom_rest_api)` == []).
반면 GitHub 저장소(main 브랜치)의 소스코드는 완전하고 정상 동작합니다. 그래서
requirements.txt 에서는 PyPI 패키지명 대신 GitHub 저장소를 zip으로 직접 설치하도록
설정했습니다:

    https://github.com/younghwan91/kiwoom-rest-api/archive/refs/heads/main.zip

만약 예전에 `pip install kiwoom-rest-api`로 PyPI 버전을 이미 설치한 적이 있다면,
아래처럼 지우고 GitHub 버전으로 다시 설치하세요.

    py -m pip uninstall -y kiwoom-rest-api
    py -m pip install -r requirements.txt

https://openapi.kiwoom.com/guide/apiguide (공식) 문서 기준 TR ID를 그대로 사용하며,
아래 각 함수에는 사용한 TR ID를 주석으로 남겨두었습니다.

** 응답 필드명에 대해 **
아래 코드는 실제 키(app_key/app_secret) 없이 응답 필드명까지 완전히 검증할 수는
없었습니다. 최초 실행 시 아래 사항을 확인하세요.
  1) 응답 구조가 예상과 다르면 _first_present() 가 후보 키를 순서대로 탐색하니,
     터미널 로그에 찍히는 _debug_shape() 출력(실제 최상위 키/첫 번째 row)을 보고
     candidates 리스트에 실제 필드명을 추가하면 됩니다.
  2) kiwoom_market_codes(config.yaml)의 업종코드(001=코스피, 101=코스닥)가
     ka10101(업종코드 리스트) 응답과 일치하는지 확인하세요.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _log_api_exception(e: Exception, context: str) -> None:
    """
    kiwoom_rest_api 호출 실패 시 최대한 유용한 정보를 로그에 남긴다.
    KiwoomAPIError(code/message/response 속성을 가짐)인 경우 API가 실제로
    돌려준 오류 코드/메시지/응답 본문까지 그대로 남겨서, 어떤 필드가 잘못됐는지
    바로 알 수 있게 한다.
    """
    code = getattr(e, "code", None)
    message = getattr(e, "message", None)
    response = getattr(e, "response", None)
    if code is not None or message is not None:
        logger.error(
            "%s 호출 실패 (Kiwoom API 오류) code=%s message=%s response=%s",
            context, code, message, response,
        )
    else:
        logger.exception("%s 호출 실패", context)


def _first_present(d: Dict, candidates: List[str], default=None):
    """응답 딕셔너리에서 candidates 중 처음 발견되는 키의 값을 반환."""
    for c in candidates:
        if c in d and d[c] not in (None, ""):
            return d[c]
    return default


def _to_float(v, default=0.0) -> float:
    if v is None:
        return default
    try:
        # 키움 API는 부호(+/-)가 붙은 문자열, 또는 앞에 0이 붙은 문자열을 자주 반환함
        s = str(v).strip().replace(",", "")
        return float(s)
    except (ValueError, TypeError):
        return default


class KiwoomClient:
    def __init__(self, app_key: str, app_secret: str, is_mock: bool = False):
        try:
            import kiwoom_rest_api as _kr_pkg
        except ImportError as e:
            raise ImportError(
                "kiwoom_rest_api 모듈을 찾을 수 없습니다. "
                "터미널에서 'py -m pip install -r requirements.txt' 를 다시 실행하세요 "
                "(GitHub 저장소를 zip으로 직접 설치합니다)."
            ) from e

        KiwoomAPI = getattr(_kr_pkg, "KiwoomAPI", None)
        if KiwoomAPI is None:
            available = sorted(n for n in dir(_kr_pkg) if not n.startswith("_"))
            logger.error(
                "kiwoom_rest_api 패키지는 설치되어 있지만 'KiwoomAPI' 이름을 찾지 못했습니다. "
                "설치된 패키지에 실제로 있는 이름 목록: %s",
                available,
            )
            raise ImportError(
                "kiwoom_rest_api 모듈에서 KiwoomAPI를 찾을 수 없습니다. "
                f"실제 사용 가능한 이름: {available}\n"
                "예전에 'pip install kiwoom-rest-api'로 설치된 (비어있는) PyPI 버전이 "
                "남아있을 가능성이 높습니다. 터미널에서 아래 두 줄을 순서대로 실행해보세요.\n"
                "  py -m pip uninstall -y kiwoom-rest-api\n"
                "  py -m pip install -r requirements.txt"
            )

        self._api = KiwoomAPI(app_key=app_key, app_secret=app_secret, is_mock=is_mock)
        self._logged_in = False

    def login(self):
        # TR: au10001 (접근토큰 발급)
        # 주의: kiwoom_rest_api 라이브러리의 KiwoomAPI.login()은 응답에 토큰이
        # 없어도(예: appkey/secret 오류, IP 제한, 미승인 앱 등으로 Kiwoom이
        # return_code!=0 인 에러 바디를 HTTP 200으로 내려줘도) 예외를 던지지
        # 않고 access_token을 빈 문자열로 조용히 저장한다. 그러면 이후 모든
        # API 호출이 "authorization 필드가 설정되어 있어야 합니다" 라는
        # 엉뚱해 보이는 오류로 실패해서 진짜 원인(로그인 실패 사유)이 로그에
        # 안 남는다. 그래서 여기서 토큰 발급 여부를 직접 확인해 실패 시
        # Kiwoom이 실제로 응답한 내용을 그대로 로그/예외에 남긴다.
        result = self._api.login()
        token = (result or {}).get("token") or (result or {}).get("access_token")
        if not token:
            logger.error("Kiwoom 로그인(토큰 발급) 실패 - 토큰을 받지 못했습니다. Kiwoom 응답 원문: %s", result)
            raise RuntimeError(f"Kiwoom 로그인 실패(토큰 미발급) - Kiwoom 응답: {result}")
        self._logged_in = True

    def logout(self):
        if self._logged_in:
            # TR: au10002 (접근토큰 폐기)
            self._api.logout()
            self._logged_in = False

    # ------------------------------------------------------------------
    # 지수
    # ------------------------------------------------------------------
    def get_index_price(self, market_code: str) -> Optional[float]:
        """
        전업종지수요청 (TR: ka20003) 으로 전체 업종 지수를 받아온 뒤
        market_code(예: 001=코스피종합, 101=코스닥종합)에 해당하는 현재가를 찾는다.

        공식 문서 기준 요청 바디는 inds_cd 하나뿐이다: {"inds_cd": "001"}.
        응답은 all_inds_idex 리스트, 종합지수 행의 필드는 stk_cd/stk_nm/cur_prc.
        """
        try:
            resp = self._api.sector.all_industry_index(inds_cd=market_code)
        except Exception as e:
            _log_api_exception(e, "ka20003(전업종지수요청)")
            return None

        rows = _extract_rows(resp)
        for row in rows:
            code = _first_present(row, ["stk_cd", "inds_cd", "idx_cd", "code"])
            if code and str(code).strip() == str(market_code):
                price = _first_present(row, ["cur_prc", "now_prc", "prpr", "price"])
                return _to_float(price)
        logger.warning(
            "업종코드 %s 를 전업종지수(ka20003) 응답에서 찾지 못함. "
            "원본 응답 키/샘플: %s",
            market_code,
            _debug_shape(resp, rows),
        )
        return None

    # ------------------------------------------------------------------
    # 투자자별(개인/기관/외국인) 순매수
    # ------------------------------------------------------------------
    def get_investor_net_buy(self, market_code: str, base_dt: Optional[str] = None) -> Dict[str, float]:
        """
        업종별투자자순매수요청 (TR: ka10051)
        반환: {"individual": 개인순매수, "institution": 기관순매수, "foreign": 외국인순매수}
        (단위: 억원, 공식 문서 기준 응답이 이미 억원 단위라 그대로 반환)

        공식 문서 기준 요청 바디:
          mrkt_tp: 코스피=0, 코스닥=1 (앱 전체에서 쓰는 001/101 코드와 다르므로 변환 필요)
          amt_qty_tp: 금액=0, 수량=1
          base_dt: 기준일자(YYYYMMDD, 선택) - 과거 특정일 조회용. 생략하면 당일 기준.
          stex_tp: 1=KRX, 2=NXT, 3=통합
        응답은 inds_netprps 리스트, 개인=ind_netprps, 기관계=orgn_netprps, 외국인=frgnr_netprps.
        """
        ka10051_mrkt_tp = "0" if str(market_code) == "001" else "1"
        kwargs = {"mrkt_tp": ka10051_mrkt_tp, "amt_qty_tp": "0", "stex_tp": "3"}
        if base_dt:
            kwargs["base_dt"] = base_dt
        try:
            resp = self._api.sector.industry_investor_net_buy(**kwargs)
        except Exception as e:
            _log_api_exception(e, f"ka10051(업종별투자자순매수요청, base_dt={base_dt})")
            return {"individual": 0.0, "institution": 0.0, "foreign": 0.0}

        rows = _extract_rows(resp)
        # 코스피/코스닥 종합 행(inds_cd가 001_AL 또는 101_AL 등)을 우선 찾고, 없으면 첫 행 사용
        row = None
        for r in rows:
            inds_cd = str(_first_present(r, ["inds_cd"], "") or "")
            if inds_cd.startswith(str(market_code)):
                row = r
                break
        if row is None:
            row = rows[0] if rows else (resp if isinstance(resp, dict) else {})

        result = {
            "individual": _to_float(_first_present(row, ["ind_netprps", "individual_netbuy", "prsn_netprps"])),
            "institution": _to_float(_first_present(row, ["orgn_netprps", "institution_netbuy"])),
            "foreign": _to_float(_first_present(row, ["frgnr_netprps", "frgn_netprps", "foreign_netbuy"])),
        }
        if row and all(v == 0.0 for v in result.values()):
            logger.warning(
                "업종별투자자순매수(ka10051, base_dt=%s) 응답에서 예상 필드를 못 찾음(모두 0). "
                "원본 응답 키/샘플: %s",
                base_dt,
                _debug_shape(resp, rows),
            )
        return result

    def get_investor_net_buy_history(
        self, market_code: str, days: int = 20, end_date: Optional[str] = None
    ) -> List[Dict]:
        """
        end_date(YYYYMMDD, 한국시간 기준 "오늘")부터 거슬러 올라가며 ka10051을
        반복 호출해 최근 `days`영업일치 투자자별 순매수 내역을 모은다
        (주말/휴장일은 응답이 비어있다고 보고 건너뜀).
        반환: [{"date": "YYYYMMDD", "individual":.., "institution":.., "foreign":..}, ...]
        (index 0 = 가장 최근 영업일 = "어제")
        캘린더 기준 최대 days*3일(주말/공휴일 감안 여유분)까지만 거슬러 올라간다.

        end_date를 생략하면 이 서버 프로세스의 로컬 시간대 기준 오늘 날짜를 쓰는데,
        클라우드 호스팅은 보통 UTC라서 한국시간 자정~오전 9시 사이에는 날짜가
        하루 어긋날 수 있다. 반드시 호출하는 쪽(app.py)에서 한국시간 기준 날짜를
        end_date로 넘겨주는 것을 권장한다.
        """
        if end_date:
            today = datetime.strptime(end_date, "%Y%m%d").date()
        else:
            logger.warning(
                "get_investor_net_buy_history: end_date 미지정 - 서버 로컬 시간대의 "
                "오늘 날짜를 사용합니다 (한국시간과 다를 경우 '어제' 값이 하루 어긋날 수 있음)."
            )
            today = datetime.now().date()

        results: List[Dict] = []
        max_lookback = days * 3 + 10
        for i in range(1, max_lookback + 1):
            if len(results) >= days:
                break
            d = today - timedelta(days=i)
            date_str = d.strftime("%Y%m%d")
            nb = self.get_investor_net_buy(market_code, base_dt=date_str)
            if all(v == 0.0 for v in nb.values()):
                continue  # 휴장일/데이터없음으로 간주하고 건너뜀
            nb["date"] = date_str
            results.append(nb)
        return results

    # ------------------------------------------------------------------
    # 거래대금 상위 종목
    # ------------------------------------------------------------------
    def get_top_trading_value(self, market_type: str, top_n: int = 5) -> List[Dict]:
        """
        거래대금상위요청 (TR: ka10032)
        market_type: 키움 REST 가이드 기준 시장구분 코드 (예: "001"=코스피, "101"=코스닥).
                     최초 실행 전 공식 가이드에서 정확한 코드값을 확인하세요.
        반환: [{"name": 종목명, "code": 종목코드, "trading_value": 거래대금(억원)}, ...]
        (공식 문서 기준 원본 응답 단위는 백만원 → 여기서 100으로 나눠 억원으로 변환)
        """
        try:
            # 공식 문서 기준 요청 바디: mrkt_tp(000전체/001코스피/101코스닥),
            # mang_stk_incls(관리종목포함 0/1), stex_tp(1KRX/2NXT/3통합)
            resp = self._api.ranking.top_trading_value(
                mrkt_tp=market_type,
                mang_stk_incls="0",
                stex_tp="3",
            )
        except Exception as e:
            _log_api_exception(e, "ka10032(거래대금상위요청)")
            return []

        rows = _extract_rows(resp)
        results = []
        for row in rows[:top_n]:
            results.append(
                {
                    "name": _first_present(row, ["stk_nm", "hts_kor_isnm", "name"], "알수없음"),
                    "code": _first_present(row, ["stk_cd", "code"], ""),
                    "trading_value": _to_float(
                        _first_present(row, ["trde_prica", "acml_tr_pbmn", "trading_value"])
                    ) / 100,
                }
            )
        if rows and not results:
            logger.warning(
                "거래대금상위(ka10032) 응답을 파싱하지 못함. 원본 응답 키/샘플: %s",
                _debug_shape(resp, rows),
            )
        return results

    # ------------------------------------------------------------------
    # 개별 종목 일별 거래대금 이력 (거래대금 상위 종목의 "전일"/"최근 20일" 비교용)
    # ------------------------------------------------------------------
    def get_stock_trading_value_history(self, stk_cd: str, from_date: str, days: int = 20) -> List[Dict]:
        """
        일별주가요청 (TR: ka10086) - from_date(YYYYMMDD)부터 과거로 여러 날짜의
        일별 데이터를 한 번에 반환한다 (공식 문서 응답 예시에도 여러 날짜가
        한 응답에 같이 들어있음).
        반환: [{"date": "YYYYMMDD", "trading_value": 거래대금(억원)}, ...] (최신순)
        indc_tp=1(금액, 백만원 단위)로 요청 후 억원으로 환산.
        """
        try:
            resp = self._api.market.daily_stock_price(stk_cd=stk_cd, qry_dt=from_date, indc_tp="1")
        except Exception as e:
            _log_api_exception(e, f"ka10086(일별주가요청, stk_cd={stk_cd})")
            return []

        rows = _extract_rows(resp)
        results = []
        for row in rows[:days]:
            date_v = _first_present(row, ["date"])
            amt = _to_float(_first_present(row, ["amt_mn", "trde_prica", "amount"]))
            if date_v is None:
                continue
            results.append({"date": str(date_v), "trading_value": amt / 100})  # 백만원 -> 억원
        return results


def _debug_shape(resp, rows: List[Dict]) -> str:
    """
    응답 파싱에 실패했을 때 진단용으로 남기는 요약 문자열.
    전체 응답을 그대로 로그에 남기면 너무 길어서, 최상위 키 목록과
    첫 번째 row의 키/값만 잘라서 보여줍니다. 이 로그를 보면 실제 필드명을
    바로 알 수 있어 _first_present() candidates 를 정확히 고칠 수 있습니다.
    """
    try:
        top_keys = list(resp.keys()) if isinstance(resp, dict) else f"<{type(resp).__name__}>"
        first_row = rows[0] if rows else None
        return f"top_level_keys={top_keys}, first_row={first_row}"
    except Exception:
        return "<디버그 정보 생성 실패>"


def _extract_rows(resp) -> List[Dict]:
    """
    kiwoom-rest-api 응답에서 리스트 데이터를 최대한 유연하게 추출.

    키움 공식 REST API는 TR마다 리스트 필드 이름이 다르다 (예: ka10008 응답은
    "stk_frgnr", ka10020 응답은 "bid_req_upper", ka00198 응답은 "item_inq_rank"
    처럼 TR을 설명하는 한글/영문 축약 이름을 그대로 씀 - "output"/"list" 같은
    범용 키가 아님). 그래서 흔한 범용 키를 먼저 시도하고, 없으면 딕셔너리 값
    중 dict로 이루어진 비어있지 않은 리스트를 아무거나 찾아서 사용한다
    (return_code/return_msg/cont-yn 같은 메타 필드는 리스트가 아니므로 자동 제외됨).
    """
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in (
            "output", "output1", "output2", "list", "data", "rows",
            # 공식 문서에서 확인된 TR별 실제 리스트 필드명 (ka20003/ka10051/ka10032/ka10086)
            "all_inds_idex", "inds_netprps", "trde_prica_upper", "daly_stkpc",
        ):
            v = resp.get(key)
            if isinstance(v, list):
                return v
        # TR별로 이름이 다른 리스트 필드를 자동으로 탐색
        for v in resp.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        # 단일 레코드 딕셔너리인 경우
        return [resp]
    return []
