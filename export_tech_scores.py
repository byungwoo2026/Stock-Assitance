# -*- coding: utf-8 -*-
"""
Stock_Assitance의 기술점수 전체 스캔풀(~100종목)을 계산해 tech_score_export.json으로 저장한다.

2026-09-20 신설 — 별도 저장소인 Stock_Agent(자동매매 신호 에이전트)가 이 파일을
raw.githubusercontent.com으로 직접 읽어, 자체 STEP4 후보(~40종목) 필터링에 "그날 기술점수
분포의 하위 25%"를 컷 기준으로 쓴다(실측 검증: Stock_Agent의 517건 실제 픽에 이 기술점수
공식을 소급 적용했을 때 상관계수 +0.092, 최하위 사분위 평균수익률 -2.53% vs 최상위 사분위
+0.68%로 뚜렷한 개선 확인됨).

GitHub Actions(.github/workflows/export_tech_scores.yml)로 매 평일 장마감 후 헤드리스
실행되며, streamlit 앱과 독립적으로 동작한다. 대시보드 출력에는 영향을 주지 않는다.
"""
import json
import sys

from screener_core import now_kst, score_full_pool

OUTPUT_FILE = "tech_score_export.json"
MIN_POOL_SIZE = 20  # 표본이 너무 적으면 사분위 계산이 불안정 — 파일을 갱신하지 않고 실패 처리


def main():
    pool = score_full_pool(pool_size=100)
    if len(pool) < MIN_POOL_SIZE:
        print(f"스코어링 성공 종목이 {len(pool)}개뿐 — 파일을 갱신하지 않고 실패 처리 "
              f"(어제자 파일이 그대로 남아, Stock_Agent 쪽 신선도 검증에서 자동으로 결측 취급됨)")
        sys.exit(1)

    # 2026-09-20: now_kst().date()가 아니라 실제 마지막 OHLCV 바 날짜로 기록 — 휴장일에
    # 워크플로우가 실행돼도(예: 스케줄러 자체는 평일에만 돌지만 KRX 공휴일과는 무관하게
    # 실행될 수 있음) 자동으로 직전 실거래일로 자기보정된다. 별도 휴장일 캘린더 불필요
    # (Stock_Agent가 2026-09 초 겪었던 "장 시작 전 가격비교로 휴장일 오판" 문제와 같은
    # 함정을 애초에 피하는 설계).
    trading_date = max(s["last_date"] for s in pool).isoformat()

    payload = {
        "computed_at": now_kst().isoformat(),
        "trading_date": trading_date,
        "universe_size": len(pool),
        "scores": [{"code": s["code"], "name": s["name"], "tech_score": s["tech_score"]} for s in pool],
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"저장 완료: {OUTPUT_FILE} ({trading_date}, {len(pool)}종목)")


if __name__ == "__main__":
    main()
