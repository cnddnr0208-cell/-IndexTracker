"""
스냅샷 저장소
------------
매 브리핑 실행 시점의 지표값을 로컬 JSON 파일에 저장해두고,
이후 다른 시각 브리핑에서 "특정 기준점 대비 변동률"을 계산할 때 사용합니다.

저장 키 형식: "YYYY-MM-DD__슬롯이름"  (예: "2026-08-10__morning")
"""

import json
import logging
import os
from datetime import date
from typing import Dict, Optional

from snapshot_store_redis import RedisSnapshotStore

logger = logging.getLogger(__name__)


def get_store():
    """
    저장소 인스턴스를 만든다 (Redis 우선, 없으면 로컬 파일로 폴백).

    app.py(Render)와 local_collector.py(집 PC)가 "같은" Upstash Redis를
    바라봐야 서로 데이터를 주고받을 수 있으므로, 이 함수 하나로 통일해서
    둘 다 사용합니다. UPSTASH_REDIS_REST_TOKEN이 .env.example의 예시값
    그대로 남아있으면(설정을 안 한 것으로 보고) 로컬 파일로 폴백합니다.
    """
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token and token != "YOUR_UPSTASH_TOKEN" and "xxxx" not in url:
        return RedisSnapshotStore(url, token)
    logger.warning(
        "UPSTASH_REDIS_REST_URL/TOKEN이 설정되지 않았습니다 - 로컬 파일(snapshots.json)로 폴백합니다. "
        "Render 등 클라우드 호스팅에서는 재시작 시 값이 초기화될 수 있고, local_collector.py와도 "
        "데이터를 공유할 수 없습니다."
    )
    return SnapshotStore("snapshots.json")


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path
        self._data = self._load()

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # 손상된 파일은 백업해두고 빈 상태로 시작
            backup = self.path + ".corrupted"
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return {}

    def _save(self) -> None:
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    @staticmethod
    def make_key(d: date, slot_name: str) -> str:
        return f"{d.isoformat()}__{slot_name}"

    def get(self, key: str) -> Optional[Dict]:
        return self._data.get(key)

    def put(self, key: str, values: Dict) -> None:
        self._data[key] = values
        self._save()

    def prune_older_than(self, days: int = 14) -> None:
        """오래된 스냅샷 정리 (파일이 무한정 커지는 것 방지)."""
        from datetime import datetime, timedelta

        cutoff = datetime.now().date() - timedelta(days=days)
        keep = {}
        for k, v in self._data.items():
            try:
                d_str = k.split("__", 1)[0]
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
                if d >= cutoff:
                    keep[k] = v
            except ValueError:
                keep[k] = v  # 파싱 실패한 키는 보존
        self._data = keep
        self._save()
