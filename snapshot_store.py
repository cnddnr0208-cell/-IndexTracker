"""
스냅샷 저장소
------------
매 브리핑 실행 시점의 지표값을 로컬 JSON 파일에 저장해두고,
이후 다른 시각 브리핑에서 "특정 기준점 대비 변동률"을 계산할 때 사용합니다.

저장 키 형식: "YYYY-MM-DD__슬롯이름"  (예: "2026-08-10__morning")
"""

import json
import os
from datetime import date
from typing import Dict, Optional


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
