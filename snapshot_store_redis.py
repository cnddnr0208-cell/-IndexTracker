"""
Upstash Redis 기반 스냅샷 저장소 (클라우드 배포용)
--------------------------------------------------
Render 같은 무료 호스팅의 웹 서비스는 15분 이상 요청이 없으면 슬립되고,
다시 깨어나거나 재배포될 때 로컬 디스크 내용이 초기화될 수 있습니다.
그래서 "기준점 비교용 스냅샷"은 로컬 파일 대신 Upstash Redis(무료 티어)에 저장합니다.

https://upstash.com 에서 무료 Redis 데이터베이스를 만들고, Connect 화면의 REST 탭에서
UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN 값을 환경변수로 설정하세요.

REST API 문서: https://upstash.com/docs/redis/features/restapi
"""

import json
import logging
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class RedisSnapshotStore:
    def __init__(self, rest_url: str, rest_token: str, key_prefix: str = "idxalarm:"):
        self.rest_url = rest_url.rstrip("/")
        self.rest_token = rest_token
        self.key_prefix = key_prefix

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.rest_token}"}

    def get(self, key: str) -> Optional[Dict]:
        full_key = f"{self.key_prefix}{key}"
        try:
            resp = requests.get(
                f"{self.rest_url}/get/{full_key}", headers=self._headers(), timeout=10
            )
            resp.raise_for_status()
            result = resp.json().get("result")
            if result is None:
                return None
            return json.loads(result)
        except Exception:
            logger.exception("Redis GET 실패: %s", key)
            return None

    def put(self, key: str, value: Dict) -> None:
        full_key = f"{self.key_prefix}{key}"
        try:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            resp = requests.post(
                f"{self.rest_url}/set/{full_key}",
                headers=self._headers(),
                data=body,
                timeout=10,
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("Redis SET 실패: %s", key)
