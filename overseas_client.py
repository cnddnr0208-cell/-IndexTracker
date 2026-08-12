"""
해외 지수/원자재/환율/코인/금리 클라이언트 (yfinance 사용)
--------------------------------------------------------
키움증권 REST API는 국내주식 위주라, VIX/DXY/코인/미국채금리 같은 항목은
무료 데이터 소스인 Yahoo Finance(yfinance)로 수집합니다.

주의:
- yfinance는 야후파이낸스 웹 데이터를 비공식적으로 가져오는 라이브러리로,
  야후 쪽 변경에 따라 간헐적으로 오류가 날 수 있습니다.
- 선물(NQ=F, ES=F, CL=F 등)은 장 마감 시간대에 갱신이 늦어질 수 있고,
  ^TNX/^TYX(미국채 금리)는 "수익률(%) 값"을 그대로 제공합니다(별도 스케일링 불필요.
  다만 최초 실행 시 실제 값과 한 번 비교해 확인하는 것을 권장합니다).
- 미국 2년물 국채금리는 안정적인 무료 실시간 티커가 없어 config에서 기본 비활성화되어 있습니다.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class OverseasClient:
    def __init__(self):
        try:
            import yfinance  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "yfinance 패키지가 설치되어 있지 않습니다. 'pip install yfinance' 로 설치하세요."
            ) from e

    def get_prices(self, assets: List[Dict]) -> Dict[str, Optional[float]]:
        """
        assets: config.yaml의 overseas_assets 리스트 (enabled: true 인 것만 전달할 것)
        반환: {key: 현재가(float) or None}
        """
        import yfinance as yf

        tickers = [a["ticker"] for a in assets]
        result: Dict[str, Optional[float]] = {}

        if not tickers:
            return result

        try:
            data = yf.Tickers(" ".join(tickers))
        except Exception:
            logger.exception("yfinance Tickers 초기화 실패")
            data = None

        for asset in assets:
            key = asset["key"]
            ticker_str = asset["ticker"]
            price = None
            try:
                if data is not None:
                    t = data.tickers.get(ticker_str)
                    if t is not None:
                        fast = getattr(t, "fast_info", None)
                        if fast is not None:
                            price = fast.get("lastPrice") or fast.get("last_price")
                        if price is None:
                            # fast_info로 못 가져오면 history로 폴백
                            hist = t.history(period="1d", interval="1m")
                            if not hist.empty:
                                price = float(hist["Close"].dropna().iloc[-1])
            except Exception:
                logger.exception("가격 조회 실패: %s (%s)", key, ticker_str)
                price = None

            result[key] = float(price) if price is not None else None

        return result
