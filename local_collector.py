"""
집 PC 전용 국내 지표 수집 스크립트
------------------------------------
키움증권 계좌의 "지정단말기(IP)" 보안 설정 때문에, 등록된 이 PC의 IP에서만
키움 REST API 로그인이 됩니다. 그래서 Render(클라우드) 서버 대신 이 PC에서
주기적으로 실행해 국내 지수/수급/거래대금을 수집한 뒤 Upstash Redis에
저장합니다. app.py(Render)는 이 Redis 값을 읽기만 합니다.

필요한 환경변수 (.env, app.py와 동일한 파일을 그대로 씁니다):
  KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK
  UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
    -> Render에 설정한 값과 반드시 "같은" Upstash 프로젝트여야 합니다.
       다르면 app.py가 이 스크립트가 저장한 값을 못 읽습니다.

수동 실행:
  py local_collector.py

Windows 작업 스케줄러 등록 방법은 README.md의
"국내 데이터 수집 - 집 PC 병행 실행" 섹션을 참고하세요. 장중(국내 정규장
09:00~15:30, 여유를 둔 08:50~15:35)에만 실제로 수집하고, 그 외 시간에는
곧바로 종료합니다 - 스케줄러에는 그냥 짧은 간격(예: 1~2분)으로 등록해두고
장 시간 판단은 이 스크립트가 알아서 합니다.
"""

import logging
from datetime import datetime
from datetime import time as dtime

try:
    from dotenv import load_dotenv  # 로컬 실행 편의용 (선택 설치)

    load_dotenv()
except ImportError:
    pass

from domestic_collector import KST, collect_and_publish
from snapshot_store import get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def is_market_hours(now: datetime) -> bool:
    """국내 정규장(09:00~15:30) 기준, 여유를 두고 08:50~15:35에만 수집.
    주말은 건너뜀 (공휴일은 별도로 체크하지 않음 - 휴장일엔 키움 API가
    빈 값을 주므로 collect_domestic()이 알아서 빈 dict를 반환합니다)."""
    if now.weekday() >= 5:  # 5=토, 6=일
        return False
    t = now.timetz().replace(tzinfo=None)
    return dtime(8, 50) <= t <= dtime(15, 35)


def main():
    now = datetime.now(KST)
    if not is_market_hours(now):
        logger.info("국내 장 시간이 아니라 건너뜁니다 (%s)", now.isoformat())
        return

    store = get_store()
    current = collect_and_publish(store)
    if current is None:
        logger.error("국내 데이터 수집 실패 - 키움 로그인/조회에 실패했습니다. 위 로그의 오류 메시지를 확인하세요.")
        return

    logger.info(
        "국내 데이터 수집/저장 완료 (%s) - 코스피 %s / 코스닥 %s",
        now.isoformat(),
        current.get("kospi_index"),
        current.get("kosdaq_index"),
    )


if __name__ == "__main__":
    main()
