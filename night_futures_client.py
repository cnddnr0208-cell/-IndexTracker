"""
코스피200 야간선물 (현재 비활성화 - 무료 스크래핑 소스를 찾지 못함)
--------------------------------------------------------------
키움증권 REST API(openapi.kiwoom.com)는 공식 TR 목록(OAuth인증/국내주식/미국주식)을
전부 확인해봤지만 국내 선물/옵션 카테고리 자체가 없어, 코스피200 야간선물은
REST API로 조회할 방법이 없습니다.

무료 실시간 시세 사이트를 여러 곳 시도해봤지만(sonmul.co.kr, esignal.co.kr,
kred.dev, nightkospi.com, moneyrecipe.blog, futurespotal.com) 전부 서버가 HTML
껍데기만 내려주고 실제 가격은 브라우저의 자바스크립트가 웹소켓/API로 받아와
채워 넣는 방식이었습니다. 이 파일의 `requests` 기반 스크래핑(브라우저 없이 HTML만
가져옴)으로는 이런 사이트에서 값을 가져올 수 없습니다 - 정규식을 아무리 고쳐도
근본적으로 해결되지 않습니다(파싱 대상 자체가 존재하지 않음).

그래서 app.py에서는 기본값을 NIGHT_FUTURES_ENABLED=false 로 꺼두고 있습니다.
아래는 참고용으로 남겨둔 예전 sonmul.co.kr 스크래핑 코드입니다(현재는 항상 실패함).
나중에 값을 살리려면 아래 중 하나가 필요합니다.
  1) 실제로 유료/공식 데이터를 주는 소스(코스피200 야간선물은 유렉스(Eurex) 연계
     상품이라 정식 데이터는 대부분 유료입니다)를 구해서 이 파일을 API 호출로
     새로 작성
  2) 위 사이트들의 웹소켓 프로토콜을 분석해 직접 연결(기술적으로 복잡하고,
     사이트가 프로토콜을 바꾸면 다시 깨지는 불안정한 방법)
  3) (가장 현실적인 대안) 이미 대시보드에 있는 "해외 자산" 섹션의 미국 지수 선물
     (나스닥100/S&P500)과 달러/원 환율을 야간선물의 방향성 참고 지표로 사용
"""

import logging
import re
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

SOURCE_URL = "https://sonmul.co.kr/futures/KOSPI200_NIGHT"

# 페이지 텍스트 흐름: "...987.45+8.70+0.89%시가993.50고가1,011.20저가961.25..."
# (현재가/변동/등락률 뒤에 시가·고가·저가가 이어붙어 나옵니다. 2026-08 기준.
#  "실시간 시세입니다." 문구와 숫자 사이에 설명 문장이 끼어 있어 문구는 앵커로 쓰지 않습니다.
#  사이트가 개편되면 실제 페이지 텍스트를 다시 확인해 이 패턴을 조정해야 합니다.)
_PATTERN = re.compile(
    r"([\d,]+\.\d+)"        # 현재가
    r"([+-][\d,]+\.\d+)"    # 전일대비 변동
    r"([+-][\d.]+)%"        # 전일대비 등락률
    r"시가([\d,]+\.\d+)"
    r"고가([\d,]+\.\d+)"
    r"저가([\d,]+\.\d+)"
)


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def get_kospi200_night_futures() -> Optional[Dict[str, float]]:
    """
    반환: {"price", "change_vs_prev_close", "change_pct_vs_prev_close", "open", "high", "low"}
    실패 시 None (조용히 넘어가고, 해당 항목만 리포트에서 빠짐).
    """
    try:
        resp = requests.get(
            SOURCE_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; IndexAlarmBot/1.0)"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("코스피200 야간선물(sonmul.co.kr) 요청 실패")
        return None

    text = resp.text
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(resp.text, "html.parser").get_text()
    except ImportError:
        logger.warning("beautifulsoup4 미설치 - 원시 HTML로 파싱을 시도합니다 (실패 가능성 높음)")

    match = _PATTERN.search(text)
    if not match:
        logger.warning("코스피200 야간선물 페이지 구조가 예상과 달라 파싱에 실패했습니다.")
        return None

    price, change, change_pct, open_, high, low = match.groups()
    return {
        "price": _to_float(price),
        "change_vs_prev_close": _to_float(change),
        "change_pct_vs_prev_close": float(change_pct),
        "open": _to_float(open_),
        "high": _to_float(high),
        "low": _to_float(low),
    }
