# -*- coding: utf-8 -*-
"""
투자 분석 사이트(버핏연구소·Stock Issue Service·SMIC)의 새 글을 모아 invest_sites_data.json을 갱신한다.
GitHub Actions(.github/workflows/update_invest_sites.yml)가 평일 장 마감 뒤 매일 실행하고, 앱의 '투자사이트' 탭이 이 파일을 읽는다.

    python update_invest_sites.py                      # 최근 10일 글 수집(LLM 요약은 GROQ_API_KEY가 있을 때만)
    python update_invest_sites.py --backfill-days 365  # 과거 1년치 목록을 모아 영향도 표본을 늘린다(최초 1회)
    python update_invest_sites.py --no-llm --days 3    # 앱의 '지금 수집'과 같은 가벼운 모드

과거 글(--days 밖)은 목록의 제목·리드·날짜만 쓰고 본문은 받지 않는다. 영향도 계산에는 종목명과 날짜만 있으면 되기 때문이다.
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

import invest_sites_core as isc

DEFAULT_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invest_sites_data.json")
KEEP_DAYS = 560            # 보관 기간(영향도 표본 + 여유)
RECENT_DAYS = 60           # 이 기간의 글만 invest_sites_data.json에 전체 필드로 둔다. 그 이전은 월별 샤드(invest_sites_history/)
MAX_PAGES = 400
INDEX_SYMBOLS = {"KOSPI": "KS11", "KOSDAQ": "KQ11"}


def crawl_list(fetcher, source, cutoff, max_pages=MAX_PAGES, log=print):
    """목록을 1페이지부터 넘기며 cutoff(YYYY-MM-DD)보다 오래된 글만 남은 페이지에서 멈춘다."""
    parse = {"buffett": isc.parse_buffett_list, "eduinfom": isc.parse_eduinfom_list, "smic": isc.parse_smic_list}[source]
    items, seen_ids = [], set()
    for page in range(1, max_pages + 1):
        html = fetcher.get(isc.SOURCES[source]["list_url"].format(page=page))
        if not html:
            break
        got = [i for i in parse(html) if i["id"] not in seen_ids]
        if not got:
            break
        seen_ids.update(i["id"] for i in got)
        items += got
        dated = [i["date"] for i in got if i["date"]]
        if dated and max(dated) < cutoff:
            break
    log(f"  {isc.SOURCES[source]['name']}: 목록 {len(items)}건 ({page}페이지)")
    return [i for i in items if not i["date"] or i["date"] >= cutoff]


def fetch_detail(fetcher, source, item):
    if source == "buffett":
        html = fetcher.get(item["url"])
        return isc.parse_buffett_article(html) if html else None
    if source == "eduinfom":
        html = fetcher.get(item["url"])
        return isc.parse_eduinfom_article(html) if html else None
    html = fetcher.get(item["url"])
    pdf = isc.parse_smic_post(html)["pdf"] if html else None
    content = fetcher.get(pdf, binary=True) if pdf else None
    if not content:
        return None
    try:
        return {"text": isc.extract_pdf_text(content), "pdf": pdf}
    except Exception:                                   # noqa: BLE001 — 깨진 PDF 한 건 때문에 전체가 멈추면 안 된다
        return None


def load_prices(codes_markets, start, log=print):
    """종목별 종가와 시장 지수 종가. 실패한 종목은 빠진다."""
    import FinanceDataReader as fdr
    idx = {}
    for m, sym in INDEX_SYMBOLS.items():
        try:
            idx[m] = fdr.DataReader(sym, start)["Close"].astype(float).dropna()
        except Exception:                               # noqa: BLE001
            idx[m] = None

    def one(code):
        try:
            d = fdr.DataReader(code, start)
            return code, d["Close"].astype(float).dropna()
        except Exception:                               # noqa: BLE001
            return code, None

    with ThreadPoolExecutor(6) as ex:
        prices = {c: s for c, s in ex.map(one, sorted(codes_markets)) if s is not None and len(s) > 30}
    log(f"  주가 {len(prices)}/{len(codes_markets)}종목 로드")
    return prices, idx


def attach_reactions(articles, today, log=print):
    """글별 주가 반응을 (재)계산. 공표 후 20거래일이 아직 안 지난 글과 반응이 없는 글만 다시 계산한다."""
    todo = []
    for a in articles:
        if not a.get("stocks") or not a.get("date"):
            continue
        age = (today - datetime.strptime(a["date"], "%Y-%m-%d")).days
        rx = a.get("reaction") or {}
        if not rx or ("abn_post20" not in rx and age < 60) or age <= 3:
            todo.append(a)
    if not todo:
        return
    start = (min(datetime.strptime(a["date"], "%Y-%m-%d") for a in todo) - timedelta(days=90)).strftime("%Y-%m-%d")
    codes = {a["stocks"][0]["code"]: a["stocks"][0].get("market", "KOSPI") for a in todo}
    prices, idx = load_prices(codes, start, log)
    done = 0
    for a in todo:
        s0 = a["stocks"][0]
        close = prices.get(s0["code"])
        if close is None:
            continue
        rx = isc.compute_reaction(close, idx.get("KOSDAQ" if s0.get("market") == "KOSDAQ" else "KOSPI"), a["date"])
        if rx:
            a["reaction"] = rx
            done += 1
    log(f"  주가 반응 계산 {done}/{len(todo)}건")


def find_groq_key():
    """환경변수(GitHub Actions secret) 우선, 없으면 로컬 .streamlit/secrets.toml(앱과 같은 키)에서 읽는다. 값은 출력하지 않는다."""
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f).get("GROQ_API_KEY")
    except (OSError, ImportError, ValueError):
        return None


def run_update(data_path=DEFAULT_DATA, days=10, backfill_days=0, use_llm=True, max_llm=25, sources=("buffett", "eduinfom", "smic"), log=print):
    now = datetime.now(isc.KST).replace(tzinfo=None)
    list_days = max(days, backfill_days)
    cutoff = (now - timedelta(days=list_days)).strftime("%Y-%m-%d")
    detail_cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    data = isc.load_data(data_path)
    hist_dir = os.path.join(os.path.dirname(os.path.abspath(data_path)), "invest_sites_history")
    existing = {a["id"]: a for a in isc.load_history(hist_dir) + data["articles"]}   # 같은 id면 일 단위 파일의 전체 필드 기록이 이긴다
    fetcher = isc.Fetcher()
    name_map = isc.build_name_map()
    log(f"수집 시작: 목록 {list_days}일, 상세 {days}일, 기존 {len(existing)}건")
    api_key = find_groq_key() if use_llm else None
    llm_left = max_llm if api_key else 0
    new_cnt = 0
    for source in sources:
        items = crawl_list(fetcher, source, cutoff, log=log)
        if source == "buffett":
            items = [i for i in items if i["title"].startswith(isc.BUFFETT_TAGS)]
        for it in items:
            old = existing.get(it["id"])
            want_detail = bool(it["date"] and it["date"] >= detail_cutoff)
            if old and (old.get("has_detail") or not want_detail):
                continue                                 # 이미 처리했고 상세가 더 필요하지 않다
            detail = fetch_detail(fetcher, source, it) if want_detail else None
            llm_ok = llm_left > 0 and detail and source != "eduinfom"
            rec = isc.build_article(source, it, detail, name_map, use_llm=bool(llm_ok), api_key=api_key)
            if llm_ok:
                llm_left -= 1
            if old and old.get("reaction"):
                rec["reaction"] = old["reaction"]
            if source != "smic" and not rec["stocks"]:
                continue                                 # 종목을 못 찾은 시황·기타 글은 보관하지 않는다
            existing[rec["id"]] = rec
            new_cnt += 1
    articles = sorted(existing.values(), key=lambda a: (a.get("date") or "", a["id"]), reverse=True)
    keep_from = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    articles = [a for a in articles if (a.get("date") or "9999") >= keep_from]
    log(f"새로 처리한 글 {new_cnt}건 → 보관 {len(articles)}건 (LLM 요약 {max_llm - llm_left if api_key else 0}건)")
    attach_reactions(articles, now, log)
    recent_from = (now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    data["articles"] = [a for a in articles if (a.get("date") or "") >= recent_from]
    shards = isc.save_history(hist_dir, [a for a in articles if (a.get("date") or "") < recent_from])
    impact = isc.aggregate_impact(articles)
    data["impact"] = impact.replace({float("nan"): None}).to_dict("records") if len(impact) else []
    by = pd.Series([a["source"] for a in articles]).value_counts().to_dict()
    data["meta"] = {"sources": by, "with_reaction": sum(1 for a in articles if a.get("reaction"))}
    isc.save_data(data_path, data)
    log(f"저장: {data_path} (최근 {RECENT_DAYS}일 {len(data['articles'])}건) + 월별 샤드 {shards}개 갱신 · 전체 출처별 {by} · 주가 반응 있는 글 {data['meta']['with_reaction']}건")
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--days", type=int, default=10, help="본문·요약까지 받을 최근 일수")
    ap.add_argument("--backfill-days", type=int, default=0, help="목록(제목·날짜)만 더 과거까지 모을 일수")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--max-llm", type=int, default=25)
    ap.add_argument("--sources", default="buffett,eduinfom,smic")
    args = ap.parse_args()
    run_update(args.data, args.days, args.backfill_days, not args.no_llm, args.max_llm, tuple(args.sources.split(",")))


if __name__ == "__main__":
    sys.exit(main())
