# -*- coding: utf-8 -*-
"""
collect_risk_events.py — DART 공시 목록에서 '위험 공시'(자금조달·재무 악화·부정·상장 위험)를 모은다.

대상(공시 보고서명으로 분류)
  유상증자 · 전환사채(CB) · 신주인수권부사채(BW) · 감자      주요사항보고서(pblntf_ty=B)
  횡령배임 · 감사의견(부적정·거절) · 회생 · 관리종목·상장폐지 · 자본잠식 · 불성실공시   거래소 수시공시(I001)·시장조치(I003)
같은 종목에서 30일 안에 되풀이되는 같은 유형은 처음 한 건만 남긴다. 정정 공시('[기재정정]' 등)는 첫 공시일이 아니므로 뺀다.
결과: dart_cache/risk_events.json — 월 단위로 저장하므로 중단 후 다시 실행하면 이어서 한다.

사용 예
    python collect_risk_events.py --start 2019-01 --end 2026-10
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import dart_core as dc

OUT = os.path.join(dc.CACHE_DIR, "risk_events.json")
OUT_DELISTED = os.path.join(dc.CACHE_DIR, "risk_events_delisted.json")
LISTED = ("Y", "K")
DELISTED = ("E",)          # 상장폐지된 회사는 DART 목록에서 구분이 'E'(기타법인)로 바뀌지만 종목코드는 남는다 → 별도로 모아 생존 편향을 없앤다

# (유형, 보고서명 정규식) — 공백·괄호 접두어를 지운 이름에 적용
B_RULES = [("유상증자", r"유상증자결정|유무상증자결정"), ("CB", r"^주요사항보고서\(전환사채권발행결정\)"), ("BW", r"신주인수권부사채권발행결정"),
           ("감자", r"감자결정"), ("회생", r"회생절차개시신청")]
I_RULES = [("횡령배임", r"횡령.?배임(혐의발생|사실확인)"),
           ("감사의견", r"의견부적정|의견거절|감사의견.*(부적정|거절)"),
           ("회생", r"^회생절차개시결정|^회생절차개시신청"),
           ("관리종목·상폐", r"관리종목지정|상장적격성실질심사.*(사유발생|대상결정)|상장폐지사유|투자유의안내\(상장폐지기준해당\)|형식적상장폐지"),
           ("자본잠식", r"자본잠식"),
           ("불성실공시", r"^불성실공시법인지정(?!예고|유예|미)")]


def norm(name):
    return re.sub(r"\s", "", name)


def pull(bgn, end, **kw):
    rows, page = [], 1
    while True:
        d = dc.get_json("list.json", bgn_de=bgn, end_de=end, page_no=page, page_count=dc.PAGE_COUNT, last_reprt_at="N", **kw)
        rows += d.get("list", [])
        if page >= int(d.get("total_page", 0) or 0):
            return rows
        page += 1


def classify(rows, rules, classes=LISTED):
    out = []
    for r in rows:
        if r.get("corp_cls") not in classes or not r.get("stock_code"):
            continue
        nm = norm(r["report_nm"])
        if nm.startswith("["):                      # [기재정정]·[첨부정정]·[발행조건확정] 등 — 최초 공시가 아니다
            continue
        for typ, pat in rules:
            if re.search(pat, nm):
                out.append({"code": r["stock_code"], "name": r["corp_name"], "cls": r["corp_cls"], "dt": r["rcept_dt"], "rno": r["rcept_no"],
                            "report": r["report_nm"], "type": typ})
                break
    return out


def month_windows(start, end):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        first = dt.date(y, m, 1)
        nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
        yield f"{y}-{m:02d}", first.strftime("%Y%m%d"), (nxt - dt.timedelta(days=1)).strftime("%Y%m%d")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def save_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2019-01")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delisted", action="store_true", help="상장폐지된 회사(구분 E + 종목코드)만 모아 risk_events_delisted.json에 저장")
    a = ap.parse_args()
    out, classes = (OUT_DELISTED, DELISTED) if a.delisted else (OUT, LISTED)

    done = {}
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            done = json.load(f)
    todo = [w for w in month_windows(a.start, a.end) if w[0] not in done]
    print(f"월 {len(todo)}개 남음(이미 {len(done)}개)", flush=True)
    lock, n = Lock(), 0

    def work(w):
        key, bgn, end = w
        ev = classify(pull(bgn, end, pblntf_ty="B"), B_RULES, classes)
        ev += classify(pull(bgn, end, pblntf_detail_ty="I001"), I_RULES, classes)
        ev += classify(pull(bgn, end, pblntf_detail_ty="I003"), I_RULES, classes)
        return key, ev

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in [ex.submit(work, w) for w in todo]:
            try:
                key, ev = fut.result()
            except Exception as e:                  # DartError(키 없는 메시지) — 다음 실행에서 해당 월을 다시 시도
                print("  실패:", type(e).__name__, str(e)[:80], flush=True)
                continue
            with lock:
                done[key] = ev
                n += 1
                save_atomic(out, done)
                print(f"  {key}: {len(ev)}건 ({n}/{len(todo)})", flush=True)
    print("완료", flush=True)


if __name__ == "__main__":
    sys.exit(main())
