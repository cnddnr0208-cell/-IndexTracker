"""
지수 알리미 - 메인 실행 스크립트
--------------------------------
사용법:
  python main.py --slot morning     # 07:30 브리핑 (전일 evening 스냅샷과 비교)
  python main.py --slot afternoon   # 15:00 브리핑 (당일 morning 스냅샷과 비교)
  python main.py --slot evening     # 19:30 브리핑 (당일 morning 스냅샷과 비교, 내일 아침 기준점으로 저장)

Windows 작업 스케줄러에 3개 작업(morning/afternoon/evening)을 각각 등록해서 사용하세요.
자세한 등록 방법은 README.md 참고.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta

import yaml

from ecos_client import get_kr_3y_bond_yield
from kakao_notifier import KakaoNotifier
from kiwoom_client import KiwoomClient
from overseas_client import OverseasClient
from snapshot_store import SnapshotStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(
            "%s 파일이 없습니다. config.example.yaml 을 복사해서 config.yaml 로 만들고 "
            "값을 채워주세요.",
            path,
        )
        sys.exit(1)


def is_placeholder(value: str) -> bool:
    return not value or "YOUR_" in str(value)


def collect_domestic(config: dict) -> dict:
    kw = config["kiwoom"]
    if is_placeholder(kw.get("app_key")) or is_placeholder(kw.get("app_secret")):
        logger.warning("키움 API 키가 설정되지 않아 국내 지표 수집을 건너뜁니다.")
        return {}

    codes = config["kiwoom_market_codes"]
    client = KiwoomClient(kw["app_key"], kw["app_secret"], kw.get("is_mock", False))
    client.login()
    try:
        data = {}
        for market_name, code in codes.items():
            data[f"{market_name}_index"] = client.get_index_price(code)
            data[f"{market_name}_investor_netbuy"] = client.get_investor_net_buy(code)
            data[f"{market_name}_top_trading_value"] = client.get_top_trading_value(code, top_n=5)
        return data
    finally:
        client.logout()


def collect_overseas(config: dict) -> dict:
    assets = [a for a in config.get("overseas_assets", []) if a.get("enabled")]
    if not assets:
        return {}
    client = OverseasClient()
    prices = client.get_prices(assets)
    labels = {a["key"]: a["label"] for a in assets}
    return {"prices": prices, "labels": labels}


def collect_ecos(config: dict) -> dict:
    ecos_cfg = config.get("ecos", {})
    if not ecos_cfg.get("enabled") or is_placeholder(ecos_cfg.get("api_key")):
        return {}
    value = get_kr_3y_bond_yield(ecos_cfg["api_key"])
    return {"kr_3y_bond_yield": value} if value is not None else {}


def pct_change(current, baseline) -> str:
    if current is None or baseline is None or baseline == 0:
        return "기준값 없음"
    change = (current - baseline) / baseline * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.2f}%"


def fmt_num(v, decimals=2) -> str:
    if v is None:
        return "N/A"
    return f"{v:,.{decimals}f}"


def build_message(now: datetime, slot_name: str, current: dict, baseline: dict, config: dict) -> str:
    lines = [f"[지수 알리미] {now.strftime('%Y-%m-%d %H:%M')} ({slot_name})"]

    # 국내 지수
    for market_name, market_label in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        cur_val = current.get(f"{market_name}_index")
        base_val = (baseline or {}).get(f"{market_name}_index")
        if cur_val is not None:
            lines.append(
                f"- {market_label} 지수: {fmt_num(cur_val)} (기준대비 {pct_change(cur_val, base_val)})"
            )

    # 국내 수급 (개인/기관/외국인 순매수) - 당일 누적치라 기준대비 %는 표기하지 않음
    for market_name, market_label in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        netbuy = current.get(f"{market_name}_investor_netbuy")
        if netbuy:
            lines.append(
                f"- {market_label} 순매수(개인/기관/외국인): "
                f"{fmt_num(netbuy.get('individual'), 0)} / "
                f"{fmt_num(netbuy.get('institution'), 0)} / "
                f"{fmt_num(netbuy.get('foreign'), 0)} (백만원)"
            )

    # 국내 거래대금 상위 5종목
    for market_name, market_label in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        top = current.get(f"{market_name}_top_trading_value")
        if top:
            names = ", ".join(f"{t['name']}({fmt_num(t['trading_value'], 0)}백만)" for t in top)
            lines.append(f"- {market_label} 거래대금 상위: {names}")

    # 한국 국채 3년물
    kr_bond = current.get("kr_3y_bond_yield")
    if kr_bond is not None:
        base_bond = (baseline or {}).get("kr_3y_bond_yield")
        lines.append(f"- 한국 국채 3년물: {fmt_num(kr_bond)}% (기준대비 {pct_change(kr_bond, base_bond)})")

    # 해외 자산
    overseas_current = current.get("overseas_prices", {})
    overseas_labels = current.get("overseas_labels", {})
    overseas_baseline = (baseline or {}).get("overseas_prices", {})
    if overseas_current:
        lines.append("- 해외 자산:")
        for key, price in overseas_current.items():
            label = overseas_labels.get(key, key)
            base_price = overseas_baseline.get(key)
            lines.append(f"  · {label}: {fmt_num(price)} (기준대비 {pct_change(price, base_price)})")

    return "\n".join(lines)


def resolve_baseline_key(today, slot_name: str, slots_config: dict) -> str:
    baseline_type = slots_config[slot_name]["baseline"]
    if baseline_type == "prev_evening":
        return SnapshotStore.make_key(today - timedelta(days=1), "evening")
    elif baseline_type == "today_morning":
        return SnapshotStore.make_key(today, "morning")
    else:
        raise ValueError(f"알 수 없는 baseline 타입: {baseline_type}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=["morning", "afternoon", "evening"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="카카오톡 발송 없이 메시지만 출력")
    args = parser.parse_args()

    config = load_config(args.config)
    now = datetime.now()
    today = now.date()

    logger.info("=== %s 브리핑 시작 (%s) ===", args.slot, now.isoformat())

    current = {}
    try:
        current.update(collect_domestic(config))
    except Exception:
        logger.exception("국내 지표 수집 중 오류 발생 - 국내 지표 없이 계속 진행합니다.")

    try:
        overseas = collect_overseas(config)
        current["overseas_prices"] = overseas.get("prices", {})
        current["overseas_labels"] = overseas.get("labels", {})
    except Exception:
        logger.exception("해외 지표 수집 중 오류 발생 - 해외 지표 없이 계속 진행합니다.")

    try:
        current.update(collect_ecos(config))
    except Exception:
        logger.exception("ECOS 지표 수집 중 오류 발생")

    store = SnapshotStore(config.get("snapshot_file", "snapshots.json"))
    baseline_key = resolve_baseline_key(today, args.slot, config["schedule"]["slots"])
    baseline = store.get(baseline_key)
    if baseline is None:
        logger.warning("기준 스냅샷(%s)이 없습니다. 이번 브리핑은 변동률 없이 발송됩니다.", baseline_key)

    message = build_message(now, args.slot, current, baseline, config)
    logger.info("생성된 메시지:\n%s", message)

    # 이번 실행 결과를 스냅샷으로 저장 (다음 브리핑에서 기준점으로 사용)
    own_key = SnapshotStore.make_key(today, args.slot)
    store.put(own_key, current)
    store.prune_older_than(days=14)

    if args.dry_run:
        print("\n--- DRY RUN: 카카오톡 발송 생략 ---\n")
        print(message)
        return

    kakao_cfg = config["kakao"]
    if is_placeholder(kakao_cfg.get("rest_api_key")):
        logger.warning("카카오 API 키가 설정되지 않아 발송을 건너뜁니다. (--dry-run 으로 메시지만 확인 가능)")
        return

    notifier = KakaoNotifier(
        rest_api_key=kakao_cfg["rest_api_key"],
        token_file=kakao_cfg.get("token_file", "kakao_token.json"),
        max_message_length=kakao_cfg.get("max_message_length", 350),
    )
    notifier.send_message(message)
    logger.info("=== %s 브리핑 완료 ===", args.slot)


if __name__ == "__main__":
    main()
