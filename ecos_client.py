"""
한국은행 ECOS Open API 클라이언트 (선택 기능)
--------------------------------------------
국내 3년물 국채금리는 키움 REST API의 국내주식 API 목록에 포함되어 있지 않아
한국은행 ECOS(경제통계시스템) Open API로 대체 조회합니다.
https://ecos.bok.or.kr 에서 "인증키 신청" 메뉴로 무료 발급받을 수 있습니다.

주의: ECOS 금리 통계는 "일별" 데이터라 실시간이 아니라 전영업일까지 갱신됩니다.
     완전한 실시간 3년물 금리가 꼭 필요하다면 유료 시세 제공사 연동을 검토하세요.

통계표코드: 817Y002 (시장금리), 항목코드: 010200000 (국고채(3년))
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ECOS_URL_TMPL = (
    "https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr/1/5/"
    "817Y002/D/{start}/{end}/010200000"
)


def get_kr_3y_bond_yield(api_key: str) -> Optional[float]:
    end = datetime.now().date()
    start = end - timedelta(days=10)  # 휴일 대비 여유있게 조회 후 최신값 사용
    url = ECOS_URL_TMPL.format(
        api_key=api_key,
        start=start.strftime("%Y%m%d"),
        end=end.strftime("%Y%m%d"),
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()

        # ECOS는 인증키가 잘못됐거나 코드가 틀려도 HTTP 200을 주고 RESULT 안에
        # 에러 코드/메시지를 담아 돌려주는 경우가 많아, 이 경우도 따로 로그를 남긴다.
        result = payload.get("RESULT")
        if result and result.get("CODE") not in (None, "INFO-000"):
            logger.warning(
                "ECOS API가 오류를 반환함 (통계표코드/항목코드가 틀렸거나 인증키 문제일 수 있음): %s",
                result,
            )
            return None

        rows = payload.get("StatisticSearch", {}).get("row", [])
        if not rows:
            logger.warning("ECOS 응답에 데이터가 없음(통계표코드/항목코드 확인 필요). 원본 응답: %s", payload)
            return None
        latest = rows[-1]  # 날짜순 정렬되어 있다고 가정, 마지막 값이 최신
        return float(latest["DATA_VALUE"])
    except Exception:
        logger.exception("ECOS API 호출 실패")
        return None
