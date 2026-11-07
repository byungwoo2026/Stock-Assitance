import streamlit as st
import pandas as pd
import FinanceDataReader as fdr
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime, timedelta, timezone
import time
import xml.etree.ElementTree as ET
import json
import threading
# pyrefly: ignore [missing-import]
try:
    from groq import Groq
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# 2026-09-20 — KST/now_kst/get_naver_session/기술지표·기술점수 계산 로직은 screener_core.py로
# 이동(streamlit 비의존 순수 모듈). Stock_Agent(별도 저장소)가 이 모듈의 score_full_pool()
# 결과를 교차저장소로 받아 쓰므로, 이 파일과 로직이 어긋나지 않도록 여기서도 그대로 import해 쓴다.
from screener_core import (
    KST, now_kst, get_naver_session,
    compute_technical_indicators, compute_technical_score_at_row, score_full_pool,
)

# 2026-10-31 — 'AI 종목 진단'(추세·패턴·수급·거래량 체크리스트 → 등급)의 규칙 엔진과 Plotly 차트 빌더.
# 기존 calculate_stock_score(눌림목 철학)와 점수를 섞지 않도록 별도 모듈로 분리했고, 둘 다 streamlit에 의존하지 않는다.
import diagnosis_core as dc
import diagnosis_chart as dchart
import watchlist_core as wlc   # 관심종목 JSON 구조·검증(streamlit 비의존)
import os
from collections import Counter
import invest_sites_core as isc      # '투자사이트' 탭: 분석 글 수집·종목 추출·요약·주가 영향도(streamlit 비의존)
import update_invest_sites as isu     # 같은 수집기를 GitHub Actions가 매일 실행하고, 탭의 '지금 수집'도 호출한다
import risk_disclosure as rdisc       # 'AI 종목 진단'의 위험 공시 경고(DART) — 검증을 통과한 감자·관리종목/상장폐지 사유·감사의견만
import company_report as crep         # 'AI 종목 진단'의 종목 분석 보고서(DART 숫자만: 재무·현금흐름·매출 구성·주주·공시·피어 비교·밸류 시뮬레이터)
import segment_mix as smix            # 보고서의 매출 구성(사업부문·제품·지역)
import peer_compare as pcmp           # 보고서의 피어 비교(같은 업종 시총 비슷한 종목을 같은 방식으로 계산)
import filing_text as ftext           # 보고서의 AI 서술 재료: 사업보고서 본문 섹션(사업의 개요·경영진단·제재·소송·핵심감사사항)
import report_narrative as rnar       # 보고서의 AI 서술(사업 요약·변화·비판적 리스크). LLM은 서술만, 숫자·인용은 코드가 검증
import dart_core
import plotly.graph_objects as go
from streamlit.errors import StreamlitAPIException

# Groq가 특정 모델을 예고 없이 단종시킨 전례가 있어(예: llama-3.3-70b-versatile),
# 최우선 모델이 실패하면 다음 모델로 자동 전환. 가끔 갱신이 필요할 수 있음(2026-09-16 기준 확인).
GROQ_MODEL_CANDIDATES = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# 재시도/모델 전환으로 해결되지 않는 오류(인증·접근 차단)는 즉시 중단하고 원인을 알기 쉬운 문구로 안내
GROQ_FATAL_STATUS_MESSAGES = {
    401: "Groq API 키가 유효하지 않습니다(401). Secrets의 GROQ_API_KEY 값을 확인해 주세요.",
    403: "Groq API가 이 서버의 네트워크 접근을 차단했습니다(403 Access denied). "
         "잠시 후 다시 시도하거나, Streamlit Cloud의 Manage app → Reboot app으로 앱을 재부팅해 보세요.",
}

def call_groq_chat(messages, temperature=None, max_retries=2):
    """Groq 채팅 완성 호출 공통 헬퍼.
    모델별로 최대 max_retries회 짧게 재시도하고, 그래도 실패하면 다음 후보 모델로 넘어간다.
    단, 401/403처럼 재시도해도 소용없는 오류는 즉시 중단하고 안내 문구를 반환한다.
    반환: (성공 여부, 성공 시 응답 텍스트 / 실패 시 마지막 에러 메시지)
    """
    last_err = None
    for model in GROQ_MODEL_CANDIDATES:
        for attempt in range(max_retries):
            try:
                client = Groq(api_key=st.secrets["GROQ_API_KEY"])
                kwargs = {"model": model, "messages": messages}
                if temperature is not None:
                    kwargs["temperature"] = temperature
                response = client.chat.completions.create(**kwargs)
                return True, response.choices[0].message.content
            except Exception as e:
                fatal_msg = GROQ_FATAL_STATUS_MESSAGES.get(getattr(e, "status_code", None))
                if fatal_msg:
                    return False, fatal_msg
                last_err = e
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
    return False, str(last_err)

st.set_page_config(page_title="나만의 투자 조수", layout="wide")

st.title("📈 나만의 AI 투자 조수 대시보드")
st.markdown("시장의 자금 쏠림, 주주 수급, 주요 뉴스 및 기술적 매수 신호를 분석합니다.")

# 화면 상단 가로 버튼(탭)형 메뉴 — st.radio를 버튼처럼 보이도록 CSS로 스타일링
MENU_OPTIONS = ["종합 대시보드", "시장 자금 & 업종 분석", "주요 기업 헤드라인 뉴스", "외인 수급 & 기술적 조건 스크리너", "최우수 애널리스트 추천 종목", "가치재평가주", "퀀트 투자 리스트", "AI 종목 진단", "투자사이트", "개별종목분석"]

st.markdown("""
<style>
/* 상단 가로 메뉴를 탭/버튼처럼 스타일링 */
div[role="radiogroup"] {
    flex-direction: row;
    flex-wrap: wrap;
    gap: 8px;
    border-bottom: 2px solid #e6e6e6;
    padding-bottom: 12px;
    margin-bottom: 8px;
}
div[role="radiogroup"] > label {
    background-color: #f0f2f6;
    padding: 8px 16px;
    border-radius: 8px 8px 0 0;
    cursor: pointer;
    border: 1px solid #e6e6e6;
    border-bottom: none;
    transition: background-color 0.15s ease;
}
div[role="radiogroup"] > label:hover {
    background-color: #e2e6ee;
}
div[role="radiogroup"] > label[data-baseweb="radio"]:has(input:checked) {
    background-color: #ff4b4b;
    border-color: #ff4b4b;
}
div[role="radiogroup"] > label[data-baseweb="radio"]:has(input:checked) div {
    color: white !important;
}
/* 각 라디오 버튼의 동그라미(원형 선택 표시)는 숨겨서 순수 버튼처럼 보이게 함 */
div[role="radiogroup"] > label > div:first-child {
    display: none;
}
</style>
""", unsafe_allow_html=True)

menu = st.radio("메뉴 선택", MENU_OPTIONS, horizontal=True, label_visibility="collapsed")

# 차단 없는 네이버 뉴스 RSS 엔진
@st.cache_data(ttl=3600)  # 1시간 캐싱
def fetch_headlines_rss(keyword, max_n=5, period="7d"):
    headlines = []
    # 네이버 공식 뉴스 RSS 검색 URL (정확도순)
    url = f"https://news.google.com/rss/search?q={keyword}+when:{period}&hl=ko&gl=KR&ceid=KR:ko"
    
    # 브라우저처럼 보이도록 User-Agent 지정 (Google이 봇으로 판단해 차단할 확률을 낮춤)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)

        # User-Agent를 넣어도 Google이 여전히 차단(503 등)할 수 있으므로,
        # 200이 아니면 XML 파싱을 아예 시도하지 않고 조용히 빈 리스트로 반환
        if response.status_code != 200:
            st.caption(f"⚠️ 뉴스 조회 일시 실패(상태코드 {response.status_code}) — 이 종목은 뉴스 점수를 중립으로 처리합니다.")
            return headlines

        root = ET.fromstring(response.text) # 파이썬 기본 XML 파서 사용
        items = root.findall('.//item')
        
        for item in items[:max_n]: # 상위 N개 헤드라인 추출
            title_elem = item.find('title')
            link_elem = item.find('link')
            
            title = title_elem.text if title_elem is not None else ""
            link = link_elem.text if link_elem is not None else "#"
            
            # 구글 RSS 타이틀 특성상 뒤에 붙는 ' - 언론사' 분리 처리
            press = "주요 언론"
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                title = parts[0]
                press = parts[1]
                
            if title:
                headlines.append({"title": title, "press": press, "link": link})
    except Exception:
        # 상태코드는 200인데 XML 파싱 자체가 실패하는 등 예외 상황도
        # 화면을 빨간 에러박스로 깨뜨리지 않고 조용히 넘어가도록 처리
        st.caption("⚠️ 뉴스 조회 중 예기치 못한 오류 — 이 종목은 뉴스 점수를 중립으로 처리합니다.")
    return headlines

# 코스피/코스닥 데이터 안정적 수집 함수 (주말/새벽 서버 오류 및 차단 방지)
# NOTE(2026-07-20): 기존 HTML 스크래핑 방식은 "상승/하락" 텍스트를 문자열로 판별하다가
# 방향(부호)이 실제와 반대로 표시되는 버그가 있어, 부호가 이미 포함된 네이버 JSON API로 교체함.
@st.cache_data(ttl=300) # 5분 캐싱으로 잦은 요청 방지
def fetch_market_index(market_type="KOSPI", retries=3):
    url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{market_type}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    for attempt in range(retries):
        try:
            res = requests.get(url, headers=headers, timeout=5)
            res.raise_for_status()
            data = res.json()
            item = data["datas"][0]

            close_price = str(item.get("closePrice", "")).replace(",", "")
            ratio = str(item.get("fluctuationsRatio", "")).replace(",", "")
            direction_code = str(item.get("compareToPreviousPrice", {}).get("code", ""))

            if not close_price or not ratio:
                raise ValueError("응답 형식을 해석할 수 없습니다.")

            ratio_val = abs(float(ratio))
            # 네이버 방향 코드: 2=상승, 5=하락 (그 외 보합 등은 부호 없이 처리)
            if direction_code == "5":
                ratio_val = -ratio_val
            elif direction_code == "2":
                ratio_val = abs(ratio_val)

            change_val = f"{ratio_val:+.2f}%"

            return {"index": f"{float(close_price):,.2f}", "change": change_val, "status": "success"}

        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return {"index": "조회 지연", "change": "-", "status": "timeout"}

        except requests.exceptions.RequestException:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"index": "서버 점검/오류", "change": "-", "status": "network_error"}

        except Exception:
            return {"index": "데이터 오류", "change": "-", "status": "error"}

@st.cache_data(ttl=3600) # 1시간 단위 캐싱 (1달 추세이므로 자주 변하지 않음)
def fetch_1month_sector_trends():
    """대표 섹터 ETF들의 과거 1달(22영업일) 주가 데이터를 통해 진짜 자금 유입 업종 분석"""
    
    # 핵심 산업 섹터와 해당 섹터를 대표하는 ETF 종목코드 매핑
    sector_etfs = {
        "반도체": "091230",          # TIGER 반도체
        "2차전지": "305080",        # TIGER 2차전지테마
        "바이오/헬스케어": "244580", # KODEX 바이오
        "자동차": "091180",          # KODEX 자동차
        "전력기기 및 인프라": "476080", # KODEX AI전력핵심설비
        "조선/중공업": "091210",     # TIGER 200 중공업
        "은행/금융": "091220",       # KODEX 은행
        "원자력": "433420",          # KODEX 원자력핵심테마
        "소프트웨어(IT)": "157490",  # TIGER 소프트웨어
        "방산": "456340"             # KODEX 방산
    }
    
    results = []
    # 영업일 기준 22일(약 1달)을 확보하기 위해 약 45일 전 데이터부터 조회
    start_date = (now_kst() - timedelta(days=45)).strftime('%Y-%m-%d')
    
    for sector, symbol in sector_etfs.items():
        try:
            df = fdr.DataReader(symbol, start_date)
            if df.empty or len(df) < 22:
                continue
            
            df_recent = df.tail(22)
            start_price = float(df_recent.iloc[0]['Close']) # 1달 전 종가
            end_price = float(df_recent.iloc[-1]['Close'])    # 현재 종가
            
            # 1달 수익률 산출
            return_rate = (end_price - start_price) / start_price * 100
            
            results.append({
                "업종/테마": sector,
                "최근1달수익률(%)": return_rate,
                "변동": f"+{return_rate:.2f}%" if return_rate > 0 else f"{return_rate:.2f}%"
            })
        except Exception as e:
            continue
            
    # 등락률 기준으로 내림차순 정렬
    results = sorted(results, key=lambda x: x['최근1달수익률(%)'], reverse=True)
    return results[:5], now_kst().strftime('%H:%M')

@st.cache_data(ttl=3600)
def fetch_top_market_cap(market_type="KOSPI", top_n=20):
    # NOTE(2026-09-16): finance.naver.com의 sise_market_sum.naver 등 메인 시세 페이지가
    # Next.js 기반으로 전면 개편되어 서버 렌더링 HTML이 사라짐 → 모바일 웹이 쓰는
    # m.stock.naver.com JSON API로 대체 (pageSize 최대 100)
    url = f"https://m.stock.naver.com/api/stocks/marketValue/{market_type}?page=1&pageSize={min(top_n, 100)}"
    try:
        res = get_naver_session().get(url, timeout=5)
        data = res.json()
        return [s['stockName'] for s in data.get('stocks', [])], now_kst().strftime('%H:%M')
    except Exception as e:
        st.error(f"시가총액 데이터 수집 오류: {e}")
        return [], now_kst().strftime('%H:%M')

@st.cache_data(ttl=3600)
def fetch_upper_limit_stocks():
    # NOTE(2026-09-16): finance.naver.com/sise/sise_upper.naver가 Next.js 개편으로
    # 서버 렌더링 HTML을 제공하지 않게 되어, m.stock.naver.com의 시가총액 순위 API를
    # 전 종목 페이지네이션으로 훑어 상한가(compareToPreviousPrice.code == "1") 종목만 필터링.
    # (m.stock.naver.com은 매 요청 새 TLS 연결 시 느려서, 세션 재사용 + 순차 조회가
    #  오히려 세션 없는 병렬 요청보다 훨씬 빠르다 — 연결 재사용 시 페이지당 수십 ms 수준)
    session = get_naver_session()
    results = {"KOSPI": [], "KOSDAQ": []}
    PAGE_SIZE = 100
    MAX_PAGES_SAFETY = 60  # 응답 이상으로 totalCount가 비정상적으로 크게 와도 무한 조회 방지

    def fetch_page(market, page):
        url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize={PAGE_SIZE}"
        res = session.get(url, timeout=5)
        res.raise_for_status()
        return res.json()

    for market in results.keys():
        try:
            first_page = fetch_page(market, 1)
        except Exception:
            continue

        total_count = first_page.get('totalCount', 0)
        total_pages = min(-(-total_count // PAGE_SIZE), MAX_PAGES_SAFETY)  # 올림 나눗셈
        all_pages = [first_page]

        for page in range(2, total_pages + 1):
            try:
                all_pages.append(fetch_page(market, page))
            except Exception:
                continue

        for page_data in all_pages:
            for s in page_data.get('stocks', []):
                if (s.get('compareToPreviousPrice') or {}).get('code') == '1':
                    results[market].append(s['stockName'])

    return results, now_kst().strftime('%H:%M')

# 순매수 상위 집계 기간 (화면 표시명 → API periodType). DAY는 가장 최근 영업일 하루 기준.
NET_BUYING_PERIODS = {"1일": "DAY", "1주일": "WEEK", "1개월": "MONTH", "3개월": "THREE_MONTH"}

@st.cache_data(ttl=3600)
def _fetch_net_buying_top_cached(investor_type, market_type, top_n, period):
    # NOTE(2026-10-01): 기존 finance.naver.com/sise/sise_deal_rank_iframe.naver 는 네이버가 종료(HTTP 410)함.
    # 새 Npay 증권(stock.naver.com)의 "투자자별 매매동향" 화면이 쓰는 JSON API로 교체.
    #   investorType: FOREIGNER(외국인) / ORGANIZATION(기관), marketType: KOSPI / KOSDAQ(대문자),
    #   periodType: DAY(가장 최근 영업일) / WEEK / MONTH / THREE_MONTH, tradeType: KRX
    url = "https://stock.naver.com/api/domestic/market/trend/trendForeignOrg"
    params = {
        "investorType": "FOREIGNER" if investor_type == "foreign" else "ORGANIZATION",
        "tradeType": "KRX",
        "marketType": market_type,
        "startIdx": 0,
        "pageSize": top_n,
        "periodType": period,
    }
    res = get_naver_session().get(url, params=params, timeout=5)
    res.raise_for_status()
    buy_list = (res.json().get("sections") or {}).get("buyRankList") or []
    stocks = []
    for item in buy_list:
        name = (item.get("itemname") or "").strip()
        if name and name not in stocks:
            stocks.append(name)
    if not stocks:
        # 빈 결과를 정상값으로 돌려주면 1시간 동안 캐시되므로, 예외로 올려 캐시되지 않게 함
        raise ValueError("순매수 상위 데이터가 비어 있음")
    return stocks[:top_n], now_kst().strftime('%H:%M')

def fetch_net_buying_top(investor_type="foreign", market_type="KOSPI", top_n=10, period="DAY"):
    """외국인/기관 순매수 상위 종목명 리스트와 조회 시각을 반환. period는 NET_BUYING_PERIODS의 API 값.
    실패하면 ([], 시각)을 반환하며, 실패 결과는 캐시되지 않는다(성공한 결과만 1시간 캐시)."""
    try:
        return _fetch_net_buying_top_cached(investor_type, market_type, top_n, period)
    except Exception:
        return [], now_kst().strftime('%H:%M')

# 최우수 애널리스트 추천 종목 필터 기준 (코드에 고정된 값 — 매경 순위가 바뀌면 아래 두 값을 함께 수동 갱신해야 함)
ANALYST_RANKING_BASIS = "매일경제 베스트 애널리스트 종합평가(리서치센터 부문) 최상위 5개사 기준"
ANALYST_LIST_LAST_UPDATED = "2026-07-27"

@st.cache_data(ttl=3600)
def fetch_top_analyst_recommendations():
    # 최근 매일경제 베스트 애널리스트(리서치센터 부문) 평가 최상위권 증권사 집중 필터링
    best_research_centers = ["신한투자증권", "하나증권", "메리츠증권", "KB증권", "NH투자증권"]
    per_broker_limit = 4   # 증권사 1곳당 최대 노출 개수 (특정 증권사가 결과를 독점하지 않도록 제한)
    max_pages = 60         # 메리츠/KB/NH 등은 리포트 발간 빈도가 낮아 앞쪽 페이지만으로는 안 걸릴 수 있어, 충분히 깊게 조회 (5개 증권사 모두 채워지면 중간에 조기 종료됨)

    broker_results = {bc: [] for bc in best_research_centers}
    seen = set()

    try:
        # NOTE(2026-09-16): finance.naver.com/research/company_list.naver가 Next.js 개편으로
        # 서버 렌더링 표를 더 이상 제공하지 않아, 모바일 웹이 쓰는 JSON API로 대체.
        # m.stock.naver.com은 매 요청 새 TLS 연결 시 느리므로(요청당 1초 이상) 세션을 재사용해
        # 순차 조회 — 연결이 재사용되면 최대 60페이지를 조회해도 수 초 이내로 끝난다.
        session = get_naver_session()
        for page in range(1, max_pages + 1):
            url = f"https://m.stock.naver.com/api/research/company?page={page}"
            res = session.get(url, timeout=5)
            if res.status_code != 200:
                break
            reports = res.json()
            if not reports:
                break

            for item in reports:
                stock = item.get('itemName', '')
                title = item.get('title', '')
                broker = item.get('brokerName', '')
                date_str = item.get('writeDate', '')
                link = item.get('endUrl', '#')

                matched_bc = next((bc for bc in best_research_centers if bc in broker), None)
                if not matched_bc:
                    continue
                if len(broker_results[matched_bc]) >= per_broker_limit:
                    continue

                dedup_key = (stock, title, broker)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                broker_results[matched_bc].append({
                    "종목명": stock,
                    "리포트 제목": title,
                    "발간 증권사": broker,
                    "발간일": date_str,
                    "링크": link
                })

            # 5개 증권사 모두 한도만큼 채워졌으면 더 이상 조회할 필요 없음
            if all(len(v) >= per_broker_limit for v in broker_results.values()):
                break

        # 특정 증권사가 결과를 독점하지 않도록, 증권사별로 번갈아가며 최종 리스트 구성
        results = []
        for i in range(per_broker_limit):
            for bc in best_research_centers:
                if i < len(broker_results[bc]):
                    results.append(broker_results[bc][i])

        return results, now_kst().strftime('%H:%M')
    except Exception:
        return [], now_kst().strftime('%H:%M')


def rsi_signal_label(rsi):
    """RSI 값을 매수/매도 판단 참고용 문구로 변환. compute_technical_score_at_row의 배점 구간과 동일 기준(≤40/≤55/≤70)."""
    if rsi <= 40: return "과매도·매수 관심"
    elif rsi <= 55: return "중립·매수 우호"
    elif rsi <= 70: return "중립·과열 주의"
    else: return "과매수·차익실현 고려"

def bb_signal_label(bb_pct_b):
    """볼린저밴드 %B 값을 매수/매도 판단 참고용 문구로 변환 (0=하단밴드, 1=상단밴드)."""
    if bb_pct_b <= 0.2: return "하단권·매수 관심"
    elif bb_pct_b <= 0.8: return "중립"
    else: return "상단권·과열 주의"

def stoch_signal_label(stoch_k):
    """스토캐스틱 %K 값을 매수/매도 판단 참고용 문구로 변환."""
    if stoch_k <= 20: return "과매도·매수 관심"
    elif stoch_k <= 80: return "중립"
    else: return "과매수·과열 주의"

def _parse_investor_qty(value):
    """네이버 API의 '+1,660,246' 형태 순매수 수량 문자열을 정수로 변환."""
    try:
        return int(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return 0

def compute_investor_streak_label(deal_trend_infos):
    """일별 외국인/기관 순매수 수량(0번째=최근일)으로부터 연속 매수/매도 상태를 사람이 읽는 문구로 변환.
    네이버 integration API가 최근 5거래일치만 제공해 표시 가능한 연속일수는 최대 5일로 제한됨."""
    if not deal_trend_infos:
        return "수급 데이터 없음"

    def streak_text(label, key):
        values = [_parse_investor_qty(d.get(key)) for d in deal_trend_infos]
        if not values or values[0] == 0:
            return None
        buying = values[0] > 0
        streak = 0
        for v in values:
            if v == 0 or (v > 0) != buying:
                break
            streak += 1
        direction = "매수" if buying else "매도"
        return f"{label} {streak}일 연속 {direction}" if streak >= 2 else f"{label} 당일 {direction}"

    parts = [t for t in (
        streak_text("외국인", "foreignerPureBuyQuant"),
        streak_text("기관", "organPureBuyQuant"),
    ) if t]
    return " · ".join(parts) if parts else "뚜렷한 수급 신호 없음"

@st.cache_data(ttl=3600)
def run_logical_screener():
    """
    개선된 논리적 스크리닝 기법 (점수 기반 랭킹 시스템):
    엄격한 AND 조건(0개 종목 검출 방지) 대신, 주도주(거래량 상위)를 대상으로 기술적 타점 점수(100점 만점)를 매겨 상위 20개를 항상 제시.

    2026-09-20 — 후보 수집·스코어링 자체는 screener_core.score_full_pool()로 이동(Stock_Agent가
    같은 로직을 교차저장소로 공급받아 쓰므로, 두 곳의 점수 계산이 어긋나지 않도록 단일 소스로
    통일). 여기서는 화면 표시용 top20 슬라이싱과 투자자수급(외국인/기관 연속매매) enrichment만
    담당 — 기존 동작(반환 형식·정렬순서)은 완전히 동일하게 유지.
    """
    pool = score_full_pool(pool_size=100)
    if not pool:
        return [], now_kst().strftime('%H:%M')

    scored_stocks = [{
        "_code": e["code"],
        "종목명": e["name"],
        "타점 점수": e["tech_score"],
        "배열 상태": e["trend_str"],
        "MACD 신호": e["macd_str"],
        "RSI 지수": f"{e['rsi']:.1f} ({rsi_signal_label(e['rsi'])})" if e["rsi"] is not None else "-",
        "볼린저밴드(%B)": f"{e['bb_pct_b']:.2f} ({bb_signal_label(e['bb_pct_b'])})" if e["bb_pct_b"] is not None else "-",
        "스토캐스틱(%K)": f"{e['stoch_k']:.1f} ({stoch_signal_label(e['stoch_k'])})" if e["stoch_k"] is not None else "-",
        "주간추세": e["weekly_trend"] if e["weekly_trend"] is not None else "-",
        "현재가": f"{int(e['close']):,}",
    } for e in pool]

    # 점수 높은 순으로 정렬 후 상위 20개 추출
    scored_stocks = sorted(scored_stocks, key=lambda x: x['타점 점수'], reverse=True)[:20]

    # 최종 20개만 대상으로 외국인/기관 연속 순매수·순매도 여부를 조회 (100개 전체 조회 시 응답 지연이 커져 상위권만 확인)
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_deal_trend(code):
        try:
            res = get_naver_session().get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=5)
            return res.json().get('dealTrendInfos') or []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=10) as executor:
        deal_trends = list(executor.map(_fetch_deal_trend, [s['_code'] for s in scored_stocks]))

    for s, deal_trend in zip(scored_stocks, deal_trends):
        s['주요 수급'] = compute_investor_streak_label(deal_trend)
        del s['_code']

    return scored_stocks, now_kst().strftime('%H:%M')

@st.cache_data(ttl=3600)
def backtest_technical_signal(code, threshold=70, holding_days=20, lookback_years=3):
    """한 종목의 과거 가격 이력 전체에 run_logical_screener와 동일한 채점 로직
    (compute_technical_indicators + compute_technical_score_at_row)을 매일 적용해,
    타점 점수가 threshold를 처음 넘어선 날("신호 발생일")마다 이후 holding_days 거래일 뒤 수익률을 계산.
    미래 데이터를 참조하지 않도록(lookahead 방지) rolling/ewm 등 과거 시점까지만 보는 연산만 사용하고,
    각 신호의 보유기간 종료일이 데이터 범위를 벗어나면(가장 최근 신호들) 집계에서 제외한다.

    반환: (요약 dict 또는 None, 개별 신호 DataFrame, 계산 시각)"""
    ts = now_kst().strftime('%H:%M')
    start_date = (now_kst() - timedelta(days=int(lookback_years * 365) + 200)).strftime('%Y-%m-%d')
    try:
        df_fdr = fdr.DataReader(code, start_date)
        if df_fdr.empty or len(df_fdr) < 120:
            return None, pd.DataFrame(), ts
        df = compute_technical_indicators(df_fdr)
    except Exception:
        return None, pd.DataFrame(), ts

    scores = []
    for i in range(len(df)):
        row = df.iloc[i]
        if pd.isna(row['SMA60']) or pd.isna(row['RSI']) or pd.isna(row['MACD']):
            scores.append(None)
            continue
        s, _, _ = compute_technical_score_at_row(row)
        scores.append(s)
    df = df.assign(SCORE=scores)

    signals = []
    prev_above = False
    for i in range(len(df)):
        s = df['SCORE'].iloc[i]
        if s is None:
            prev_above = False
            continue
        is_above = s >= threshold
        # "신호 발생일" = 문턱값을 새로 넘어선 첫날만 카운트 (연속 며칠간 고득점 유지되는 경우 중복 집계 방지)
        if is_above and not prev_above and (i + holding_days) < len(df):
            entry_price = float(df['close'].iloc[i])
            exit_price = float(df['close'].iloc[i + holding_days])
            if entry_price > 0:
                signals.append({
                    "신호일": df.index[i].strftime('%Y-%m-%d'),
                    "타점점수": round(float(s), 1),
                    "진입가": entry_price,
                    "청산가": exit_price,
                    "수익률(%)": round((exit_price - entry_price) / entry_price * 100, 2),
                })
        prev_above = is_above

    if not signals:
        return {"signal_count": 0}, pd.DataFrame(), ts

    df_signals = pd.DataFrame(signals)
    win_rate = float((df_signals["수익률(%)"] > 0).mean() * 100)
    avg_return = float(df_signals["수익률(%)"].mean())

    # 벤치마크: "아무 날짜에나 사서 holding_days만큼 들고 있었으면" 평균 수익률 — 신호가 실제로 무작위보다 나은지 비교용
    closes = df['close'].values
    baseline_returns = [
        (closes[i + holding_days] - closes[i]) / closes[i] * 100
        for i in range(len(closes) - holding_days) if closes[i] > 0
    ]
    baseline_avg = float(sum(baseline_returns) / len(baseline_returns)) if baseline_returns else None

    summary = {
        "signal_count": len(df_signals),
        "win_rate": win_rate,
        "avg_return": avg_return,
        "baseline_avg": baseline_avg,
    }
    return summary, df_signals, ts

@st.cache_data(ttl=60)
def fetch_stock_name_and_fundamentals(code):
    # NOTE(2026-09-16): finance.naver.com/item/main.naver가 Next.js 개편으로 서버 렌더링 HTML을
    # 제공하지 않게 되어(#_per, tb_type1_ifrs 등 기존 선택자가 전부 무효화됨), 모바일 웹이 쓰는
    # m.stock.naver.com JSON API(integration + finance/annual)로 전면 교체.
    # 병렬 스캔(scan_fundamentals) 시 스레드별 세션을 재사용해야 TLS 연결 재사용 이점을 살릴 수 있음.
    session = get_naver_session()

    def clean_num(val):
        if val is None:
            return 0.0
        val = str(val).replace(',', '').replace('%', '').replace('배', '').replace('원', '').strip()
        try:
            return float(val) if val and val != '-' else 0.0
        except ValueError:
            return 0.0

    try:
        res = session.get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=5)
        if res.status_code != 200:
            return None
        integ = res.json()
        name = integ.get('stockName')
        if not name:
            return None

        total_infos = {item.get('code'): item.get('value') for item in (integ.get('totalInfos') or [])}
        per = clean_num(total_infos.get('per'))
        pbr = clean_num(total_infos.get('pbr'))
        cns_per = clean_num(total_infos.get('cnsPer'))

        # 동일업종 PER: 신규 API에서 제공하지 않음 → 0으로 두면 아래 밸류에이션 로직이 기본 점수로 자동 처리
        sector_per = 0.0

        op_margin, roe = "0", "0"
        op_margin_series, revenue_series = [], []
        try:
            fin_res = session.get(f"https://m.stock.naver.com/api/stock/{code}/finance/annual", timeout=5)
            finance_info = fin_res.json().get('financeInfo') if fin_res.status_code == 200 else None
            row_list = (finance_info or {}).get('rowList') or []
            title_list = (finance_info or {}).get('trTitleList') or []

            ordered_keys = [t['key'] for t in sorted(title_list, key=lambda t: t['key'])]
            actual_keys = [t['key'] for t in title_list if t.get('isConsensus') != 'Y']
            latest_actual_key = max(actual_keys) if actual_keys else (ordered_keys[-1] if ordered_keys else None)

            def row_by_title(title):
                return next((row for row in row_list if row.get('title') == title), None)

            def row_series(title, keys):
                row = row_by_title(title)
                if not row:
                    return []
                values = []
                for k in keys:
                    raw = (row.get('columns') or {}).get(k, {}).get('value')
                    if raw and raw != '-':
                        try:
                            values.append(float(str(raw).replace(',', '')))
                        except ValueError:
                            continue
                return values

            if latest_actual_key:
                op_margin_row = row_by_title('영업이익률')
                if op_margin_row:
                    op_margin = (op_margin_row.get('columns') or {}).get(latest_actual_key, {}).get('value') or "0"
                roe_row = row_by_title('ROE')
                if roe_row:
                    roe = (roe_row.get('columns') or {}).get(latest_actual_key, {}).get('value') or "0"

            # 확인 가능한 전체 연도의 영업이익률 / 매출액 시계열 (평균·성장률 계산용)
            op_margin_series = row_series('영업이익률', ordered_keys)
            revenue_series = row_series('매출액', ordered_keys)
        except Exception:
            pass

        # 연간 매출액 시계열로부터 연도별 YoY 성장률 계산 → 평균값을 "매출성장률"로 사용
        revenue_growth_points = []
        for i in range(1, len(revenue_series)):
            prev, cur = revenue_series[i - 1], revenue_series[i]
            if prev != 0:
                revenue_growth_points.append((cur - prev) / abs(prev) * 100)
        revenue_growth_avg = sum(revenue_growth_points) / len(revenue_growth_points) if revenue_growth_points else None

        op_margin_avg = sum(op_margin_series) / len(op_margin_series) if op_margin_series else None

        return {
            "name": name,
            "per": per,
            "pbr": pbr,
            "cns_per": cns_per,
            "sector_per": sector_per,
            "op_margin": clean_num(op_margin),
            "roe": clean_num(roe),
            "op_margin_avg": op_margin_avg,          # 확인 가능한 연도 전체 평균 영업이익률 (없으면 None)
            "op_margin_years": len(op_margin_series),
            "revenue_growth": revenue_growth_avg,     # 확인 가능한 연도들의 평균 매출성장률(%) (없으면 None)
            "revenue_growth_years": len(revenue_growth_points),
        }
    except Exception:
        return None

def analyze_stock_technical(code):
    # 주간추세(20주 이동평균)까지 계산하려면 100영업일보다 더 긴 과거 데이터가 필요해 약 500일 전부터 조회
    start_date = (now_kst() - timedelta(days=500)).strftime('%Y-%m-%d')
    try:
        df_fdr = fdr.DataReader(code, start_date)
        if df_fdr.empty or len(df_fdr) < 60:
            return None

        df = compute_technical_indicators(df_fdr)
        last = df.iloc[-1]

        start_price = df.iloc[-22]['close'] if len(df) >= 22 else df.iloc[0]['close']
        one_month_return = (last['close'] - start_price) / start_price * 100

# --- [수정] 5일 평균 거래량 대비 당일 증가율 계산 ---
        avg_volume_5d = df['volume'].iloc[-6:-1].mean() if len(df) >= 6 else 1.0
        if avg_volume_5d == 0: avg_volume_5d = 1.0
        volume_ratio = (last['volume'] / avg_volume_5d) * 100

        return {
            "price": last['close'],
            "sma20": last['SMA20'],
            "sma60": last['SMA60'],
            "macd": last['MACD'],
            "signal": last['Signal'],
            "rsi": last['RSI'],
            "atr": last['ATR'],
            "bb_upper": last['BB_UPPER'],
            "bb_lower": last['BB_LOWER'],
            "bb_pct_b": last['BB_PCT_B'],
            "stoch_k": last['STOCH_K'],
            "stoch_d": last['STOCH_D'],
            "weekly_trend": last['WEEKLY_TREND'] if pd.notna(last['WEEKLY_TREND']) else None,
            "one_month_return": one_month_return,
            "volume_ratio": volume_ratio # 반환값 추가
        }
    except Exception:
        return None

@st.cache_data(ttl=1800)  # 30분 캐싱 (퀀트/가치재평가주 유니버스 스캔 시 수백 종목 단위로 호출되므로 캐시 재사용 이점이 큼)
def fetch_price_momentum(code):
    """퀀트 스캔용 경량 모멘텀 지표: 최근 1개월(22거래일) 가격 수익률만 계산.
    analyze_stock_technical과 달리 SMA/RSI/MACD/ATR은 계산하지 않아 대량 종목 스캔에 적합."""
    try:
        start_date = (now_kst() - timedelta(days=45)).strftime('%Y-%m-%d')
        hist = fdr.DataReader(code, start_date)
        if len(hist) < 2:
            return None
        closes = hist['Close']
        start_price = closes.iloc[-22] if len(closes) >= 22 else closes.iloc[0]
        if start_price == 0:
            return None
        return float((closes.iloc[-1] - start_price) / start_price * 100)
    except Exception:
        return None

def compute_risk_levels(price, atr):
    """ATR(변동성) 기반 손절가/목표가 참고치 산출.
    손절 = 현재가 - 2×ATR, 목표가 = 현재가 + 3×ATR (위험 대비 보상 약 1:1.5).
    실제 지지/저항선을 반영한 값이 아니라 변동성만으로 근사한 참고치이므로,
    화면에는 반드시 "참고용" 문구와 함께 노출할 것."""
    if not atr or atr <= 0:
        return None
    stop_loss = price - 2 * atr
    target_price = price + 3 * atr
    return {"stop_loss": stop_loss, "target_price": target_price}

@st.cache_data(ttl=300)  # 5분 캐싱 (동일업종 피어 종목들의 PER 조회는 다소 무거워 개별종목 캐시(60초)보다 여유있게)
def fetch_sector_average_per(code):
    """동일업종 PER 근사 산출.
    m.stock.naver.com 신규 API는 '동일업종 PER'을 직접 제공하지 않아,
    integration API의 동일업종 비교 종목(industryCompareInfo) 목록의 PER을 직접 조회해 대표값으로 대체.
    (개별종목분석에서 최대 10종목까지만 호출되므로 종목당 몇 건의 추가 조회는 부담이 크지 않음 —
    퀀트/가치재평가주처럼 수백 종목을 스캔하는 곳에서는 호출하지 않을 것)

    적자에 가까운 종목 1곳만 있어도 PER이 수백~수천 배로 튀어 평균을 왜곡하는 경우가 흔해
    (실측 예: 반도체 피어 6곳 중 1곳이 PER 1279배), 단순 평균 대신 이상치에 강한 중앙값(median)을 사용하고
    상식적으로 비교 의미가 없는 극단치(PER 200배 초과)는 아예 제외한다.
    """
    session = get_naver_session()
    try:
        res = session.get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=5)
        if res.status_code != 200:
            return 0.0
        peers = res.json().get('industryCompareInfo') or []
        peer_codes = [p['itemCode'] for p in peers if p.get('itemCode')]

        pers = []
        for peer_code in peer_codes:
            try:
                peer_res = session.get(f"https://m.stock.naver.com/api/stock/{peer_code}/integration", timeout=5)
                if peer_res.status_code != 200:
                    continue
                total_infos = {item.get('code'): item.get('value') for item in (peer_res.json().get('totalInfos') or [])}
                per_raw = total_infos.get('per')
                if not per_raw:
                    continue
                per_val = float(str(per_raw).replace(',', '').replace('배', '').strip())
                if 0 < per_val <= 200:  # 200배 초과는 적자 근접 등으로 왜곡된 값일 가능성이 커 제외
                    pers.append(per_val)
            except Exception:
                continue

        if not pers:
            return 0.0
        pers.sort()
        mid = len(pers) // 2
        median_per = pers[mid] if len(pers) % 2 == 1 else (pers[mid - 1] + pers[mid]) / 2
        return median_per
    except Exception:
        return 0.0

@st.cache_data(ttl=86400)
def get_stock_code_map():
    """종목명 -> 6자리 코드 매핑 테이블.
    1차로는 이미 안정적으로 작동 중인 fetch_market_universe()(FinanceDataReader 기반)를 사용하고,
    그것이 실패할 때만 kind.krx.co.kr 직접 스크래핑으로 폴백한다.
    (kind.krx.co.kr는 Streamlit Cloud 공유 IP가 차단당할 수 있어 기본 경로로 쓰기 위험함)
    """
    code_map = {}
    # 1차: 이미 검증된 FinanceDataReader 소스 우선 사용
    try:
        universe = fetch_market_universe()
        if not universe.empty:
            for _, row in universe.iterrows():
                code_map[row['Name']] = row['Code']
    except Exception:
        pass

    # 2차 폴백: 위가 실패했을 때만 기존 kind.krx.co.kr 스크래핑 시도
    if not code_map:
        url = 'http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13'
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            res = requests.get(url, headers=headers, timeout=10)
            res.encoding = 'euc-kr'
            soup = BeautifulSoup(res.text, 'html.parser')
            rows = soup.find_all('tr')
            for row in rows:
                tds = row.find_all('td')
                if len(tds) >= 3:
                    company_name = tds[0].text.strip()
                    code_text = tds[2].text.strip()
                    if len(code_text) >= 5:
                        if code_text.isdigit():
                            code_map[company_name] = f"{int(code_text):06d}"
                        else:
                            code_map[company_name] = code_text
        except Exception:
            pass

    return code_map

@st.cache_data(ttl=21600)  # 6시간 캐싱 (시가총액/업종은 하루 중 자주 바뀌지 않음)
def fetch_market_universe():
    """KRX 전종목의 시가총액과 업종분류를 가져와 병합.
    NOTE(2026-09-16): fdr.StockListing('KRX'/'KRX-MARCAP')의 시세 병합이 깨져 Marcap이 전종목 NaN으로
    반환되는 것을 실측으로 확인(Close도 '-' 문자열, Stocks(발행주식수)만 정상) → 이미 이 프로젝트 전반에서
    안정적으로 쓰고 있는 네이버 모바일 API의 시가총액 랭킹(stocks/marketValue) 엔드포인트로 교체.
    업종은 KRX-DESC의 'Sector' 컬럼이 실제로는 코스닥 소속부(벤처기업부 등)만 채워져 있고 코스피는 전부
    NaN이라(실제 업종분류는 'Industry' 컬럼에 있음) 함께 수정."""
    session = get_naver_session()
    rows = []
    try:
        for market in ["KOSPI", "KOSDAQ"]:
            page = 1
            while True:
                res = session.get(
                    "https://m.stock.naver.com/api/stocks/marketValue/" + market,
                    params={"page": page, "pageSize": 100}, timeout=10
                )
                if res.status_code != 200:
                    break
                data = res.json()
                stocks = data.get("stocks") or []
                if not stocks:
                    break
                for s in stocks:
                    marcap_raw = s.get("marketValueRaw")
                    if not marcap_raw:
                        continue
                    rows.append({
                        "Code": s.get("itemCode"),
                        "Name": s.get("stockName"),
                        "Market": market,
                        "시가총액(억원)": float(marcap_raw) / 1e8,
                    })
                if page * 100 >= data.get("totalCount", 0):
                    break
                page += 1
    except Exception:
        pass

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    try:
        df_desc = fdr.StockListing('KRX-DESC')[['Code', 'Industry']].rename(columns={'Industry': 'Sector'})
        df = pd.merge(df, df_desc, on='Code', how='left')
    except Exception:
        df['Sector'] = None
    df['Sector'] = df['Sector'].fillna('업종정보없음')
    return df


def scan_fundamentals(codes, max_workers=15):
    """주어진 종목코드 리스트에 대해 개별 페이지 펀더멘털(PER/PBR/ROE/영업이익률/매출성장률)과
    모멘텀(1개월 수익률)을 같은 스레드풀에서 함께 병렬 수집 (두 API를 순차 2-pass로 나누면 왕복시간이 배가되므로 종목당 1워커에서 함께 처리)"""
    from concurrent.futures import ThreadPoolExecutor

    def fetch_one(code):
        f = fetch_stock_name_and_fundamentals(code)
        if not f:
            return None
        f = dict(f)
        f['momentum'] = fetch_price_momentum(code)
        return f

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, code): code for code in codes}
        for future in futures:
            code = futures[future]
            try:
                data = future.result()
                if data:
                    results[code] = data
            except Exception:
                continue
    return results


def _percentile_score(series, higher_is_better=True):
    """시리즈를 스캔 유니버스 내 상대 순위 기준 0~100 백분위 점수로 변환 (결측치는 중립값 50).
    개별종목 화면(calculate_stock_score)의 고정 임계값 방식과 달리, 여기서는 시가총액/업종 조건으로 좁힌
    스캔 대상 "안에서의 상대 순위"를 쓴다 — ROE 10% 이상처럼 고정 커트라인은 시장 상황에 따라
    항상 적절한 기준이 되지 않기 때문."""
    pct = pd.Series(50.0, index=series.index)
    valid = series.notna()
    if valid.sum() > 1:
        pct.loc[valid] = series[valid].rank(pct=True, ascending=higher_is_better) * 100
    return pct

def add_quant_composite_score(df):
    """PER/PBR(가치) + ROE/영업이익률(품질) + 매출성장률(성장) + 모멘텀 4개 팩터를 각각 25점 만점으로
    가중합산한 종합점수(0~100) 컬럼을 추가."""
    df = df.copy()
    df['가치점수'] = (_percentile_score(df['PER'].where(df['PER'] > 0), higher_is_better=False)
                    + _percentile_score(df['PBR'].where(df['PBR'] > 0), higher_is_better=False)) / 2
    df['품질점수'] = (_percentile_score(df['ROE']) + _percentile_score(df['영업이익률'])) / 2
    df['성장점수'] = _percentile_score(df['매출성장률'])
    df['모멘텀점수'] = _percentile_score(df['모멘텀'])
    df['종합점수'] = (df['가치점수'] + df['품질점수'] + df['성장점수'] + df['모멘텀점수']) / 4
    return df

def _format_market_cap(x):
    """시가총액(억원 단위 숫자)을 억원/조원 단위 문자열로 변환 (소수점 1자리로 통일)"""
    if pd.isna(x):
        return "-"
    x = float(x)
    return f"{x:,.1f}억 원" if x < 10000 else f"{x/10000:.1f}조 원"

@st.cache_data(ttl=1800)
def build_quant_filter_candidates(market_cap_min=0, included_sector="", max_scan=150):
    """실제 시가총액/업종 데이터로 먼저 후보를 좁힌 뒤, 그 안에서만 개별 페이지를 스캔하여 펀더멘털·모멘텀을 수집하고
    멀티팩터 종합점수까지 계산합니다. 가치재평가주·퀀트 투자 리스트 두 메뉴가 공유하는 단일 스캔 파이프라인.

    반환: (결과 DataFrame, 계산 시각, 상태코드). 상태코드는 빈 결과의 원인을 구분해 화면에 정확한 안내를
    띄우기 위함 — "ok"=정상, "universe_failed"=시가총액 데이터 수집 자체가 실패(네트워크 문제, 재시도 유도),
    "no_candidates"=시가총액/업종 조건을 통과하는 종목이 아예 없음(조건 완화 유도),
    "fundamentals_failed"=후보는 있었지만 펀더멘털 조회가 전부 실패(일시적 API 문제, 재시도 유도)."""
    ts = now_kst().strftime('%H:%M')
    universe = fetch_market_universe()
    if universe.empty:
        return pd.DataFrame(), ts, "universe_failed"

    candidates = universe.copy()
    if market_cap_min and market_cap_min > 0:
        candidates = candidates[candidates['시가총액(억원)'] >= market_cap_min]
    if included_sector:
        candidates = candidates[candidates['Sector'].astype(str).str.contains(included_sector, case=False, na=False)]

    if candidates.empty:
        return pd.DataFrame(), ts, "no_candidates"

    # 스캔 부하를 제한하기 위해 (필터 통과 종목 중) 시가총액 상위 max_scan개까지만 실제 조회
    candidates = candidates.sort_values('시가총액(억원)', ascending=False).head(max_scan)

    fundamentals_map = scan_fundamentals(candidates['Code'].tolist())

    rows = []
    for _, row in candidates.iterrows():
        f = fundamentals_map.get(row['Code'])
        if not f:
            continue
        rows.append({
            "종목코드": row['Code'],
            "종목명": f["name"],
            "PER": f["per"],
            "PBR": f["pbr"],
            "ROE": f["roe"],
            "영업이익률": f["op_margin"],
            "영업이익률평균": f["op_margin_avg"],          # 확인 가능한 연도 전체 평균 (가치재평가주 "고수익성" 탭용)
            "영업이익률_확인연도수": f["op_margin_years"],
            "매출성장률": f["revenue_growth"],  # 확인 가능한 연도들의 평균 YoY 성장률(%), 데이터 없으면 None
            "매출성장률_확인연도수": f["revenue_growth_years"],
            "모멘텀": f["momentum"],            # 최근 1개월 가격 수익률(%), 데이터 없으면 None
            "시가총액": row['시가총액(억원)'],
            "업종": row['Sector'],
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df, ts, "fundamentals_failed"
    return add_quant_composite_score(df), ts, "ok"

QUANT_SCAN_STATUS_MESSAGES = {
    "universe_failed": ("error", "시가총액 데이터를 불러오지 못했습니다. 네트워크 문제일 수 있으니 잠시 후 다시 시도해주세요."),
    "no_candidates": ("warning", "설정한 시가총액/업종 조건에 맞는 종목이 없습니다. 조건을 완화해보세요."),
    "fundamentals_failed": ("warning", "종목 데이터 조회에 실패했습니다. 일시적인 문제일 수 있으니 잠시 후 다시 시도해주세요."),
}

def render_quant_scan_status(status):
    """build_quant_filter_candidates의 빈 결과 상태코드에 맞는 안내 메시지 표시 (원인별로 사용자의 다음 행동이 다르므로 구분)"""
    kind, msg = QUANT_SCAN_STATUS_MESSAGES.get(status, ("warning", "조건에 맞는 종목을 찾지 못했거나 데이터를 불러오지 못했습니다."))
    getattr(st, kind)(msg)

@st.cache_data(ttl=1800)
def compute_price_correlation(codes, names):
    """스크리닝 상위 종목들의 최근 60거래일 일간수익률 상관계수 행렬을 계산 (포트폴리오 분산 참고용).
    ETF 간 실제 보유종목 중복도 분석과 달리, 개별 종목은 구성종목 데이터 없이 가격만으로 직접 계산 가능."""
    from concurrent.futures import ThreadPoolExecutor
    start_date = (now_kst() - timedelta(days=100)).strftime('%Y-%m-%d')

    def fetch_close(code):
        try:
            hist = fdr.DataReader(code, start_date)
            return hist['Close'].tail(60).pct_change().reset_index(drop=True)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        series_list = list(executor.map(fetch_close, codes))

    data = {name: s for name, s in zip(names, series_list) if s is not None and len(s) > 10}
    if len(data) < 2:
        return None
    return pd.DataFrame(data).corr()

def _correlation_cell_color(val):
    """상관계수 값을 빨강(고상관, 분산효과 낮음)~흰색(무상관)~초록(음의 상관, 분산효과 높음)으로 색칠.
    pandas Styler.background_gradient는 matplotlib 의존성이 필요해, 이 프로젝트엔 없는 무거운 패키지를 새로 추가하지 않도록 직접 계산."""
    if pd.isna(val):
        return ''
    intensity = min(abs(val), 1.0)
    if val >= 0:
        r, g, b = 255, int(255 - 180 * intensity), int(255 - 180 * intensity)
    else:
        r, g, b = int(255 - 180 * intensity), int(255 - 100 * intensity), int(255 - 180 * intensity)
    return f'background-color: rgb({r},{g},{b})'

def find_high_correlation_pairs(corr_df, threshold=0.7):
    """상관계수 행렬에서 threshold 이상인 종목 쌍을 상관계수 내림차순으로 추출"""
    pairs = []
    cols = corr_df.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr_df.iloc[i, j]
            if pd.notna(v) and v >= threshold:
                pairs.append((cols[i], cols[j], v))
    return sorted(pairs, key=lambda x: -x[2])

def render_portfolio_correlation(df_top, key_prefix):
    """스크리닝 결과 상위 종목들의 가격 상관관계 분석 섹션을 렌더링 (가치재평가주/퀀트 투자 리스트 공통 사용)"""
    st.markdown("#### 🔗 상위 종목 간 가격 상관관계 (최근 60거래일)")
    st.caption("서로 다른 종목이라도 가격이 함께 움직이면(상관계수 높음) 담아도 분산 효과가 떨어집니다. 실제 보유종목 데이터가 아닌 가격 흐름 기반 근사치입니다.")
    top_for_corr = df_top.head(15)
    if len(top_for_corr) < 2:
        st.info("상관관계를 계산하려면 최소 2종목이 필요합니다.")
        return
    with st.spinner("상위 종목 가격 상관관계 계산 중..."):
        corr_df = compute_price_correlation(tuple(top_for_corr['종목코드']), tuple(top_for_corr['종목명']))
    if corr_df is None:
        st.info("상관관계를 계산하기에 가격 데이터가 부족합니다.")
        return
    st.dataframe(
        corr_df.style.map(_correlation_cell_color).format("{:.2f}"),
        use_container_width=True, key=f"{key_prefix}_corr_df"
    )
    high_pairs = find_high_correlation_pairs(corr_df, threshold=0.7)
    if high_pairs:
        pair_text = ", ".join([f"{a}·{b}({v:.2f})" for a, b, v in high_pairs[:5]])
        st.warning(f"⚠️ 상관관계 높은(0.7 이상) 종목 쌍: {pair_text} — 함께 담으면 분산 효과가 제한적일 수 있습니다.")
    else:
        st.success("✅ 상위 종목 간 뚜렷한 고상관 쌍은 발견되지 않았습니다.")

def get_ai_summary(news_list):
    news_text = "\n".join([f"- [{n['press']}] {n['title']}" for n in news_list])
    prompt = f"""
    당신은 전문 주식 투자 분석가입니다. 다음 실시간 뉴스 리스트를 꼼꼼히 분석하여
    투자자 입장에서 핵심 요약을 명확하게 '3줄'로 작성해주세요.
    그리고 종합 결론으로 이 뉴스가 해당 기업의 주가 흐름에 [긍정적 / 중립 / 부정적] 일지 판단하고 이유를 덧붙여주세요.

    [뉴스 리스트]
    {news_text}
    """
    ok, content = call_groq_chat([{"role": "user", "content": prompt}])
    if ok:
        return content
    return f"⚠️ AI 분석 중 오류가 발생했습니다. (Secrets 설정이나 키 값을 확인해 주세요): {content}"

@st.cache_data(ttl=900)  # 15분 캐싱 (버튼 재클릭마다 불필요한 API 재호출 방지)
def get_individual_stock_ai_analysis(fundamentals, tech, news_list):
    news_text = "\n".join([f"- [{n['press']}] {n['title']}" for n in news_list]) if news_list else "최근 관련 뉴스 없음"
    prompt = f"""
    당신은 전문 주식 투자 분석가(리서치 센터장)입니다.
    다음 개별 종목 정보를 바탕으로 투자자에게 깊이 있고 전문적인 'AI 심층 투자 분석 보고서'를 작성해주세요.

    [종목 기본 정보]
    - 종목명: {fundamentals['name']}
    - 영업이익률: {fundamentals['op_margin']}%
    - ROE: {fundamentals['roe']}%
    - 추정 PER: {fundamentals['cns_per']}배 (후행 PER: {fundamentals['per']}배)
    - PBR: {fundamentals['pbr']}배

    [기술적 지표 및 트렌드]
    - 현재가: {int(tech['price']):,}원
    - 최근 1개월 수익률: {tech['one_month_return']:.2f}%
    - RSI (14): {tech['rsi']:.2f} (30 이하는 과매도, 70 이상은 과열)
    - MACD 상태: {'매수 우위 (MACD > Signal)' if tech['macd'] > tech['signal'] else '매도 우위 (MACD <= Signal)'}
    - 20일 이동평균선 위치: {'20일선 위 (상승추세)' if tech['price'] > tech['sma20'] else '20일선 아래 (조정/하락세)'}
    - 볼린저밴드 %B: {f"{tech['bb_pct_b']:.2f} (0=하단밴드, 1=상단밴드)" if tech.get('bb_pct_b') is not None and pd.notna(tech['bb_pct_b']) else "데이터 없음"}
    - 스토캐스틱 %K/%D: {f"{tech['stoch_k']:.1f} / {tech['stoch_d']:.1f} (20 이하 과매도, 80 이상 과열)" if tech.get('stoch_k') is not None and pd.notna(tech['stoch_k']) else "데이터 없음"}
    - 주간 추세(주봉 5주/20주 이평): {tech.get('weekly_trend') or "데이터 없음"}

    [관련 최신 뉴스 및 시장 평가]
    {news_text}

    보고서는 가독성 있게 작성되어야 하며, 다음 내용을 포함해 주세요:
    1. **재무 건전성 및 밸류에이션 평가**: 영업이익률/ROE를 통한 수익성 평가, PER/PBR 기준 고평가/저평가 여부 분석.
    2. **차트 및 기술적 흐름 해석**: 이동평균선 추세, RSI 과매도/과열 강도, MACD 신호를 조합한 매수 타점 분석.
    3. **시장 모멘텀 및 뉴스 분석**: 최근 뉴스들의 호재/악재 성격 및 수급 영향 평가.
    4. **최종 AI 투자 전략 & 가이드**: 추천 매매 전략(분할 매수, 관망, 매도 등)과 목표/손절 대응 팁.
    """
    ok, content = call_groq_chat([{"role": "user", "content": prompt}])
    if not ok:
        # 예외는 st.cache_data에 캐시되지 않으므로, 실패한 리포트가 15분간 그대로 노출되는 것을 막는다.
        raise RuntimeError(content)
    return content

def render_data_ts_caption(ts, cache_minutes=None):
    """캐시된 데이터의 실제 계산 시각 캡션. 캐시 히트 시에도 처음 계산된 시각이 그대로 표시되어,
    화면이 "방금 조회한 데이터"처럼 보이지만 실제로는 최대 cache_minutes분 전 데이터일 수 있음을 안내."""
    cache_note = f" (최대 {cache_minutes}분 캐시)" if cache_minutes else ""
    st.caption(f"⏱ {ts} 기준 데이터{cache_note}")

@st.cache_data(ttl=600)  # 10분 캐싱 (등락 확인 목적이라 지수 캐싱(5분)보단 조금 여유있게)
def fetch_semiconductor_snapshot():
    """반도체 대표 종목(업종 ETF + 삼성전자 + SK하이닉스)의 당일 등락 정보를 수집"""
    targets = {
        "반도체 업종(TIGER 반도체TOP10)": "091230",
        "삼성전자": "005930",
        "SK하이닉스": "000660",
    }
    results = {}
    start_date = (now_kst() - timedelta(days=10)).strftime('%Y-%m-%d')
    for name, code in targets.items():
        try:
            df = fdr.DataReader(code, start_date)
            if len(df) >= 2:
                prev_close = float(df['Close'].iloc[-2])
                last_close = float(df['Close'].iloc[-1])
                change_pct = (last_close - prev_close) / prev_close * 100
                results[name] = {"price": last_close, "change": change_pct}
        except Exception:
            continue
    return results, now_kst().strftime('%H:%M')

def fetch_multi_angle_news(queries, per_query=4, max_total=8, period="7d"):
    """여러 키워드로 나눠 뉴스를 조회한 뒤 제목 기준 중복을 제거해 하나의 리스트로 합침
    (한 키워드만 쓰면 특정 각도의 뉴스만 잡혀서, 수급/업황/실적/이슈 등 다각도로 조회)"""
    seen_titles = set()
    combined = []
    for q in queries:
        for n in fetch_headlines_rss(q, max_n=per_query, period=period):
            if n['title'] not in seen_titles:
                seen_titles.add(n['title'])
                combined.append(n)
    return combined[:max_total]

def summarize_news_sentiment(news_list):
    """뉴스 헤드라인들의 긍정/중립/부정 개수를 AI로 집계 (classify_news_sentiment_ai 재사용).
    AI 판정이 실패하면 굳이 부정확한 키워드 집계를 만들지 않고 None을 반환한다."""
    if not news_list:
        return None
    sentiment_map = classify_news_sentiment_ai(news_list)
    if not sentiment_map:
        return None
    counts = {"긍정": 0, "중립": 0, "부정": 0}
    for verdict in sentiment_map.values():
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts

def _format_sentiment_text(counts):
    if not counts:
        return "판정 불가"
    return f"긍정 {counts['긍정']}건 / 중립 {counts['중립']}건 / 부정 {counts['부정']}건"

@st.cache_data(ttl=3600)  # 1시간 캐싱
def get_price_move_reason_analysis(kospi_data, semi_data):
    """코스피 지수와 반도체 대표 종목들이 오늘 왜 이렇게 움직였는지, 다각도로 조회한 실제 뉴스를 근거로 AI가 논리적으로 추정 분석
    반환값: (분석 텍스트, 코스피 관련 뉴스 리스트, 반도체 관련 뉴스 리스트, meta)
    meta = {"ts": "이 분석이 실제로 계산된 시각(HH:MM)", "sentiment": 뉴스 감성 집계 dict 또는 None}"""
    ts = now_kst().strftime('%H:%M')
    # 단일 키워드로는 근거가 얕을 수 있어, 수급/업황/실적/이슈 등 여러 각도로 나눠 검색 후 통합
    # "오늘 왜 이렇게 움직였는지"가 목적이므로 최근 1일(period="1d") 뉴스로 한정해 며칠 전 뉴스가 섞이는 것을 방지
    kospi_news = fetch_multi_angle_news(
        ["코스피 마감", "코스피 외국인 순매수", "코스피 증시 이슈"], per_query=4, max_total=8, period="1d"
    )
    semi_news = fetch_multi_angle_news(
        ["반도체 주가", "반도체 수출 업황", "삼성전자 SK하이닉스 주가", "반도체 D램 가격"], per_query=4, max_total=8, period="1d"
    )
    sentiment_counts = summarize_news_sentiment(kospi_news + semi_news)
    meta = {"ts": ts, "sentiment": sentiment_counts}

    semi_text = "\n".join(
        [f"- {name}: {d['price']:,.0f}원 ({d['change']:+.2f}%)" for name, d in semi_data.items()]
    ) if semi_data else "반도체 데이터 없음"

    kospi_news_text = "\n".join([f"- [{n['press']}] {n['title']}" for n in kospi_news]) if kospi_news else "조회된 관련 뉴스 없음"
    semi_news_text = "\n".join([f"- [{n['press']}] {n['title']}" for n in semi_news]) if semi_news else "조회된 관련 뉴스 없음"

    prompt = f"""
    당신은 냉철하고 논리적인 시장 분석가입니다. 아래 [코스피 지수], [반도체 대표 종목 당일 등락],
    [수급/업황/실적 등 여러 각도로 조회한 실제 뉴스 헤드라인], [뉴스 감성 집계]를 종합하여
    오늘 코스피와 반도체 관련 종목이 왜 이렇게 움직였는지 합리적으로 추정 분석해주세요.

    [코스피 지수]
    - KOSPI: {kospi_data.get('index', 'N/A')} ({kospi_data.get('change', 'N/A')})

    [반도체 대표 종목 당일 등락]
    {semi_text}

    [코스피 관련 뉴스 (수급/증시 이슈 등 여러 각도)]
    {kospi_news_text}

    [반도체 관련 뉴스 (업황/수출/가격/개별종목 등 여러 각도)]
    {semi_news_text}

    [위 뉴스 전체의 감성 집계]
    {_format_sentiment_text(sentiment_counts)}

    반드시 지켜야 할 사항:
    1. 위에 제공된 뉴스에 실제로 언급된 내용만 근거로 사용하세요. 뉴스에 없는 사실을 지어내지 마세요.
    2. 여러 뉴스가 있다면 개별 헤드라인을 단순 나열하지 말고, 서로 연결지어(예: 수급 동향 + 업황 뉴스 + 가격 동향) 하나의 논리적인 흐름으로 종합 설명하세요.
    3. 뉴스 근거가 뚜렷하지 않다면 추측하지 말고 "이 부분은 명확한 뉴스 근거가 없어 추정입니다"처럼 솔직하게 밝히세요.
    4. 코스피 등락 원인과 반도체 등락 원인을 구분해서 각각 3~4문장으로 설명해주세요.
    5. 응답은 반드시 한국어로만, 마크다운으로 가독성 있게 작성하세요.
    """
    ok, content = call_groq_chat([{"role": "user", "content": prompt}], temperature=0.3)
    meta["ai_ok"] = ok  # 호출부가 실패 결과를 캐시에서 비우는 데 사용
    if ok:
        return content, kospi_news, semi_news, meta
    return f"⚠️ AI 등락 원인 분석 중 오류가 발생했습니다: {content}", kospi_news, semi_news, meta

@st.cache_data(ttl=3600)  # 1시간 캐싱
def get_market_ai_briefing(kospi_data, kosdaq_data, top_sectors):
    """반환값: (브리핑 텍스트, 근거로 사용한 시장 뉴스 리스트, meta)
    meta = {"ts": "이 분석이 실제로 계산된 시각(HH:MM)", "sentiment": 뉴스 감성 집계 dict 또는 None,
            "foreign_top": 외국인 순매수 상위 종목명 리스트, "institution_top": 기관 순매수 상위 종목명 리스트}"""
    ts = now_kst().strftime('%H:%M')
    # 지수/업종 숫자만으로 추론하지 않도록, 오늘자 실제 시장 뉴스 헤드라인을 함께 조회해서 근거로 제공
    market_news = fetch_headlines_rss("코스피 코스닥 증시", period="1d")
    sentiment_counts = summarize_news_sentiment(market_news)
    # 시황 브리핑에 실제 수급 방향(외국인/기관 순매수)을 정량 근거로 반영
    foreign_top, _ = fetch_net_buying_top("foreign", "KOSPI", 5)
    institution_top, _ = fetch_net_buying_top("institution", "KOSPI", 5)
    meta = {"ts": ts, "sentiment": sentiment_counts, "foreign_top": foreign_top, "institution_top": institution_top}

    sectors_text = "\n".join([f"- {s['업종/테마']}: {s['변동']}" for s in top_sectors]) if top_sectors else "업종 정보 없음"
    news_text = "\n".join([f"- [{n['press']}] {n['title']}" for n in market_news]) if market_news else "조회된 관련 뉴스 없음"
    foreign_text = ", ".join(foreign_top) if foreign_top else "데이터 없음"
    institution_text = ", ".join(institution_top) if institution_top else "데이터 없음"

    prompt = f"""
    당신은 금융 전략가이자 투자 비서입니다. 아래 [시장 지수], [업종 데이터], [수급 동향], [오늘의 실제 시장 뉴스], [뉴스 감성 집계]를 종합하여 투자 전략을 브리핑해주세요.

    [시장 지수]
    - KOSPI: {kospi_data.get('index', 'N/A')} ({kospi_data.get('change', 'N/A')})
    - KOSDAQ: {kosdaq_data.get('index', 'N/A')} ({kosdaq_data.get('change', 'N/A')})

    [최근 1달 자금 유입 TOP 5 업종/테마]
    {sectors_text}

    [오늘 코스피 외국인 순매수 상위 종목]
    {foreign_text}

    [오늘 코스피 기관 순매수 상위 종목]
    {institution_text}

    [오늘의 실제 시장 뉴스 헤드라인]
    {news_text}

    [위 뉴스 전체의 감성 집계]
    {_format_sentiment_text(sentiment_counts)}

    위 데이터를 바탕으로 시장의 흐름을 냉철하게 분석하고, 투자자가 참고할 수 있는 짤막하고 명쾌한 'AI 투자 전략 보고서'를 3~4문장 단위로 단락을 나누어 작성해주세요.

    반드시 지켜야 할 사항:
    1. 위에 제공된 뉴스/수급 데이터에 실제로 나타난 내용만 근거로 사용하세요. 없는 사실을 지어내지 마세요.
    2. 뉴스가 지수 등락과 직접적인 관련이 없거나 부족하다면, 추측하지 말고 "관련 뉴스 근거는 뚜렷하지 않으며, 지수 데이터상으로는 ~한 흐름입니다"처럼 데이터와 뉴스를 구분해서 솔직하게 설명하세요.
    3. 응답은 반드시 한국어로만 작성하세요. 다른 언어 단어를 섞지 마세요.
    4. 글자 크기가 너무 크지 않도록 마크다운 구조(강조 등)를 활용하여 정중하고 명확한 어조로 요약해 주세요.
    """
    ok, content = call_groq_chat([{"role": "user", "content": prompt}], temperature=0.3)
    meta["ai_ok"] = ok  # 호출부가 실패 결과를 캐시에서 비우는 데 사용
    if ok:
        return content, market_news, meta
    return f"⚠️ AI 시장 분석 중 오류가 발생했습니다: {content}", market_news, meta


def classify_news_sentiment_ai(news_list):
    """뉴스 제목들의 감성(긍정/중립/부정)을 Groq에게 문맥까지 고려해 판정하도록 맡김.
    (기존 방식인 '단순 키워드 매칭'은 문맥을 무시해 "적자 축소"처럼 부정 키워드가 있어도 실제로는
    긍정적인 기사를 오분류하는 문제가 있었음)
    반환: {인덱스: "긍정"/"중립"/"부정"} 딕셔너리. 호출 실패/개수 불일치 시 None을 반환하여
    호출부가 기존 키워드 매칭 방식으로 자동 대체(fallback)하도록 함.
    """
    if not news_list or not GENAI_AVAILABLE:
        return None
    try:
        titles_text = "\n".join([f"{i+1}. {n['title']}" for i, n in enumerate(news_list)])
        prompt = f"""
        당신은 주식 뉴스 감성 분석 전문가입니다. 아래 번호가 매겨진 뉴스 제목들을 각각 읽고,
        해당 기업의 주가에 미칠 영향을 기준으로 "긍정", "중립", "부정" 중 하나로만 판정하세요.
        단어만 보지 말고 반드시 문맥을 고려하세요. 예를 들어 "적자 폭 축소"는 '적자'라는 단어가 있어도
        실제로는 긍정적 신호이고, "역대급 실적에도 불구하고 주가 하락"은 반대로 해석해야 합니다.

        [뉴스 제목 목록]
        {titles_text}

        다른 설명 없이, 아래 JSON 배열 형식으로만 정확히 {len(news_list)}개 항목을 응답하세요:
        [{{"번호": 1, "판정": "긍정"}}, {{"번호": 2, "판정": "중립"}}]
        """
        ok, content = call_groq_chat([{"role": "user", "content": prompt}], temperature=0)
        if not ok:
            return None
        raw = content.strip()
        raw = re.sub(r'^```(json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)

        sentiment_map = {}
        for item in parsed:
            idx = int(item.get("번호")) - 1
            verdict = item.get("판정", "중립")
            if verdict not in ("긍정", "중립", "부정"):
                verdict = "중립"
            sentiment_map[idx] = verdict

        # 응답 개수가 뉴스 개수와 안 맞으면 신뢰할 수 없으므로 폴백 처리
        if len(sentiment_map) != len(news_list):
            return None
        return sentiment_map
    except Exception:
        return None

@st.cache_data(ttl=1800)
def fetch_etf_market_data():
    try:
        return fdr.StockListing('ETF/KR'), now_kst().strftime('%H:%M')
    except Exception:
        return pd.DataFrame(), now_kst().strftime('%H:%M')

def _annualized_volatility(closes):
    """최근 최대 20거래일 일간수익률의 표준편차를 연율화(연 252거래일 가정)한 변동성(%)."""
    if len(closes) < 6:
        return None
    returns = closes.pct_change().dropna().tail(20)
    if len(returns) < 5:
        return None
    return float(returns.std() * (252 ** 0.5) * 100)

@st.cache_data(ttl=21600)  # 총보수/분배율은 하루 안에 거의 바뀌지 않아 6시간 캐싱
def fetch_etf_key_indicators(code):
    """ETF 총보수(연, %)와 분배율(TTM, %)을 네이버 모바일 API(동일업종 PER 조회와 같은 integration 엔드포인트)에서 조회."""
    session = get_naver_session()
    try:
        res = session.get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=5)
        if res.status_code != 200:
            return {"total_fee": None, "dividend_yield": None}
        indicator = res.json().get('etfKeyIndicator') or {}
        return {
            "total_fee": indicator.get('totalFee'),
            "dividend_yield": indicator.get('dividendYieldTtm'),
        }
    except Exception:
        return {"total_fee": None, "dividend_yield": None}

def _fetch_etf_row_metrics(symbol, name, price_display, start_date, need_weekly_return=True):
    """개별 ETF 1건의 1주일 수익률·변동성(가격 히스토리)과 총보수·분배율(키 지표 API)을 함께 조회."""
    result = {"종목명": name, "종목코드": symbol, "현재가": price_display, "_weekly_return": None, "_volatility": None}
    try:
        hist = fdr.DataReader(symbol, start_date)
        if len(hist) > 2:
            closes = hist['Close']
            if need_weekly_return:
                week_ago = closes.iloc[-6] if len(closes) >= 6 else closes.iloc[0]
                result["_weekly_return"] = (closes.iloc[-1] - week_ago) / week_ago * 100
            result["_volatility"] = _annualized_volatility(closes)
    except Exception:
        pass
    indicators = fetch_etf_key_indicators(symbol)
    result["_total_fee"] = indicators.get("total_fee")
    result["_dividend_yield"] = indicators.get("dividend_yield")
    return result

def _format_etf_metric_columns(df):
    """_volatility/_total_fee/_dividend_yield 내부 컬럼을 화면 표시용 문자열 컬럼으로 변환."""
    df['변동성(20일 연율화)'] = df['_volatility'].apply(lambda x: f"{x:.1f}%" if x is not None else "-")
    df['총보수'] = df['_total_fee'].apply(lambda x: f"{x:.2f}%" if x is not None else "-")
    df['분배율'] = df['_dividend_yield'].apply(lambda x: f"{x:.2f}%" if x is not None else "-")
    return df

@st.cache_data(ttl=1800)
def fetch_etf_weekly_returns(df_etf):
    from concurrent.futures import ThreadPoolExecutor

    # 레버리지/인버스 상품 제외 (이름에 해당 키워드 포함된 종목 필터링)
    exclude_keywords = ['레버리지', '인버스', '2X']
    exclude_pattern = '|'.join(exclude_keywords)
    df_filtered = df_etf[~df_etf['Name'].str.contains(exclude_pattern, case=False, na=False)]

    # 거래대금 상위 50개만 필터링하여 수익률 연산 (부하 분산)
    df_top = df_filtered.sort_values(by='Amount', ascending=False).head(50)
    # 45일치를 가져와 1주일 수익률 기준점(약 5거래일 전)과 20거래일 변동성 계산에 함께 사용
    start_date = (now_kst() - timedelta(days=45)).strftime('%Y-%m-%d')

    def fetch_row(row):
        m = _fetch_etf_row_metrics(row['Symbol'], row['Name'], f"{int(row['Price']):,}원", start_date)
        return m if m["_weekly_return"] is not None else None

    rows = [row for _, row in df_top.iterrows()]
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_row, rows))

    results = [r for r in results if r is not None]
    df_res = pd.DataFrame(results)
    if not df_res.empty:
        df_res = df_res.sort_values(by='_weekly_return', ascending=False).head(10)
        df_res['1주일 수익률'] = df_res['_weekly_return'].apply(lambda x: f"+{x:.2f}%" if x > 0 else f"{x:.2f}%")
        df_res = _format_etf_metric_columns(df_res)
    return df_res

# ETF 카테고리 분류 우선순위: 레버리지/인버스 > 채권/현금성 > 배당 > 원자재 > 국내 섹터 > 테마 > 해외지수 > 국내 지수 > 기타
ETF_CATEGORY_ORDER = ["국내 섹터", "테마", "배당", "원자재", "해외지수", "레버리지/인버스"]

def classify_etf(name: str) -> str:
    """ETF 종목명을 이름 키워드 기반으로 투자 카테고리로 분류"""
    # 1순위: 레버리지/인버스 (고위험, 별도 탭에서 취급)
    if any(kw in name for kw in ['레버리지', '인버스', '2X']):
        return '레버리지/인버스'

    # 2순위: 채권/현금성 (투자 테마 성격이 아니므로 카테고리 탭에서는 제외)
    # '국채'만으로는 "국고채"/"국공채"가 매칭되지 않아(부분 문자열 불일치) 실측 데이터 기준 국채류 ETF 다수가 누락되던 것을 수정
    if any(kw in name for kw in ['채권', '국채', '국고채', '국공채', '회사채', 'CD금리', 'CD1년',
                                   'KOFR', 'SOFR', '머니마켓', 'TDF', 'TRF',
                                   '통안채', '금리액티브', '금리플러스']):
        return '채권/현금성'

    # 3순위: 배당 (커버드콜·인컴형 상품은 대부분 배당형 인컴 상품이라 함께 분류)
    if any(kw in name for kw in ['배당', '커버드콜', '인컴']):
        return '배당'

    # 4순위: 원자재 (금/은/원유/구리 등 실물·선물 추종 상품 — 실측 데이터에서 "기타"로 새던 것을 별도 분리)
    commodity_kw = ['골드', '금선물', '금액티브', '은선물', '은액티브', 'WTI', '원유',
                    '구리실물', '천연가스', '팔라듐', '농산물', '원자재']
    if any(kw in name for kw in commodity_kw):
        return '원자재'

    # 5순위: 국내 섹터
    sector_kw = ['반도체', '2차전지', '바이오', '자동차', '조선', '은행',
                 '증권', '보험', '철강', '건설', 'IT', '헬스케어', '게임',
                 '화장품', '에너지화학', '운송', '기계', '리츠', '금융',
                 '소재', '소부장', '그룹', '지주회사', '우선주']
    if any(kw in name for kw in sector_kw):
        return '국내 섹터'

    # 6순위: 테마
    theme_kw = ['AI', '로봇', '로보틱스', '휴머노이드', '우주', '방산', '원자력', 'SMR',
                '신재생', '수소', '양자컴퓨팅', '전력', '메타버스', '자율주행',
                '드론', '데이터센터', 'K-', '밸류업', 'ESG']
    if any(kw in name for kw in theme_kw):
        return '테마'

    # 7순위: 해외지수
    overseas_kw = ['미국', 'S&P', '나스닥', '차이나', '일본', '인도', '유럽',
                   '베트남', '다우존스', '니케이', '항셍', 'CSI300', '중국', '버크셔']
    if any(kw in name for kw in overseas_kw):
        return '해외지수'

    # 8순위: 국내 지수 (기본 지수 추종 상품)
    if any(kw in name for kw in ['200', '코스피', '코스닥', 'KRX']):
        return '국내 지수'

    return '기타'

@st.cache_data(ttl=1800)
def fetch_etf_category_returns(df_etf, category, top_n=10, pool_size=40):
    """특정 카테고리 내에서 거래대금 상위 종목들의 1주일 수익률을 계산해 상위 N개 반환"""
    from concurrent.futures import ThreadPoolExecutor

    df_cat = df_etf[df_etf['Name'].apply(classify_etf) == category]
    if df_cat.empty:
        return pd.DataFrame()

    # 해당 카테고리 내 거래대금 상위 종목만 수익률 연산 (API 호출 부하 분산)
    df_top = df_cat.sort_values(by='Amount', ascending=False).head(pool_size)
    # 45일치를 가져와 1주일 수익률 기준점(약 5거래일 전)과 20거래일 변동성 계산에 함께 사용
    start_date = (now_kst() - timedelta(days=45)).strftime('%Y-%m-%d')

    def fetch_row(row):
        m = _fetch_etf_row_metrics(row['Symbol'], row['Name'], f"{int(row['Price']):,}원", start_date)
        return m if m["_weekly_return"] is not None else None

    rows = [row for _, row in df_top.iterrows()]
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_row, rows))

    results = [r for r in results if r is not None]
    df_res = pd.DataFrame(results)
    if not df_res.empty:
        df_res = df_res.sort_values(by='_weekly_return', ascending=False).head(top_n)
        df_res['1주일 수익률'] = df_res['_weekly_return'].apply(lambda x: f"+{x:.2f}%" if x > 0 else f"{x:.2f}%")
        df_res = _format_etf_metric_columns(df_res)
    return df_res

@st.cache_data(ttl=1800)
def fetch_etf_inflow_top10(df_etf):
    """거래대금 상위 10개 ETF에 변동성/총보수/분배율 지표를 추가"""
    from concurrent.futures import ThreadPoolExecutor

    df_top = df_etf.sort_values(by='Amount', ascending=False).head(10)
    start_date = (now_kst() - timedelta(days=45)).strftime('%Y-%m-%d')

    def fetch_row(row):
        m = _fetch_etf_row_metrics(row['Symbol'], row['Name'], f"{int(row['Price']):,}원", start_date, need_weekly_return=False)
        m["거래대금"] = f"{row['Amount']/100:.1f}억 원" if row['Amount'] < 10000 else f"{row['Amount']/10000:.2f}조 원"
        return m

    rows = [row for _, row in df_top.iterrows()]
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_row, rows))

    df_res = pd.DataFrame(results)
    return _format_etf_metric_columns(df_res)

@st.cache_data(ttl=1800)
def fetch_etf_new_listing_top10(df_etf):
    """종목코드가 큰 순서(추정 신규상장) 10개 ETF에 변동성/총보수/분배율 지표를 추가"""
    from concurrent.futures import ThreadPoolExecutor

    df_top = df_etf.sort_values(by='Symbol', ascending=False).head(10)
    start_date = (now_kst() - timedelta(days=45)).strftime('%Y-%m-%d')

    def fetch_row(row):
        m = _fetch_etf_row_metrics(row['Symbol'], row['Name'], f"{int(row['Price']):,}원", start_date, need_weekly_return=False)
        m["시가총액"] = f"{float(row['MarCap']):,.0f}억 원" if float(row['MarCap']) < 10000 else f"{float(row['MarCap'])/10000:.2f}조 원"
        return m

    rows = [row for _, row in df_top.iterrows()]
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_row, rows))

    df_res = pd.DataFrame(results)
    return _format_etf_metric_columns(df_res)

def render_index_metric(label, data):
    """한국 증시 관례에 맞춰 상승=빨강, 하락=파랑으로 지수를 표시 (st.metric은 초록/빨강만 지원해 커스텀 HTML 사용)"""
    if data['status'] != 'success':
        st.metric(label=label, value=data['index'], delta=None)
        return

    change_str = data['change']  # 예: "+4.46%" 또는 "-5.33%"
    is_down = change_str.startswith('-')
    color = "#1857e0" if is_down else "#d60000"  # 파랑(하락) / 빨강(상승)
    arrow = "▼" if is_down else "▲"

    st.markdown(f"""
        <div style="padding: 4px 0;">
            <div style="font-size: 0.875rem; color: rgba(49,51,63,0.6);">{label}</div>
            <div style="font-size: 2.25rem; font-weight: 600; line-height: 1.2;">{data['index']}</div>
            <div style="font-size: 0.875rem; color: {color}; font-weight: 600;">{arrow} {change_str}</div>
        </div>
    """, unsafe_allow_html=True)

def render_news_links(news_list, expander_label="📰 참고한 관련 기사 보기"):
    """AI 분석에 근거로 사용된 뉴스 제목들을 원문 링크와 함께 접이식으로 표시"""
    if not news_list:
        return
    with st.expander(expander_label):
        for n in news_list:
            st.markdown(f"- [{n['title']}]({n['link']})  \n  <span style='color:gray; font-size:0.85rem;'>{n['press']}</span>", unsafe_allow_html=True)

# 각 메뉴별 UI 화면 구성
if menu == "종합 대시보드":
    st.subheader("오늘의 투자 핵심 요약")
    
    # 상단에 안정적으로 수집된 코스피/코스닥 지수 표시
    kospi_data = fetch_market_index("KOSPI")
    kosdaq_data = fetch_market_index("KOSDAQ")
    
    m_col1, m_col2, m_col3 = st.columns(3)
    with m_col1:
        render_index_metric("KOSPI", kospi_data)
    with m_col2:
        render_index_metric("KOSDAQ", kosdaq_data)

    st.markdown("---")

    # 반도체 대표 종목 당일 등락 스냅샷
    st.markdown("#### 🔧 반도체 대표 종목 당일 등락")
    with st.spinner("반도체 대표 종목 시세를 수집 중입니다..."):
        semi_data, semi_ts = fetch_semiconductor_snapshot()
    if semi_data:
        semi_cols = st.columns(len(semi_data))
        for s_col, (name, d) in zip(semi_cols, semi_data.items()):
            with s_col:
                st.metric(label=name, value=f"{d['price']:,.0f}원", delta=f"{d['change']:+.2f}%")
        render_data_ts_caption(semi_ts, cache_minutes=10)
    else:
        st.warning("반도체 데이터를 불러오지 못했습니다.")

    if GENAI_AVAILABLE:
        if st.button("🔄 지금 다시 분석", key="refresh_reason_analysis"):
            get_price_move_reason_analysis.clear()
        with st.spinner("AI가 코스피·반도체 등락 원인을 분석하고 있습니다..."):
            reason_analysis, kospi_news_used, semi_news_used, reason_meta = get_price_move_reason_analysis(kospi_data, semi_data)
        if not reason_meta["ai_ok"]:
            get_price_move_reason_analysis.clear()  # 실패 결과가 1시간 캐시되지 않도록 비움(다음 조회 시 재시도)
        st.info(f"**🔍 오늘 코스피·반도체 등락 원인 분석 (AI 추정)**\n\n{reason_analysis}")
        st.caption(f"⏱ 이 분석은 {reason_meta['ts']} 기준 데이터로 생성됨 (최대 1시간 캐시) · 뉴스 톤: {_format_sentiment_text(reason_meta['sentiment'])}")
        news_col1, news_col2 = st.columns(2)
        with news_col1:
            render_news_links(kospi_news_used, "📰 코스피 관련 참고 기사")
        with news_col2:
            render_news_links(semi_news_used, "📰 반도체 관련 참고 기사")
    else:
        st.info("💡 Groq 패키지가 설치되지 않아 AI 등락 원인 분석 기능을 사용할 수 없습니다.")

    st.markdown("---")
    
    col1, col2 = st.columns(2)
    with col1:
        st.info("🔥 최근 1달 자금 유입 TOP 5 업종")
        top_sectors, sectors_ts = fetch_1month_sector_trends()
        if top_sectors:
            for idx, sector in enumerate(top_sectors, 1):
                st.write(f"{idx}. {sector['업종/테마']} ({sector['변동']})")
            render_data_ts_caption(sectors_ts, cache_minutes=60)
        else:
            st.warning("데이터를 불러오지 못했습니다.")
    with col2:
        st.success("🎯 수급 & 기술적 조건 포착 종목")
        st.write("스크리너 메뉴는 주말 한국거래소(KRX) 서버 점검으로 인해 평일 장 거래 시간에 정상 가동됩니다.")
        
    st.markdown("---")
    if GENAI_AVAILABLE:
        st.markdown("### 🤖 AI 투자 비서의 데일리 시장 분석 & 전략")
        st.caption("※ 지수/업종 데이터뿐 아니라, 실제 시장 뉴스 헤드라인과 외국인/기관 순매수 동향을 근거로 작성합니다 (뉴스에 없는 내용은 추측하지 않도록 지시되어 있습니다).")
        if st.button("🔄 지금 다시 분석", key="refresh_market_briefing"):
            get_market_ai_briefing.clear()
        with st.spinner("AI가 오늘의 시장 상황과 실제 뉴스를 종합 분석하고 있습니다..."):
            market_briefing, market_news_used, briefing_meta = get_market_ai_briefing(kospi_data, kosdaq_data, top_sectors)
            if not briefing_meta["ai_ok"]:
                get_market_ai_briefing.clear()  # 실패 결과가 1시간 캐시되지 않도록 비움(다음 조회 시 재시도)
            st.info(market_briefing)
            st.caption(f"⏱ 이 분석은 {briefing_meta['ts']} 기준 데이터로 생성됨 (최대 1시간 캐시) · 뉴스 톤: {_format_sentiment_text(briefing_meta['sentiment'])}")
            st.caption(
                f"📊 외국인 순매수 상위: {', '.join(briefing_meta['foreign_top']) if briefing_meta['foreign_top'] else '데이터 없음'}  \n"
                f"📊 기관 순매수 상위: {', '.join(briefing_meta['institution_top']) if briefing_meta['institution_top'] else '데이터 없음'}"
            )
            render_news_links(market_news_used, "📰 참고한 시장 뉴스")
    else:
        st.info("💡 Groq 패키지가 설치되지 않아 AI 분석 기능을 사용할 수 없습니다.")

elif menu == "시장 자금 & 업종 분석":
    st.subheader("📊 시장 자금 흐름 & 스마트머니 수급 분석")
    st.markdown("---")
    
    tab1, tab2 = st.tabs(["🔥 최근 1달 자금 유입 5대 업종", "💰 외국인/기관 연속 순매수 분석"])
    
    with tab1:
        st.write("각 산업 섹터를 대표하는 주요 ETF들의 최근 22영업일(약 1개월) 추세를 바탕으로 단기 노이즈를 배제한 진짜 자금 유입 업종을 분석합니다.")
        with st.spinner("최근 1달간의 업종별 트렌드를 분석 중입니다..."):
            top_sectors, sectors_ts = fetch_1month_sector_trends()

            if top_sectors:
                df_sectors = pd.DataFrame(top_sectors)
                df_sectors.index = range(1, len(df_sectors) + 1)
                st.dataframe(df_sectors[['업종/테마', '변동']], width='stretch')
                render_data_ts_caption(sectors_ts, cache_minutes=60)
            else:
                st.error("업종 데이터를 불러오는 데 실패했습니다.")
            
    with tab2:
        st.write("선택한 기간 동안 외국인 및 기관이 가장 많이 순매수한 코스피/코스닥 상위 10개 종목을 도출하고 매수 사유(관련 최신 뉴스)를 분석합니다.")
        period_label = st.radio("집계 기간", list(NET_BUYING_PERIODS), index=1, horizontal=True)
        period = NET_BUYING_PERIODS[period_label]

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("#### 🌐 코스피 (KOSPI)")
            kospi_investor = st.radio("수급 주체 선택 (코스피)", ["외국인", "기관"], horizontal=True)
        with col2:
            st.markdown("#### 🚀 코스닥 (KOSDAQ)")
            kosdaq_investor = st.radio("수급 주체 선택 (코스닥)", ["외국인", "기관"], horizontal=True)
            
        if st.button("수급 종목 분석 시작"):
            with st.spinner("순매수 데이터를 수집하고 매수 사유(뉴스)를 분석 중입니다..."):
                kpi_type = "foreign" if kospi_investor == "외국인" else "institution"
                kdq_type = "foreign" if kosdaq_investor == "외국인" else "institution"
                
                kpi_stocks, kpi_ts = fetch_net_buying_top(kpi_type, "KOSPI", 10, period)
                kdq_stocks, kdq_ts = fetch_net_buying_top(kdq_type, "KOSDAQ", 10, period)

                st.markdown("---")
                c1, c2 = st.columns(2)
                with c1:
                    st.success(f"코스피 {kospi_investor} {period_label} 순매수 상위 10선")
                    if not kpi_stocks:
                        st.warning("순매수 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
                    for idx, stock in enumerate(kpi_stocks, 1):
                        news = fetch_headlines_rss(stock)
                        reason = news[0]['title'] if news else "관련 기사 없음 (기술적/패시브 자금 매수 추정)"
                        link = news[0]['link'] if news else "#"
                        st.markdown(f"**{idx}. {stock}**\n- 🔍 **매수 사유 분석**: [{reason}]({link})")
                        st.write("")
                    if kpi_stocks:
                        render_data_ts_caption(kpi_ts, cache_minutes=60)

                with c2:
                    st.success(f"코스닥 {kosdaq_investor} {period_label} 순매수 상위 10선")
                    if not kdq_stocks:
                        st.warning("순매수 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
                    for idx, stock in enumerate(kdq_stocks, 1):
                        news = fetch_headlines_rss(stock)
                        reason = news[0]['title'] if news else "관련 기사 없음 (기술적/패시브 자금 매수 추정)"
                        link = news[0]['link'] if news else "#"
                        st.markdown(f"**{idx}. {stock}**\n- 🔍 **매수 사유 분석**: [{reason}]({link})")
                        st.write("")
                    if kdq_stocks:
                        render_data_ts_caption(kdq_ts, cache_minutes=60)

elif menu == "주요 기업 헤드라인 뉴스":
    st.subheader("📰 시총 상위 기업 및 상한가 종목 헤드라인")
    st.markdown("---")
    
    tab1, tab2 = st.tabs(["시가총액 상위 기업 뉴스", "상한가 종목 및 이슈"])
    
    with tab1:
        st.markdown("### 🏆 코스피 상위 20위 & 코스닥 상위 10위 기업 뉴스")
        market_choice = st.radio("시장 선택", ["코스피 (상위 20위)", "코스닥 (상위 10위)"], horizontal=True)
        
        with st.spinner("시가총액 상위 기업 목록을 불러오는 중..."):
            if "코스피" in market_choice:
                companies, companies_ts = fetch_top_market_cap("KOSPI", 20)
            else:
                companies, companies_ts = fetch_top_market_cap("KOSDAQ", 10)

        if companies:
            render_data_ts_caption(companies_ts, cache_minutes=60)
            selected_company = st.selectbox("🎯 실시간 뉴스 브리핑을 보고 싶은 기업을 선택하세요:", companies)
            search_button = st.button(f"{selected_company} 뉴스 검색")

            if search_button:
               with st.spinner(f"'{selected_company}' 최신 이슈를 수신 중..."):
                    news_list = fetch_headlines_rss(selected_company)
            
               st.markdown(f"#### 📢 {selected_company} 실시간 주요 헤드라인")

               if news_list:
                # 1. AI 투자 비서 상자를 먼저 상단에 띄웁니다.
                   if GENAI_AVAILABLE:
                       st.markdown("### 🤖 AI 투자 비서의 3줄 시장 분석")
                       with st.spinner("AI가 실시간 호재/악재 감성 분석을 진행하고 있습니다..."):
                           ai_briefing = get_ai_summary(news_list)
                           st.info(ai_briefing)
                       st.markdown("---")

                # 2. 그 아래에 기존 뉴스 리스트가 차례대로 출력됩니다.
                   for idx, news in enumerate(news_list, 1):
                       st.markdown(f"**{idx}. [{news['press']}]** [{news['title']}]({news['link']})")
                       st.write("") # 한 줄씩 띄워주는 센스
               else:
                st.warning("현재 검색된 최신 뉴스 헤드라인이 없습니다.")
            
            else:
             st.error("기업 목록을 불러오지 못했습니다.")
            
    with tab2:
        st.markdown("### 🚀 상한가 도달 종목 및 주요 이슈")
        st.write("전 거래일 기준 상한가에 도달한 종목과 관련 최신 뉴스를 코스피/코스닥 별로 제공합니다.")
        
        if st.button("상한가 종목 및 이슈 분석 시작"):
            with st.spinner("상한가 종목 데이터와 관련 이슈를 분석 중입니다..."):
                upper_stocks, upper_ts = fetch_upper_limit_stocks()
                render_data_ts_caption(upper_ts, cache_minutes=60)

                st.markdown("#### 🔵 코스피 상한가 종목")
                if upper_stocks["KOSPI"]:
                    for stock in upper_stocks["KOSPI"]:
                        news = fetch_headlines_rss(stock)
                        issue_title = news[0]['title'] if news else "관련 최신 기사 없음"
                        issue_link = news[0]['link'] if news else "#"
                        st.markdown(f"- **{stock}** : [{issue_title}]({issue_link})")
                else:
                    st.info("코스피 상한가 종목이 없습니다.")

                st.markdown("#### 🔴 코스닥 상한가 종목")
                if upper_stocks["KOSDAQ"]:
                    for stock in upper_stocks["KOSDAQ"]:
                        news = fetch_headlines_rss(stock)
                        issue_title = news[0]['title'] if news else "관련 최신 기사 없음"
                        issue_link = news[0]['link'] if news else "#"
                        st.markdown(f"- **{stock}** : [{issue_title}]({issue_link})")
                else:
                    st.info("코스닥 상한가 종목이 없습니다.")

elif menu == "외인 수급 & 기술적 조건 스크리너":
    st.subheader("🔍 주도주 기술적 타점 랭킹 스크리너")

    scr_tab1, scr_tab2 = st.tabs(["🚀 실시간 타점 랭킹", "📊 스크리너 신호 백테스트"])

    with scr_tab1:
        st.info("""
        💡 **추천 기법 반영 (Scoring & Ranking System)**:
        엄격한 필터링(AND 조건)으로 인해 시장이 과열되거나 침체되었을 때 종목이 하나도 나오지 않는 현상을 방지합니다.
        대신, **'가장 트렌드와 일치하는 수급 주도주(거래량 상위 100개)'**를 대상으로 이동평균 배열/MACD/RSI/볼린저밴드/스토캐스틱/주간추세 6개 지표를 종합한 **기술적 타점 점수(100점 만점)**를 매깁니다.
        점수가 가장 높은 **상위 20개 종목을 각 지표 상태와 함께 리스트업**하여, 투자자가 데이터를 직접 보고 최적의 매수/매도 시점을 검증 및 판단할 수 있도록 고도화했습니다.
        """)

        with st.expander("📖 지표 해석 가이드 (RSI·볼린저밴드·스토캐스틱·주요 수급 보는 법)"):
            st.markdown("""
| 지표 | 정의 | 매수 관심 구간 | 중립 구간 | 매도/주의 구간 |
|---|---|---|---|---|
| **RSI (14)** | 최근 상승폭 대비 하락폭 비율(0~100). 낮을수록 많이 팔려 저평가된 상태 | 40 이하 (과매도) | 40~70 | 70 초과 (과매수, 차익실현 고려) |
| **볼린저밴드 %B** | 현재가가 20일 이동평균 밴드(±2표준편차) 내 어디쯤인지(0=하단, 1=상단) | 0.2 이하 (하단권) | 0.2~0.8 | 0.8 초과 (상단권, 과열 주의) |
| **스토캐스틱 %K** | 최근 14일 고가·저가 범위 대비 현재가 위치(0~100) | 20 이하 (과매도) | 20~80 | 80 초과 (과매수) |

- 표에 표시되는 값 뒤 괄호가 위 기준에 따른 신호이며, 세 지표 모두 "매수 관심"에 가까울수록 눌림목/저점 매수 관점에서, "과열/주의"에 가까울수록 단기 차익실현 관점에서 참고하시면 됩니다.
- 다만 개별 지표 하나만으로 매매를 판단하지 말고, **타점 점수(6개 지표 종합)·MACD 신호·배열 상태**를 함께 확인하세요. 예: RSI가 과매도라도 정배열이 무너진 하락 추세면 "떨어지는 칼날"일 수 있습니다.
- **주요 수급**: 외국인·기관의 최근 순매수/순매도 방향이 며칠째 이어지고 있는지 표시합니다 (예: "외국인 5일 연속 매수"). 네이버 API 특성상 최근 5거래일까지만 확인 가능해, 5일 연속으로 표시되어도 실제로는 더 오래 지속됐을 수 있습니다.
            """)

        st.markdown("---")

        search_btn = st.button("🚀 실시간 타점 랭킹 분석 시작")

        if search_btn:
            with st.spinner("시장 주도주 100개의 데이터를 수집하고 기술적 타점 점수·수급 동향을 계산 중입니다. (약 10~20초 소요)..."):
                screener_results, screener_ts = run_logical_screener()

            if screener_results:
                st.success(f"현재 시장에서 가장 기술적 타점이 우수한 상위 {len(screener_results)}개 종목입니다!")
                df_screen = pd.DataFrame(screener_results)
                df_screen.index = range(1, len(df_screen) + 1)
                st.dataframe(df_screen, width='stretch')
                render_data_ts_caption(screener_ts, cache_minutes=60)
            else:
                st.warning("데이터를 수집하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")

    with scr_tab2:
        st.info("""
        💡 **백테스트 방식**: 선택한 종목의 과거 가격 이력 전체에 왼쪽 탭과 동일한 채점 로직(정배열·MACD·RSI·볼린저밴드·스토캐스틱·주간추세, 100점 만점)을 매일 적용해,
        타점 점수가 기준점을 **새로 넘어선 첫날**마다 이후 지정한 거래일 수만큼 보유했을 때의 수익률을 계산합니다.
        "이 스크리너의 신호를 과거에 그대로 따라갔다면 실제로 통했는지"를 검증하는 용도입니다.
        """)
        st.markdown("---")

        bt_col1, bt_col2 = st.columns(2)
        with bt_col1:
            bt_input = st.text_input("종목명 또는 6자리 종목코드", key="backtest_stock_input", placeholder="예: 삼성전자 또는 005930")
        with bt_col2:
            bt_years = st.slider("백테스트 기간(년)", min_value=1, max_value=5, value=3, key="bt_years")

        bt_col3, bt_col4 = st.columns(2)
        with bt_col3:
            bt_threshold = st.slider("신호 기준 타점 점수", min_value=40, max_value=90, value=70, step=5, key="bt_threshold")
        with bt_col4:
            bt_holding = st.slider("보유 기간(거래일)", min_value=5, max_value=60, value=20, step=5, key="bt_holding")

        bt_btn = st.button("📊 백테스트 실행")

        if bt_btn:
            bt_input_clean = (bt_input or "").strip()
            bt_code, bt_name = None, bt_input_clean
            if bt_input_clean.isdigit() and len(bt_input_clean) == 6:
                bt_code = bt_input_clean
            elif bt_input_clean:
                bt_code = get_stock_code_map().get(bt_input_clean)

            if not bt_code:
                st.warning("올바른 종목명 또는 6자리 종목코드를 입력해주세요.")
            else:
                with st.spinner(f"'{bt_name}'의 최근 {bt_years}년 데이터로 백테스트 중입니다..."):
                    summary, df_signals, bt_ts = backtest_technical_signal(
                        bt_code, threshold=bt_threshold, holding_days=bt_holding, lookback_years=bt_years
                    )

                if summary is None:
                    st.warning("가격 데이터가 부족하거나 조회에 실패해 백테스트를 수행할 수 없습니다. 종목코드를 확인하거나 잠시 후 다시 시도해주세요.")
                elif summary["signal_count"] == 0:
                    st.info(f"최근 {bt_years}년간 타점 점수가 {bt_threshold}점을 넘은 신호가 없었습니다. 기준 점수를 낮춰보세요.")
                    render_data_ts_caption(bt_ts, cache_minutes=60)
                else:
                    st.success(f"'{bt_name}' 백테스트 결과 (최근 {bt_years}년, 신호 기준 {bt_threshold}점 이상, 보유 {bt_holding}거래일)")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("신호 발생 횟수", f"{summary['signal_count']}건")
                    c2.metric("상승 승률", f"{summary['win_rate']:.1f}%")
                    c3.metric("평균 수익률", f"{summary['avg_return']:+.2f}%")
                    if summary['baseline_avg'] is not None:
                        diff = summary['avg_return'] - summary['baseline_avg']
                        c4.metric(
                            "동일기간 무작위 대비", f"{diff:+.2f}%p",
                            help=f"아무 날짜에나 사서 {bt_holding}거래일 보유했을 때 평균 수익률({summary['baseline_avg']:+.2f}%) 대비 차이"
                        )
                    render_data_ts_caption(bt_ts, cache_minutes=60)
                    st.markdown("#### 개별 신호 내역")
                    display_signals = df_signals.copy()
                    display_signals['진입가'] = display_signals['진입가'].apply(lambda x: f"{x:,.0f}원")
                    display_signals['청산가'] = display_signals['청산가'].apply(lambda x: f"{x:,.0f}원")
                    st.dataframe(display_signals, hide_index=True, use_container_width=True)
                    st.caption("※ 과거 데이터 기반 시뮬레이션 결과이며 미래 수익을 보장하지 않습니다. 신호는 '타점 점수가 기준점을 새로 넘어선 첫날'만 집계해 연속 고득점 구간의 중복 계산을 방지했습니다.")

elif menu == "최우수 애널리스트 추천 종목":
    st.subheader("🏆 2026 최우수 애널리스트 & 주요 증권사 추천 종목")
    
    st.info(f"""
    💡 **신뢰도 향상 로직 안내**: 네이버 금융 등 공개 포털에서는 리포트 목록에 작성자(애널리스트) 실명이 제공되지 않아 개별 인물 단위의 필터링이 어렵습니다. 
    이를 해결하기 위해, 최근 **매일경제 베스트 애널리스트 종합 평가(리서치센터 부문)에서 최상위권(신한투자증권, 하나증권, 메리츠증권, KB증권, NH투자증권)**에 
    오른 '리서치 명가' 5곳의 리포트만을 집중 선별하여 데이터의 신뢰성을 극대화했습니다.

    📌 **선정 기준**: {ANALYST_RANKING_BASIS}
    🗓️ **이 3개사 리스트 최종 확인/갱신일**: {ANALYST_LIST_LAST_UPDATED} (매경 순위가 갱신되면 코드도 함께 수동으로 갱신해야 합니다)
    """)
    st.markdown("---")
    
    with st.spinner("최우수 리서치센터의 최근 추천 리포트를 수집 중입니다... (증권사에 따라 최대 60페이지까지 조회하여 다소 시간이 걸릴 수 있습니다)"):
        recom_list, recom_ts = fetch_top_analyst_recommendations()

    if recom_list:
        st.success("매경 베스트 리서치센터 최상위 증권사들이 발간한 핵심 추천 종목입니다.")
        st.caption("※ 특정 증권사가 결과를 독점하지 않도록, 5개 증권사별 최근 리포트를 최대 4건씩 균형 있게 표시합니다.")
        st.caption("※ 증권사에 따라 네이버 금융에 리포트가 게시되는 빈도가 달라, 일부 증권사는 이번 조회에서 적게 나오거나 안 나올 수 있습니다.")
        render_data_ts_caption(recom_ts, cache_minutes=60)
        df_recom = pd.DataFrame(recom_list)
        df_recom.index = range(1, len(df_recom) + 1)
        
        for i, row in df_recom.iterrows():
            st.markdown(f"**{i}. {row['종목명']}** | 🏢 {row['발간 증권사']} (발간일: {row['발간일']})")
            st.markdown(f"↪ 📄 [{row['리포트 제목']}]({row['링크']})")
            st.write("")
    else:
        st.warning("최근 1주일 내 추천 종목 데이터를 불러오지 못했거나 발간된 리포트가 없습니다.")

elif menu == "가치재평가주":
    st.subheader("💎 가치재평가주 (Value Re-evaluation) 스크리닝")

    st.info("""
    💡 **스크리닝 방식**: 시가총액 상위 N개 종목을 대상으로 각 종목의 실제 PBR·영업이익률·매출액·모멘텀 데이터를 실시간으로 조회하여,
    조건(저 PBR / 고수익성 / 고성장 / 종합점수)에 부합하는 상위 종목을 그때그때 계산합니다. (고정된 예시 리스트가 아닙니다)

    ⚠️ 단, 스캔 대상을 "시가총액 상위 N개"로 한정하기 때문에, 고성장/고수익성 테마에 흔한 중소형주가 스캔 범위 밖에 있을 수 있습니다.
    더 폭넓게 보고 싶다면 아래 스캔 수를 늘려주세요 (다만 조회 시간이 늘어납니다).
    """)
    st.markdown("---")

    scan_size = st.slider("스캔 대상 시가총액 상위 종목 수", min_value=50, max_value=500, value=300, step=50)
    st.caption("※ 종목마다 개별 페이지+가격 데이터를 실시간 조회하므로, 스캔 수를 늘리면 정확도(대상 폭)는 넓어지지만 조회 시간도 늘어납니다 (300종목 기준 약 1~2분 소요).")
    run_value_scan = st.button("💎 실시간 스캔 시작")

    if run_value_scan:
        with st.spinner(f"시가총액 상위 {scan_size}개 종목의 PBR/영업이익률/매출액/모멘텀 데이터를 실시간 조회 중입니다..."):
            df_result, scan_ts, scan_status = build_quant_filter_candidates(market_cap_min=0, included_sector="", max_scan=scan_size)
            st.session_state['value_scan_df'] = df_result
            st.session_state['value_scan_ts'] = scan_ts
            st.session_state['value_scan_status'] = scan_status
            st.session_state['value_scan_size'] = scan_size

    if st.session_state.get('value_scan_df') is not None and not st.session_state['value_scan_df'].empty:
        df_scan = st.session_state['value_scan_df']
        scanned_n = st.session_state.get('value_scan_size', scan_size)
        render_data_ts_caption(st.session_state.get('value_scan_ts'), cache_minutes=30)

        tab1, tab2, tab3, tab4 = st.tabs([
            "📉 1. 저 PBR 종목 (상위 20선)", "💰 2. 고수익성 종목 (영업이익률 평균 상위)",
            "🚀 3. 고성장 종목 (매출성장률 평균 상위)", "🏆 4. 종합점수 상위 (멀티팩터)"
        ])

        with tab1:
            st.markdown("#### 기업가치 대비 저평가된 저 PBR 상위 20종목")
            low_pbr = df_scan[df_scan['PBR'] > 0].sort_values('PBR', ascending=True).head(20)
            if low_pbr.empty:
                st.warning("조건에 맞는 종목을 찾지 못했습니다.")
            else:
                display = low_pbr[['종목명', '종목코드', 'PBR', '시가총액', '업종']].copy()
                display['시가총액'] = display['시가총액'].apply(_format_market_cap)
                st.dataframe(display, hide_index=True, use_container_width=True)
                st.caption(f"※ 시가총액 상위 {scanned_n}개 종목 중 PBR이 0보다 크면서 가장 낮은 순")

        with tab2:
            st.markdown("#### 확인 가능한 연도 기준, 영업이익률 평균이 가장 높은 상위 20종목")
            high_margin = df_scan.dropna(subset=['영업이익률평균']).sort_values('영업이익률평균', ascending=False).head(20)
            if high_margin.empty:
                st.warning("조건에 맞는 종목을 찾지 못했습니다.")
            else:
                display = high_margin[['종목명', '종목코드', '영업이익률평균', '영업이익률_확인연도수', '시가총액', '업종']].copy()
                display['영업이익률평균'] = display['영업이익률평균'].apply(lambda x: f"{x:.2f}%")
                display['시가총액'] = display['시가총액'].apply(_format_market_cap)
                st.dataframe(display, hide_index=True, use_container_width=True)
                st.caption("※ '영업이익률_확인연도수'는 네이버 금융에 공시된 연간 실적 중 실제로 확인 가능했던 연도 수입니다(기업마다 상이할 수 있음).")

        with tab3:
            st.markdown("#### 확인 가능한 연도 기준, 평균 매출성장률(YoY)이 가장 높은 상위 20종목")
            high_growth = df_scan.dropna(subset=['매출성장률']).sort_values('매출성장률', ascending=False).head(20)
            if high_growth.empty:
                st.warning("조건에 맞는 종목을 찾지 못했습니다.")
            else:
                display = high_growth[['종목명', '종목코드', '매출성장률', '매출성장률_확인연도수', '시가총액', '업종']].copy()
                display['매출성장률'] = display['매출성장률'].apply(lambda x: f"{x:+.2f}%")
                display['시가총액'] = display['시가총액'].apply(_format_market_cap)
                st.dataframe(display, hide_index=True, use_container_width=True)
                st.caption("※ '매출성장률_확인연도수'는 평균 계산에 사용된 연도별 YoY 성장률 개수입니다(기업마다 상이할 수 있음).")

        with tab4:
            st.markdown("#### 가치·품질·성장·모멘텀 4개 팩터를 종합한 점수 상위 20종목")
            st.caption("※ 각 팩터는 이번 스캔 대상(시가총액 상위 " + str(scanned_n) + "개) 안에서의 상대 순위(백분위)로 환산해 25점씩 동일 가중 합산한 점수입니다. 절대 기준이 아니므로 스캔 범위가 바뀌면 점수도 달라질 수 있습니다.")
            top_score = df_scan.sort_values('종합점수', ascending=False).head(20)
            if top_score.empty:
                st.warning("조건에 맞는 종목을 찾지 못했습니다.")
            else:
                display = top_score[['종목명', '종목코드', '종합점수', '가치점수', '품질점수', '성장점수', '모멘텀점수', '시가총액', '업종']].copy()
                for col in ['종합점수', '가치점수', '품질점수', '성장점수', '모멘텀점수']:
                    display[col] = display[col].round(1)
                display['시가총액'] = display['시가총액'].apply(_format_market_cap)
                st.dataframe(display, hide_index=True, use_container_width=True)
                st.markdown("---")
                render_portfolio_correlation(top_score, key_prefix="value_score")
    elif st.session_state.get('value_scan_df') is not None:
        render_quant_scan_status(st.session_state.get('value_scan_status'))
    else:
        st.info("위 '실시간 스캔 시작' 버튼을 눌러 조회를 시작하세요.")

elif menu == "퀀트 투자 리스트":
    st.subheader("🧠 퀀트 투자 리스트")
    st.markdown("조건을 입력하면 실제 KRX 상장 종목 데이터를 실시간으로 조회하여 조건에 맞는 리스트를 보여줍니다.")
    st.info("예시 조건: PER ≤ 15, PBR ≤ 1.5, ROE ≥ 10, 영업이익률 ≥ 5")

    with st.expander("📌 필터 조건 설정", expanded=True):
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        with col1:
            per_limit = st.number_input("PER 상한선", min_value=0.0, value=15.0, step=0.5)
        with col2:
            pbr_limit = st.number_input("PBR 상한선", min_value=0.0, value=1.5, step=0.1)
        with col3:
            roe_min = st.number_input("ROE 하한선 (%)", min_value=0.0, value=10.0, step=0.5)
        with col4:
            op_margin_min = st.number_input("영업이익률 하한선 (%)", min_value=0.0, value=5.0, step=0.5)
        with col5:
            revenue_growth_min = st.number_input("매출성장률 하한선 (%)", min_value=-100.0, value=10.0, step=1.0)
        with col6:
            momentum_min = st.number_input("모멘텀(1개월수익률) 하한선 (%)", min_value=-100.0, value=-100.0, step=1.0)

        col7, col8, col9 = st.columns(3)
        with col7:
            market_cap_min = st.number_input("시가총액 최소값(억원)", min_value=0, value=1000, step=100)
        with col8:
            included_sector = st.text_input("업종 키워드(예: 반도체, 바이오)", value="")
        with col9:
            top_n = st.slider("표시 종목 수", min_value=5, max_value=50, value=20, step=5)

        max_scan = st.slider(
            "스캔 대상 최대 종목 수 (시가총액/업종 조건 통과 종목 중 시가총액 상위 N개만 상세 조회)",
            min_value=30, max_value=400, value=150, step=10
        )
        st.caption("※ 종목마다 개별 페이지+가격 데이터를 실시간 조회하므로, 스캔 수를 늘리면 정확도(대상 폭)는 넓어지지만 조회 시간도 늘어납니다.")

        sort_column = st.selectbox("정렬 기준", ["종합점수", "ROE", "영업이익률", "PER", "PBR", "매출성장률", "모멘텀"])

    run_scan = st.button("🔍 조건에 맞는 종목 스캔 시작")

    if run_scan:
        with st.spinner(f"시가총액/업종 조건으로 후보를 추린 뒤 상위 {max_scan}개 종목의 펀더멘털을 실시간 조회 중입니다..."):
            df_candidates, scan_ts, scan_status = build_quant_filter_candidates(
                market_cap_min=market_cap_min,
                included_sector=included_sector,
                max_scan=max_scan,
            )

        if df_candidates.empty:
            render_quant_scan_status(scan_status)
        else:
            render_data_ts_caption(scan_ts, cache_minutes=30)
            df_candidates = df_candidates.dropna(subset=["PER", "PBR", "ROE", "영업이익률"]).copy()
            mask = (
                (df_candidates["PER"] > 0) &
                (df_candidates["PER"] <= per_limit) &
                (df_candidates["PBR"] > 0) &
                (df_candidates["PBR"] <= pbr_limit) &
                (df_candidates["ROE"] >= roe_min) &
                (df_candidates["영업이익률"] >= op_margin_min)
            )
            filtered = df_candidates.loc[mask].copy()

            # 매출성장률/모멘텀: 데이터가 없는 종목(None)은 조건 판단에서 제외하지 않고 통과시킴
            filtered = filtered.loc[filtered["매출성장률"].isna() | (filtered["매출성장률"] >= revenue_growth_min)]
            filtered = filtered.loc[filtered["모멘텀"].isna() | (filtered["모멘텀"] >= momentum_min)]

            if filtered.empty:
                st.warning("입력한 조건에 맞는 종목이 없습니다. 기준을 완화해 보세요.")
            else:
                filtered = filtered.sort_values(by=sort_column, ascending=False)
                filtered = filtered.head(top_n)
                st.success(f"조건에 맞는 종목 리스트 (시가총액 상위 {max_scan}개 종목 중 스캔)")
                display = filtered[["종목명", "종목코드", "종합점수", "PER", "PBR", "ROE", "영업이익률", "매출성장률", "모멘텀", "시가총액", "업종"]].copy()
                display["종합점수"] = display["종합점수"].round(1)
                display["시가총액"] = display["시가총액"].apply(_format_market_cap)
                st.dataframe(display, hide_index=True, use_container_width=True)
                st.caption(f"적용 조건: PER ≤ {per_limit}, PBR ≤ {pbr_limit}, ROE ≥ {roe_min}%, 영업이익률 ≥ {op_margin_min}%, 매출성장률 ≥ {revenue_growth_min}%, 모멘텀 ≥ {momentum_min}%(매출성장률·모멘텀은 데이터 없는 종목은 통과), 시가총액 ≥ {market_cap_min}억, 업종 키워드: {included_sector or '전체'}")
                st.caption("※ 매출성장률은 네이버 금융에 공시된 연간 매출액 중 확인 가능한 연도들의 평균 YoY 성장률, 모멘텀은 최근 1개월 가격 수익률입니다. 종합점수는 이번 스캔 대상 안에서의 가치·품질·성장·모멘텀 상대 순위를 25점씩 가중합산한 값(0~100)입니다.")

                st.markdown("---")
                render_portfolio_correlation(filtered, key_prefix="quant_list")

def calculate_stock_score(fundamentals, tech, news_list):
    """개선된 점수 체계화 모델 (뉴스/수급 40 + 밸류에이션 10 + 트렌드 10 + 기술적지표 20 + 경영지표 20 = 100점)
    반환: (총점, 뉴스/수급 점수, 밸류에이션 점수, 트렌드 점수, 기술적지표 점수, 경영지표 점수)
    — 화면에서 이 세부 점수를 다시 계산하지 않고 그대로 재사용할 것 (중복 계산 시 총점과 어긋날 위험)"""
    
    # --------------------------------------------------
    # 1. 뉴스 및 수급 점수 체계화 (40점 만점)
    # --------------------------------------------------
    pos_keywords = ['공급계약', '특허', '흑자전환', 'M&A', '인수', '개발', '상승', '돌파', '수주', '호실적']
    neg_keywords = ['적자', '하락', '감소', '취소', '소송', '과징금', '유상증자', '횡령', '부도']
    
    pos_count = 0
    neg_count = 0
    
    # 가중치 언론사 및 공시 필터링
    premium_sources = ['연합인포맥스', '이데일리', '매일경제', '한국경제', '공시', 'DART']

    # 문맥을 반영한 AI 감성 판정 시도 (실패 시 None → 아래에서 키워드 매칭으로 자동 대체)
    ai_sentiment_map = classify_news_sentiment_ai(news_list)

    for idx, n in enumerate(news_list):
        title = n['title']
        press = n['press']
        
        # 신뢰도 높은 소스이거나 공시인 경우 가중치 (+1)
        source_weight = 2 if any(ps in press for ps in premium_sources) else 1

        if ai_sentiment_map is not None:
            # AI가 문맥을 고려해 판정한 감성 사용
            verdict = ai_sentiment_map.get(idx, "중립")
            if verdict == "긍정":
                pos_count += 1 * source_weight
            elif verdict == "부정":
                neg_count += 1 * source_weight
        else:
            # 폴백: 기본 감성 단어 매칭
            p_match = sum(1 for kw in pos_keywords if kw in title)
            n_match = sum(1 for kw in neg_keywords if kw in title)
            if p_match > n_match:
                pos_count += 1 * source_weight
            elif n_match > p_match:
                neg_count += 1 * source_weight

    # A. 뉴스 감성 점수 (15점 만점, 뉴스 있으면 0~15 전 구간 선형)
    total_news = len(news_list)
    if total_news > 0:
        # (긍정 - 부정) / 전체 비율을 0~15 구간으로 매핑
        sentiment_ratio = (pos_count - neg_count) / total_news
        sentiment_score = ((sentiment_ratio + 1) / 2) * 15
    else:
        sentiment_score = 5.0 # 뉴스 없을 시 기본 점수
        
    # B. 뉴스 노출 빈도 점수 (10점 만점, 변경 없음)
    if total_news >= 5: freq_score = 10
    elif total_news >= 3: freq_score = 7
    elif total_news >= 1: freq_score = 4
    else: freq_score = 0
    
    # C. 거래량 모멘텀 점수 (15점 만점, 기존 10점 만점 기준값의 ×1.5)
    v_ratio = tech.get('volume_ratio', 100.0)
    if v_ratio >= 200: vol_score = 15      # 평소 대비 거래량 2배 폭발
    elif v_ratio >= 150: vol_score = 12
    elif v_ratio >= 100: vol_score = 9     # 평소 수준 유지
    elif v_ratio >= 50: vol_score = 4.5
    else: vol_score = 0
    
    news_suqub_total = int(sentiment_score + freq_score + vol_score)
    news_suqub_total = min(40, max(0, news_suqub_total)) # 40점 방어선 (15+10+15=40, 총합 동일)
    
    # --------------------------------------------------
    # 2. 업종별 상대 PER 전략 (10점 만점)
    # --------------------------------------------------
    val_score = 0
    per = fundamentals['cns_per'] if fundamentals['cns_per'] > 0 else fundamentals['per']
    sector_per = fundamentals.get('sector_per', 0.0)
    
    if per > 0 and sector_per > 0:
        relative_per_index = per / sector_per
        
        if relative_per_index < 0.7:
            val_score = 6  # 매우 저평가
        elif 0.7 <= relative_per_index < 0.9:
            val_score = 4  # 적정 저평가
        elif 0.9 <= relative_per_index < 1.1:
            val_score = 2  # 보통
        else:
            val_score = 0  # 고평가
    else:
        # PER 정보가 없거나 비교 불가능할 경우 기존 기본값 부여
        val_score = 3
        
    # PBR 보조 점수 (기존 밸류에이션 점수 중 PBR 비중 유지, 최대 4점)
    if 0 < fundamentals['pbr'] < 1.5: val_score += 4
    elif 1.5 <= fundamentals['pbr'] < 3: val_score += 2
    
    # --------------------------------------------------
    # 3. 나머지 점수 체계 (경영 20점 + 기술/트렌드 30점 = 총 50점)
    # --------------------------------------------------
    # 시장 트렌드 (10점) — 기술적 지표 비중을 늘리는 대신 절반으로 축소 (기술/트렌드 합계 30점은 유지)
    trend_score = 5
    if tech['one_month_return'] > 5: trend_score = 10
    elif tech['one_month_return'] > 0: trend_score = 7
    elif tech['one_month_return'] < -10: trend_score = 0

    # 기술적 지표 (20점) — "기술적 조건" 스크리너라는 이름값에 비해 배점이 지나치게 작았던 문제 개선.
    # 이동평균 배열(6)+MACD(5)+RSI(5)+볼린저밴드(2)+스토캐스틱(2) — run_logical_screener와 같은 비율 취지, 신규 지표 2종을 위해 기존 8:6:6을 6:5:5로 재분배
    tech_score = 0
    if tech['price'] > tech['sma20'] and tech['sma20'] > tech['sma60']:
        tech_score += 6   # 완벽 정배열
    elif tech['sma20'] > tech['sma60']:
        tech_score += 4   # 20/60 정배열(눌림목)
    elif tech['price'] > tech['sma20']:
        tech_score += 2   # 20일선 회복(반등중)

    if tech['macd'] > tech['signal']:
        tech_score += 5   # 매수 우위
    elif tech['macd'] > 0:
        tech_score += 2   # 조정 중(0선 위)

    if tech['rsi'] <= 40:
        tech_score += 5   # 과매도(눌림목) 매수 타점
    elif tech['rsi'] <= 55:
        tech_score += 3
    elif tech['rsi'] <= 70:
        tech_score += 1   # 70 초과 과열권은 가점 없음

    bb_pct_b = tech.get('bb_pct_b')
    if bb_pct_b is not None and pd.notna(bb_pct_b):
        if bb_pct_b <= 0.2: tech_score += 2      # 볼린저밴드 하단 근접(과매도)
        elif bb_pct_b <= 0.8: tech_score += 1

    stoch_k = tech.get('stoch_k')
    if stoch_k is not None and pd.notna(stoch_k):
        if stoch_k <= 20: tech_score += 2        # 스토캐스틱 과매도 구간
        elif stoch_k <= 80: tech_score += 1

    # 경영지표 (20점)
    mgmt_score = 0
    if fundamentals['op_margin'] >= 10: mgmt_score += 12
    elif fundamentals['op_margin'] >= 5: mgmt_score += 8
    else: mgmt_score += 4

    if fundamentals['roe'] >= 10: mgmt_score += 8
    elif fundamentals['roe'] >= 5: mgmt_score += 5
    else: mgmt_score += 2

    total_score = news_suqub_total + val_score + trend_score + tech_score + mgmt_score
    return int(total_score), news_suqub_total, val_score, trend_score, tech_score, mgmt_score

# ==========================================
# AI 종목 진단 — 판정은 전부 diagnosis_core.py의 규칙(같은 입력 → 같은 결과). LLM은 '재료 요약'에만 사용하며 등급에 반영하지 않음.
# ==========================================
# DART 키는 환경변수 DART_API_KEY 또는 .streamlit/secrets.toml 에서 읽는다(dart_core). 클라우드 Secrets 는 st.secrets 로 오므로 환경변수로 옮겨 둔다.
try:
    if "DART_API_KEY" in st.secrets:
        os.environ.setdefault("DART_API_KEY", str(st.secrets["DART_API_KEY"]))
except Exception:
    pass    # secrets 가 없는 환경 — 위험 공시 확인만 '키 없음'으로 안내하고 나머지 진단은 그대로 동작
dart_core.UNREACHABLE_TTL = 600     # DART에 접속이 안 되면(배포 서버는 해외 접속이 막힘) 10분간은 종목마다 다시 기다리지 않는다

DART_UNREACHABLE = ("이 환경에서는 DART에 접속되지 않습니다(접속 시간 초과). 배포 앱(Streamlit Cloud)은 해외 서버라 "
                    "DART가 접속을 받지 않는 것으로 확인됐고, 이 PC에서 로컬로 실행하면 정상 동작합니다.")

def dart_unreachable(payload):
    """fetch_* 가 돌려준 오류 사유가 '접속 불가'인지 — 키·한도 문제와 다른 안내를 하려고 구분한다."""
    return "ConnectTimeout" in str(payload)

DIAG_VERDICT_COLORS = {
    "매수 고려": "green", "지켜보기": "orange", "사지 않기": "red",
    "매도 고려": "red", "보유 유지": "green", "손절 고려": "red",
    "분할 익절 고려": "orange", "물타기 고려": "blue", "추세 점검": "orange",
}
DIAG_POSITIVE_STATES = ("상승", "유입", "급증↑", "쌍바닥", "역헤드앤숄더", "박스권 돌파")
DIAG_NEGATIVE_STATES = ("하락", "이탈", "급증↓", "쌍봉", "헤드앤숄더", "박스권 이탈")

@st.cache_data(ttl=300)
def fetch_diag_ohlcv(code, lookback_days):
    df = dc.fetch_ohlcv(code, lookback_days)
    if df is None or df.empty:
        # 예외는 st.cache_data에 캐시되지 않으므로, 일시적인 조회 실패가 5분간 굳어지는 것을 막는다.
        raise ValueError("시세 데이터를 불러오지 못했습니다.")
    return df

@st.cache_data(ttl=300)
def fetch_diag_flow(code):
    flow = dc.fetch_investor_flow(code)
    if flow is None:
        raise ValueError("수급 데이터를 불러오지 못했습니다.")
    return flow

@st.cache_data(ttl=21600)
def get_code_market_map():
    """종목코드 -> 'KOSPI'/'KOSDAQ'. 시장 지수 대비 모멘텀(참고 신호)에 쓴다."""
    universe = fetch_market_universe()
    return dict(zip(universe["Code"], universe["Market"])) if not universe.empty else {}

@st.cache_data(ttl=300)
def fetch_diag_index(market, lookback_days):
    s = dc.fetch_index_close(market, lookback_days)
    if s is None:
        raise ValueError("지수 데이터를 불러오지 못했습니다.")   # 예외는 캐시되지 않는다
    return s

def get_diag_index(code, lookback_days):
    """종목이 속한 시장 지수의 종가. 시장을 모르거나 조회에 실패하면 None → 모멘텀 참고 신호만 '평가 불가'로 두고 나머지는 그대로 진단."""
    market = get_code_market_map().get(code)
    if not market:
        return None
    try:
        return fetch_diag_index(market, lookback_days)
    except Exception:
        return None

@st.cache_data(ttl=1800)
def get_news_sentiment_counts(news_list):
    """뉴스 감성(긍정/중립/부정) 개수. AI 판정 실패는 예외로 알려 캐시되지 않게 한다."""
    sentiment = classify_news_sentiment_ai(news_list)
    if sentiment is None:
        raise RuntimeError("감성 분류에 실패했습니다.")
    counts = {"긍정": 0, "중립": 0, "부정": 0}
    for verdict in sentiment.values():
        counts[verdict] += 1
    return counts

@st.cache_data(ttl=1800)
def get_material_summary_ai(stock_name, news_list):
    """'요즘 이 종목의 재료' 3줄 요약. 기사 제목에 없는 내용은 지어내지 않도록 제한."""
    news_text = "\n".join([f"- [{n['press']}] {n['title']}" for n in news_list])
    prompt = f"""
    당신은 주식 뉴스 요약가입니다. 아래는 '{stock_name}' 관련 최근 기사 제목입니다.
    투자자가 알아야 할 '요즘 이 종목의 재료'를 한국어 불릿 3개 이내로 요약하세요.
    - 기사 제목에 없는 내용(수치·원인·전망 포함)은 절대 지어내지 마세요.
    - 매수/매도 의견이나 호재·악재 판단은 쓰지 말고, 어떤 일이 있었는지 사실만 정리하세요.
    - 반드시 한국어로만 작성하세요.

    [기사 제목]
    {news_text}
    """
    ok, content = call_groq_chat([{"role": "user", "content": prompt}], temperature=0.3)
    if not ok:
        raise RuntimeError(content)
    return content

def resolve_diag_stock(user_input):
    """종목명 또는 6자리 코드 → (코드, 종목명, 비슷한 종목명 후보). 찾지 못하면 코드가 None."""
    code_map = get_stock_code_map()
    text = user_input.strip()
    if text.isdigit() and len(text) == 6:
        return text, next((n for n, c in code_map.items() if c == text), text), []
    if text in code_map:
        return code_map[text], text, []
    return None, text, [n for n in code_map if text and text in n][:5]

def render_diag_pillar(col, axis, state, detail):
    color = "#d60000" if state.startswith(DIAG_POSITIVE_STATES) else "#1857e0" if state.startswith(DIAG_NEGATIVE_STATES) else "#6b7280"
    col.markdown(f"""
        <div style="border:1px solid #e6e6e6; border-radius:8px; padding:10px 12px; height:100%;">
            <div style="font-size:0.8rem; color:rgba(49,51,63,0.6);">{axis}</div>
            <div style="font-size:1.15rem; font-weight:700; color:{color}; line-height:1.3;">{state}</div>
            <div style="font-size:0.75rem; color:gray;">{detail}</div>
        </div>
    """, unsafe_allow_html=True)

def render_check_line(c):
    icon = {True: "✅", False: "❌", None: "➖"}[c.passed]
    st.markdown(f"{icon} **{c.axis}** · {c.label}  \n<span style='color:gray; font-size:0.85rem;'>{c.detail}</span>",
                unsafe_allow_html=True)

@st.cache_data(ttl=600, show_spinner=False)
def fetch_risk_flags(code, as_of_str):
    """DART에서 최근 위험 공시(감자·관리종목·감사의견)를 찾는다. 실패는 예외 대신 ('err', 사유)로 돌려 10분간 DART를 다시 두드리지 않는다."""
    try:
        return "ok", rdisc.fetch_flags(code, datetime.strptime(as_of_str, "%Y-%m-%d").date())
    except Exception as e:      # DartError 메시지는 키를 담지 않는다(dart_core)
        return "err", f"{type(e).__name__}: {str(e)[:100]}"

def run_risk_flags(codes, max_workers=6):
    """관심종목 전체의 위험 공시를 병렬로 확인. 반환: {코드: ('ok', 목록) 또는 ('err', 사유)}."""
    from concurrent.futures import ThreadPoolExecutor
    today = now_kst().strftime("%Y-%m-%d")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return dict(zip(codes, executor.map(lambda c: fetch_risk_flags(c, today), codes)))

def render_risk_flags(code, risk_asof=None):
    """검증을 통과한 위험 공시(감자 · 관리종목 지정/상장폐지 사유 · 감사의견)가 최근 있었는지 DART에서 찾아 경고로 보여준다. 등급·신호에는 반영하지 않는다."""
    asof = risk_asof or now_kst().date()
    status, payload = fetch_risk_flags(code, asof.strftime("%Y-%m-%d"))
    if status == "err":
        if dart_unreachable(payload):
            st.caption(f"⚠️ 위험 공시(감자·관리종목·감사의견)를 확인하지 못했습니다. '위험 공시 없음'이 아니라 '확인 못 함'입니다. {DART_UNREACHABLE}")
        else:
            st.caption(f"⚠️ DART 위험 공시를 확인하지 못했습니다({payload}). Secrets에 DART_API_KEY가 있는지 확인해 주세요.")
        return
    recent = [f for f in payload if f["in_window"]]
    older = [f for f in payload if not f["in_window"]]
    for f in recent:
        box, icon = (st.error, "🔴") if f["level"] == "경고" else (st.warning, "🟠")
        box(f"**{icon} 위험 공시 {f['level']} — {f['label']}** ({f['date']:%Y-%m-%d} 공시, {f['days_ago']}일 전)  \n"
            f"{f['evidence']}  \n[DART 원문 보기]({f['url']})")
    if recent:
        with st.expander("위험 공시 경고의 근거와 한계"):
            for f in recent:
                st.markdown(f"- **{f['label']}** — {f['caveat']}")
            st.markdown(
                f"- 근거: {rdisc.EVIDENCE_NOTE}. 같은 날·같은 시장 다른 종목과 견주고, 두 기간(2019~2022 / 2023~2026)에서 방향이 같은지 확인한 유형만 담았습니다.\n"
                "- 효과 크기는 평균 −5% 안팎으로 작고, 공시 당일에 이미 평균 −1% 안팎이 반영돼 있습니다. 개별 종목의 하락을 예측하는 것이 아니라 '이 공시 뒤 평균적으로 시장보다 약했다'는 과거 통계입니다.\n"
                "- 거래정지·상장폐지 종목은 가격이 멈춰 수익률 0으로 잡혀 하락이 과소평가됩니다. 등급과 신호에는 반영하지 않았습니다.")
    if older:
        st.caption("검증 구간(공시 후 30일) 밖의 최근 6개월 내 위험 공시: "
                   + " · ".join(f"[{f['label']} {f['date']:%Y-%m-%d}]({f['url']})" for f in older))
    if not payload:
        st.caption("➖ 최근 6개월 DART 공시에 감자·관리종목 지정/상장폐지 사유·감사의견 부적정 관련 위험 공시가 없습니다(그 밖의 위험은 이 확인에 포함되지 않습니다)."
                   + (" 과거 날짜 진단이라 그 날짜까지의 공시만 봅니다." if risk_asof else ""))

# --- 종목 분석 보고서(1단계: DART 숫자만) -------------------------------------------------------
# 모든 숫자는 company_report.build_report 한 곳에서 계산하고 여기서는 그리기만 한다(같은 지표를 화면마다 다시 계산하지 않는다).
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_company_report_raw(code, today_str):
    """DART 원자료. 실패는 예외 대신 ('err', 사유)·('nodata', 사유)로 돌려 같은 종목을 열 때마다 DART를 다시 두드리지 않는다."""
    try:
        return "ok", crep.fetch_raw(code, datetime.strptime(today_str, "%Y-%m-%d").date())
    except crep.NoDartData as e:
        return "nodata", str(e)
    except Exception as e:      # DartError 메시지는 키를 담지 않는다(dart_core)
        return "err", f"{type(e).__name__}: {str(e)[:100]}"

@st.cache_data(ttl=21600, show_spinner=False)
def fetch_naver_annual(code):
    """네이버 연간 재무(연도별 PER) — 밸류 시뮬레이터의 과거 PER 범위 근거. 실패하면 None."""
    try:
        res = get_naver_session().get(f"https://m.stock.naver.com/api/stock/{code}/finance/annual", timeout=5)
        return res.json() if res.status_code == 200 else None
    except Exception:
        return None

def _rep_scale(values, currency):
    """차트 축 단위. 원화는 최대값에 맞춰 조원/억원, 외화는 백만."""
    peak = max((abs(v) for v in values if v is not None), default=0)
    if currency != "KRW":
        return 1e6, f"백만 {currency}"
    return (1e12, "조원") if peak >= 1e12 else (1e8, "억원")

def _rep_annual_figure(rows, currency):
    years = [str(r["year"]) for r in rows]
    div, unit = _rep_scale([r["revenue"] for r in rows] + [r["op"] for r in rows], currency)
    scaled = lambda k: [None if r[k] is None else r[k] / div for r in rows]
    fig = go.Figure()
    fig.add_bar(x=years, y=scaled("revenue"), name="매출액", marker_color="#9ca3af")
    fig.add_bar(x=years, y=scaled("op"), name="영업이익", marker_color="#d60000")
    fig.add_scatter(x=years, y=[r["op_margin"] for r in rows], name="영업이익률(%)", yaxis="y2", mode="lines+markers", line=dict(color="#1857e0"))
    fig.update_layout(barmode="group", height=320, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.15),
                      yaxis=dict(title=unit), yaxis2=dict(title="%", overlaying="y", side="right", showgrid=False), hovermode="x unified")
    return fig

def _rep_cash_figure(rows, currency):
    years = [str(r["year"]) for r in rows]
    div, unit = _rep_scale([r["ocf"] for r in rows] + [r["capex"] for r in rows], currency)
    scaled = lambda k: [None if r[k] is None else r[k] / div for r in rows]
    fig = go.Figure()
    fig.add_bar(x=years, y=scaled("ocf"), name="영업활동현금흐름", marker_color="#9ca3af")
    fig.add_bar(x=years, y=scaled("capex"), name="CAPEX(유형+무형 취득)", marker_color="#64748b")
    fig.add_scatter(x=years, y=scaled("fcf"), name="FCF", mode="lines+markers", line=dict(color="#d60000"))
    fig.update_layout(barmode="group", height=300, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.15), yaxis=dict(title=unit), hovermode="x unified")
    return fig

def _rep_annual_dataframe(rep):
    cur, rows = rep["currency"], rep["annual"]["rows"]
    ni_label = "순이익(지배주주)" if rep["annual"]["ni_basis"] == "지배주주" else "순이익(전체)"
    spec = [("매출액", "revenue", "amt"), ("영업이익", "op", "amt"), ("영업이익률", "op_margin", "pct"), (ni_label, "ni", "amt"),
            ("자산총계", "assets", "amt"), ("부채총계", "liab", "amt"), ("자본총계", "equity", "amt"), ("부채비율", "debt_ratio", "pct0"), ("ROE", "roe", "pct"),
            ("영업활동현금흐름", "ocf", "amt"), ("CAPEX", "capex", "amt"), ("FCF", "fcf", "amt")]
    fmt = {"amt": lambda v: crep.fmt_amount(v, cur), "pct": crep.fmt_pct, "pct0": lambda v: crep.fmt_pct(v, 0)}
    data = {str(r["year"]): [fmt[kind](r[key]) for _, key, kind in spec] for r in rows}
    return pd.DataFrame(data, index=[label for label, _, _ in spec])

def render_report_header(rep):
    h, cur = rep["header"], rep["currency"]
    ttm = h["ttm"]
    c1, c2, c3 = st.columns(3)
    c1.metric("종가", f"{h['price']:,.0f}원" if h["price"] else "—", help=f"{h['price_date']:%Y-%m-%d} 종가(진단 화면과 같은 시세)" if h["price_date"] else None)
    c2.metric("시가총액", crep.fmt_won(h["market_cap"]),
              help=(f"종가 × 보통주 발행주식수 {h['shares_common']:,.0f}주({h['shares_date']} 기준, 자기주식 포함)" if h["shares_common"] else "주식수를 읽지 못했습니다."))
    per_txt = "적자" if h["per_status"] == "loss" else crep.fmt_mult(h["per"])
    c3.metric("PER (후행)", per_txt, help=f"시가총액 ÷ 후행 순이익. 이익 기준: {ttm['label']}. 최근 사업연도 기준 PER은 {crep.fmt_mult(h['per_fy'])}")
    c4, c5, c6 = st.columns(3)
    pbr_txt = "자본잠식" if h["pbr_status"] == "neg" else crep.fmt_mult(h["pbr"], 2)
    c4.metric("PBR", pbr_txt, help=f"시가총액 ÷ 자본({h['equity_label']}, {crep.fmt_amount(h['equity'], cur)})")
    c5.metric("PSR (후행)", crep.fmt_mult(h["psr"], 2), help="시가총액 ÷ 후행 매출")
    c6.metric("영업이익률 (후행)", crep.fmt_pct(h["op_margin_ttm"]), help="후행 영업이익 ÷ 후행 매출")
    st.caption(f"후행 지표 기준: {ttm['label']} · 후행 매출 {crep.fmt_amount(ttm['revenue'], cur)} · 영업이익 {crep.fmt_amount(ttm['op'], cur)} · 순이익 {crep.fmt_amount(ttm['ni'], cur)}")
    if h.get("pref_note"):
        st.caption("※ " + h["pref_note"])

def render_report_holders(rep):
    ho, tr = rep["holders"], rep["treasury"]
    left, right = st.columns(2)
    with left:
        st.markdown("##### 최대주주·특수관계인")
        if not ho or not ho["major"]:
            st.caption("DART에서 최대주주 현황을 받지 못했습니다.")
        else:
            m = ho["major"]
            st.markdown(f"**{m['name']}** {crep.fmt_pct(m['ratio'], 2)} · 특수관계인 합계 **{crep.fmt_pct(ho['group_ratio'], 2)}**")
            st.caption(f"{ho['period']}(결산일 {ho['date']}) 기준 보통주")
            df = pd.DataFrame([{"이름": p["name"], "관계": p["relate"], "지분율(%)": p["ratio"],
                                "기초 대비(%p)": None if p["ratio"] is None or p["prev_ratio"] is None else round(p["ratio"] - p["prev_ratio"], 2)}
                               for p in ho["people"][:10]])
            st.dataframe(df, hide_index=True, width="stretch",
                         column_config={"지분율(%)": st.column_config.NumberColumn(format="%.2f"), "기초 대비(%p)": st.column_config.NumberColumn(format="%+.2f")})
    with right:
        st.markdown("##### 자기주식")
        if not tr or tr["end"] is None:
            st.caption("DART에서 자기주식 현황을 받지 못했습니다.")
        else:
            a, b = st.columns(2)
            a.metric("보유 자기주식", f"{tr['end']:,.0f}주", help="자기주식은 의결권이 없고 시가총액 계산에는 포함돼 있습니다.")
            b.metric("발행주식 대비", crep.fmt_pct(tr["ratio"], 2))
            if tr["acquired"] is not None:
                st.caption(f"{tr['period']} 기준 기간 중 취득 {tr['acquired']:,.0f}주 · 처분 {tr['disposed']:,.0f}주 · 소각 {tr['cancelled']:,.0f}주 (기초 {tr['begin']:,.0f}주)")
            else:
                st.caption(f"{tr.get('period') or '최신 보고서'} 기준 보유 수량만 확인했습니다(기간 중 취득·처분·소각 내역은 받지 못함).")

def render_report_filings(rep, code, key_prefix):
    st.markdown("##### 최근 공시 (90일)")
    noise = st.checkbox(f"{crep.NOISE_TAG} 포함", value=False, key=f"{key_prefix}_rep_noise_{code}",
                        help="임원·주요주주가 주식을 사고팔 때마다 올라오는 보고서입니다. 양이 많아 기본은 숨깁니다.")
    shown = [f for f in rep["filings"] if noise or f["tag"] != crep.NOISE_TAG]
    if not shown:
        st.caption("최근 90일 공시가 없습니다.")
        return
    df = pd.DataFrame([{"날짜": f["date"].isoformat(), "구분": ("🔴 " if f["risk"] == "경고" else "🟠 " if f["risk"] else "") + f["tag"],
                        "제목": f["title"], "원문": f["url"]} for f in shown])
    st.dataframe(df, hide_index=True, width="stretch", column_config={"원문": st.column_config.LinkColumn("DART", display_text="원문 보기")})
    st.caption("🔴·🟠 = 위 '위험 공시' 경고에 해당하는 공시(과거 통계 근거가 있는 유형만). 나머지 구분은 제목 키워드로 붙인 분류라 틀릴 수 있습니다.")

def _mix_figure(members):
    labels = [m["label"][:26] + ("…" if len(m["label"]) > 26 else "") for m in members]
    shares = [m["share"] for m in members]
    colors = ["#cbd5e1" if m.get("derived") else "#1857e0" for m in members]
    fig = go.Figure(go.Bar(x=shares, y=labels, orientation="h", marker_color=colors, text=[f"{v:.1f}%" if v is not None else "" for v in shares], textposition="outside"))
    fig.update_layout(height=max(220, 34 * len(members) + 60), margin=dict(l=10, r=40, t=10, b=10), yaxis=dict(autorange="reversed"),
                      xaxis=dict(title="비중(%)", range=[0, max([v for v in shares if v is not None] + [10]) * 1.18]), showlegend=False)
    return fig

def render_report_mix(rep, code, key_prefix):
    st.markdown("##### 매출 구성")
    mix = rep.get("mix")
    if not mix or not mix.get("views"):
        st.caption("사업보고서에서 사업부문·제품·지역별 매출 구성을 읽지 못했습니다(주석을 태그하지 않았거나 표 서식을 해석하지 못한 경우). 금융업처럼 매출 구분이 없는 업종도 해당합니다.")
        return
    link = crep.DART_VIEW + mix.get("rcept_no", "")
    views, cur = mix["views"], rep["currency"]
    titles = [v["title"] for v in views]
    pick = st.radio("보기", titles, horizontal=True, key=f"{key_prefix}_mix_view_{code}") if len(titles) > 1 else titles[0]
    v = next(x for x in views if x["title"] == pick)
    end = mix.get("period_end")
    st.caption(f"{pick} · {end[:4] + '년' if end else '최근 사업연도'} · 출처: {v['source']} · [DART 원문]({link})")
    if v["note"]:
        st.caption("※ " + v["note"])
    members = smix.compact_members(v["members"])
    st.plotly_chart(_mix_figure(members), theme=None, key=f"{key_prefix}_mix_chart_{code}")
    rows = []
    for m in members:
        row = {"구성": m["label"], "매출": crep.fmt_amount(m["cur"], cur), "비중(%)": m["share"]}
        if v["has_prev"]:
            row["전기 매출"] = crep.fmt_amount(m.get("prev"), cur)
            row["증감률(%)"] = (m["cur"] / m["prev"] - 1) * 100 if m.get("prev") else None
        if v["has_op"]:
            row["영업이익"] = crep.fmt_amount(m.get("op_cur"), cur) if m.get("op_cur") is not None else "—"
            row["영업이익률(%)"] = m["op_cur"] / m["cur"] * 100 if m.get("op_cur") is not None and m["cur"] else None
        rows.append(row)
    cfg = {"비중(%)": st.column_config.NumberColumn(format="%.1f"), "증감률(%)": st.column_config.NumberColumn(format="%+.1f"),
           "영업이익률(%)": st.column_config.NumberColumn(format="%.1f")}
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={k: c for k, c in cfg.items() if k in rows[0]})
    st.caption("※ 회사가 공시한 구분을 그대로 옮긴 값입니다(구분 기준은 회사마다 다르고, 같은 이름이라도 해마다 범위가 바뀔 수 있습니다). 연결 매출과 합계가 맞지 않는 보기는 위 안내를 참고하세요.")

@st.cache_data(ttl=21600, show_spinner=False)
def fetch_industry_info(code):
    """네이버 업종(industryCode)과 그 업종 전체 종목·시가총액. 실패하면 None."""
    try:
        session = get_naver_session()
        integ = session.get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=5).json()
        ind_code = integ.get("industryCode")
        if not ind_code:
            return None
        merged = None
        for page in (1, 2, 3):
            res = session.get(f"https://m.stock.naver.com/api/stocks/industry/{ind_code}", params={"page": page, "pageSize": 100}, timeout=8)
            if res.status_code != 200:
                break
            data = res.json()
            parsed = pcmp.parse_industry(data)
            merged = parsed if merged is None else {**merged, "stocks": merged["stocks"] + parsed["stocks"]}
            if page * 100 >= (data.get("totalCount") or 0):
                break
        return merged
    except Exception:
        return None

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_peer_metrics(code, today_str):
    """비교 종목 하나의 지표(보고서와 같은 build_report로 계산). 실패는 {'code', 'error'}로 돌려 캐시한다."""
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
        raw = crep.fetch_raw(code, today, light=True)
        df = fetch_diag_ohlcv(code, 30)
        rep = crep.build_report(raw, float(df["Close"].iloc[-1]), df.index[-1].date(), None)
        return pcmp.metrics_from_report(rep)
    except crep.NoDartData as e:
        return {"code": code, "error": str(e)[:60]}
    except Exception as e:
        return {"code": code, "error": f"{type(e).__name__}: {str(e)[:60]}"}

def render_report_peers(rep, code, name, key_prefix):
    """피어 비교 표. 반환: 피어 PER 중앙값 요약(시뮬레이터가 같은 값을 쓴다) 또는 None."""
    from concurrent.futures import ThreadPoolExecutor
    st.markdown("##### 피어 비교 (같은 업종·비슷한 시가총액)")
    industry = fetch_industry_info(code)
    override = st.text_input("비교 종목 직접 지정(쉼표로 구분, 최대 8개)", key=f"{key_prefix}_peer_override_{code}", placeholder="예: 한미반도체, 000660 — 비우면 자동 선정",
                             help="자동 선정은 네이버 업종 분류라 사업 구조가 다른 종목이 섞일 수 있습니다. 직접 정하면 그 종목들과 비교합니다.")
    codes, failed_names = [], []
    if override.strip():
        for token in [t.strip() for t in override.split(",") if t.strip()][:pcmp.MAX_PEERS]:
            c, _, _ = resolve_diag_stock(token)
            if c and c != code:
                codes.append(c)
            elif not c:
                failed_names.append(token)
        basis = "직접 지정"
    elif industry:
        subject_cap = rep["header"]["market_cap"] or next((x["market_cap"] for x in industry["stocks"] if x["code"] == code), None)
        codes = pcmp.pick_peers(industry, code, subject_cap, set(dart_core.corp_codes()))
        basis = f"네이버 업종 '{industry['name']}'에서 시가총액이 가장 비슷한 {len(codes)}개"
    else:
        basis = ""
    if failed_names:
        st.caption(f"찾지 못한 종목: {', '.join(failed_names)}")
    if not codes:
        st.caption("업종 분류를 받지 못했거나 비교할 종목이 없어 피어 비교를 건너뜁니다. 위 입력칸에 비교 종목을 직접 적어 볼 수 있습니다.")
        return None
    today_str = now_kst().date().strftime("%Y-%m-%d")
    with st.spinner(f"비교 종목 {len(codes)}곳의 재무를 DART에서 계산하는 중입니다(처음 한 번은 20초 안팎, 이후엔 캐시)..."):
        with ThreadPoolExecutor(6) as ex:
            results = list(ex.map(lambda c: fetch_peer_metrics(c, today_str), codes))
    peers = [m for m in results if "error" not in m]
    bad = [f"{m['code']}({m['error']})" for m in results if "error" in m]
    subject = pcmp.metrics_from_report(rep)
    subject["name"] = name
    summ = pcmp.summarize(subject, peers)
    table = []
    for m, mark in [(subject, "◀ 이 종목")] + [(p, "") for p in sorted(peers, key=lambda x: -(x["market_cap"] or 0))]:
        table.append({"종목": f"{m['name']} {mark}".strip(), "시가총액": crep.fmt_won(m["market_cap"]) if m["currency"] == "KRW" else "—",
                      "PER(후행)": m["per"], "PBR": m["pbr"], "PSR(후행)": m["psr"], "영업이익률(%)": m["op_margin"], "ROE(%)": m["roe"],
                      "매출 증가율(%)": m["rev_growth"], "부채비율(%)": m["debt_ratio"]})
    med = summ["median"]
    table.append({"종목": f"피어 중앙값(n={summ['n']})", "시가총액": "—", "PER(후행)": med["per"], "PBR": med["pbr"], "PSR(후행)": med["psr"], "영업이익률(%)": med["op_margin"],
                  "ROE(%)": med["roe"], "매출 증가율(%)": med["rev_growth"], "부채비율(%)": med["debt_ratio"]})
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch",
                 column_config={"PER(후행)": st.column_config.NumberColumn(format="%.1f"), "PBR": st.column_config.NumberColumn(format="%.2f"),
                                "PSR(후행)": st.column_config.NumberColumn(format="%.2f"), "영업이익률(%)": st.column_config.NumberColumn(format="%.1f"),
                                "ROE(%)": st.column_config.NumberColumn(format="%.1f"), "매출 증가율(%)": st.column_config.NumberColumn(format="%+.1f"),
                                "부채비율(%)": st.column_config.NumberColumn(format="%.0f")})
    gaps = []
    for key, label, kind in (("per", "PER", "pct"), ("pbr", "PBR", "pct"), ("op_margin", "영업이익률", "pp"), ("roe", "ROE", "pp"), ("rev_growth", "매출 증가율", "pp")):
        g = summ["gap"].get(key)
        if g is not None:
            gaps.append(f"{label} {g:+.0f}%" if kind == "pct" else f"{label} {g:+.1f}%p")
    if gaps:
        st.caption("피어 중앙값 대비 이 종목: " + " · ".join(gaps) + " (배수는 중앙값 대비 %, 비율 지표는 %p 차이)")
    st.caption(f"비교 기준: {basis}. 이 종목과 비교 종목 모두 같은 계산(후행 12개월 순이익 ÷ 종가 기준 시가총액 등)이라 위 헤더와 같은 정의입니다. "
               "중앙값은 값이 있는 비교 종목이 3개 이상일 때만 냅니다(적자·자본잠식 종목의 PER·PBR은 제외, 영업이익률이 ±100%를 넘는 지주사 등은 영업이익률만 제외). 업종 분류는 대분류라 사업 구조가 다른 종목이 섞일 수 있습니다."
               + (f" 불러오지 못한 종목: {', '.join(bad)}." if bad else "")
               + (f" 외화 보고 회사는 중앙값에서 제외: {', '.join(summ['excluded'])}." if summ["excluded"] else ""))
    return {"per_median": med["per"], "n": summ["n"]}

def render_report_simulator(rep, code, key_prefix, peer_summary=None):
    h, v = rep["header"], rep["valuation"]
    st.markdown("##### 밸류에이션 시뮬레이터 (순이익 × PER)")
    if rep["currency"] != "KRW" or not h["market_cap_all"] or not v["ni_base"] or v["ni_base"] <= 0:
        st.caption("순이익이 0 이하이거나 시가총액을 계산하지 못해(또는 외화 보고 회사라) 시뮬레이터를 제공하지 않습니다.")
        return
    cap, price = h["market_cap_all"], h["price"]
    last_ni = rep["annual"]["rows"][-1]["ni"]
    options = {f"후행 12개월 순이익 ({crep.fmt_won(v['ni_base'])})": v["ni_base"]} if h["ttm"]["is_ttm"] else {}
    if last_ni and last_ni > 0:
        options[f"{rep['annual_year']} 사업연도 순이익 ({crep.fmt_won(last_ni)})"] = last_ni
    if not options:
        options[f"순이익 ({crep.fmt_won(v['ni_base'])})"] = v["ni_base"]
    c1, c2 = st.columns(2)
    base_label = c1.radio("이익 기준", list(options), key=f"{key_prefix}_rep_base_{code}") if len(options) > 1 else next(iter(options))
    if len(options) == 1:
        c1.markdown(f"**이익 기준** · {base_label}")
    growth = c2.slider("순이익 증감 가정(%)", -50, 100, 0, 5, key=f"{key_prefix}_rep_growth_{code}",
                       help="기준 순이익을 얼마나 늘리거나 줄여 볼지. 전망이 아니라 '이만큼 달라지면 주가가 어떻게 되나'를 보는 가정값입니다.")
    ni = options[base_label] * (1 + growth / 100)

    rng = v["range"]
    per_now = v["per_now"]
    anchor = per_now or (rng["mid"] if rng else 10.0)
    per_cap = float(max(40, int(max(anchor, rng["high"] if rng else 0) * 1.5 // 5 + 1) * 5))
    per = st.slider("적용 PER(배)", 1.0, per_cap, float(min(max(round(anchor * 2) / 2, 1.0), per_cap)), 0.5, key=f"{key_prefix}_rep_per_{code}")
    sim = crep.scenario(ni, cap, price, per)
    m1, m2, m3 = st.columns(3)
    m1.metric("목표 시가총액", crep.fmt_won(sim["cap"]), help="이익 × PER")
    m2.metric("목표 주가", f"{sim['price']:,.0f}원", f"{sim['upside'] * 100:+.1f}%", delta_color="inverse",
              help="현재가 × (목표 시총 ÷ 현재 시총). 우선주가 있어도 현재가 대비 비율이 같게 나옵니다.")
    m3.metric("가정 순이익", crep.fmt_won(ni), help=f"{base_label} × (1 {growth:+d}%)")

    st.markdown("**PER 기준점** (같은 이익 가정으로 계산)")
    rows = []
    if per_now:
        s = crep.scenario(ni, cap, price, per_now)
        rows.append({"기준": "현재 후행 PER", "PER(배)": per_now, "목표 주가": s["price"], "현재가 대비(%)": s["upside"] * 100, "근거": f"종가 ÷ 후행 순이익({h['ttm']['label']})"})
    if rng:
        yrs = f"{rng['years'][0]}~{rng['years'][-1]}"
        for lab, key in (("Bear · 과거 최저", "low"), ("Base · 과거 중앙", "mid"), ("Bull · 과거 최고", "high")):
            s = crep.scenario(ni, cap, price, rng[key])
            rows.append({"기준": lab, "PER(배)": rng[key], "목표 주가": s["price"], "현재가 대비(%)": s["upside"] * 100,
                         "근거": f"이 종목의 {yrs} 사업연도 말 PER {rng['n']}개 중 {'최저' if key == 'low' else '중앙' if key == 'mid' else '최고'}(네이버)"})
    if peer_summary and peer_summary.get("per_median"):         # 위 '피어 비교' 표의 중앙값과 같은 숫자(출처·계산이 같다)
        s = crep.scenario(ni, cap, price, peer_summary["per_median"])
        rows.append({"기준": "피어 PER 중앙값", "PER(배)": peer_summary["per_median"], "목표 주가": s["price"], "현재가 대비(%)": s["upside"] * 100,
                     "근거": f"위 '피어 비교' 표 {peer_summary['n']}개 종목의 후행 PER 중앙값(적자 제외)"})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"PER(배)": st.column_config.NumberColumn(format="%.1f"), "목표 주가": st.column_config.NumberColumn(format="%d"),
                                    "현재가 대비(%)": st.column_config.NumberColumn(format="%+.1f")})
    else:
        st.caption("이 종목의 과거 PER(최소 2개 사업연도)과 피어 PER 중앙값을 모두 얻지 못해 기준점을 만들지 못했습니다. 위 슬라이더로 직접 가정해 보세요.")
    st.caption("※ Bear·Base·Bull은 '낮은·중간·높은 배수'라는 뜻이지 주가 하락·상승 예측이 아닙니다. 후행 이익이 과거보다 크게 늘었다면 Bear 배수로도 현재가보다 높게 나옵니다. "
               "순이익 × PER을 곱한 단순 산식이라 예측도 기업가치 평가도 아닙니다. 과거 PER은 사업연도 말 주가 기준 3개 연도뿐이고, 이익이 급변한 종목(후행 이익이 사업연도의 2배 이상 등)은 "
               "배수 기준점이 현재 이익과 맞지 않을 수 있습니다. PER 기준점에 쓴 네이버 값은 DART 순이익과 출처가 달라 소수점 단위로 어긋날 수 있습니다.")

def _md_safe(s):
    """공시 원문·AI 문장을 st.markdown에 넣기 전 마크다운 기호를 무력화한다: $ 쌍은 수식, ~ 쌍은 취소선, [..](..)는 클릭 가능한 링크(원문·모델 출력에 섞이면 안 됨)가 된다.
    공백은 한 칸으로 줄여 줄바꿈이 목록·제목으로 바뀌지 않게 한다."""
    s = re.sub(r"\s+", " ", str(s))
    for ch in ("\\", "$", "~", "[", "]", "`", "*", "_", "<", ">"):
        s = s.replace(ch, "\\" + ch)
    return s

def render_narrative_result(res):
    """검증을 통과한 AI 서술 + 근거(F·Q) + 탈락 문장. 인용은 코드가 원문 발췌와 대조한 것만 나온다."""
    label = {q["id"]: q["label"] for q in res.get("excerpts", [])}
    url = crep.DART_VIEW + (res.get("rcept_no") or "")
    for key in ("business", "changes", "risks", "questions"):
        items = res.get(key) or []
        if not items and key != "risks":
            continue
        st.markdown(f"**{rnar.SECTION_NAMES[key]}**")
        if not items:
            st.caption("검증을 통과한 리스크 문장이 없습니다(근거가 약하거나 숫자·인용 검증에서 모두 탈락). 아래 '검증에서 탈락한 문장'과 공시 원문을 직접 확인해 주세요.")
        for it in items:
            if key == "risks":
                st.markdown(f"- **{_md_safe(it['title'])}** — {_md_safe(it['text'])}" if it.get("title") else f"- {_md_safe(it['text'])}")
                if it.get("quote"):
                    st.markdown(f"> “{_md_safe(it['quote'])}” — 사업보고서 · {label.get(it.get('quote_id'), '')} · [DART 원문]({url})")
            else:
                st.markdown(f"- {_md_safe(it['text'])}")
            st.caption("근거 " + " · ".join(it["refs"]))
    st.caption(f"생성 {res['generated_at']} · {res['model']} · 검증 통과 {res['kept']}/{res['asked']}문장"
               + (f" · 앞선 시도 실패: {', '.join(res['llm_errors'])}" if res.get("llm_errors") else ""))
    if res.get("dropped"):
        with st.expander(f"검증에서 탈락한 문장 ({len(res['dropped'])})"):
            for d in res["dropped"]:
                st.markdown(f"- [{rnar.SECTION_NAMES.get(d['section'], d['section'])}] {_md_safe(d['text'])} — **{_md_safe(d['reason'])}**")
    with st.expander("근거 목록 (사실 F · 공시 원문 Q)"):
        st.markdown("**사실 F** — 위 보고서 숫자를 코드가 문장으로 옮긴 것(종가에 따라 변하는 시총·PER은 넣지 않음)")
        for f in res.get("facts", []):
            st.markdown(f"- **{f['id']}** {_md_safe(f['text'])}")
        st.markdown(f"**공시 원문 발췌 Q** — [사업보고서 원문]({url})에서 뽑은 부분(글자 예산 안에서 위험 단서가 있는 문단 위주)")
        for q in res.get("excerpts", []):
            st.markdown(f"- **{q['id']}** ({q['label']}) {_md_safe(q['text'])}")

def render_report_narrative(rep, code, key_prefix):
    st.markdown("##### 🤖 AI 서술 — 사업 요약 · 최근 변화 · 비판적 리스크")
    st.caption("사업보고서 본문과 위 숫자만 재료로 Groq가 쓰고, 코드가 검증합니다. 검증은 ① 문장 속 숫자가 근거에 있는지 ② 인용문이 원문에 그대로 있는지 "
               "③ 전망·목표가·매수/매도 표현이 없는지만 확인합니다. 해석이 맞는지까지 보장하지는 않으니 인용 원문을 직접 확인하세요. 자동 실행하지 않고 버튼을 눌렀을 때만 생성합니다.")
    if not rep.get("rcept_no"):
        st.info("사업보고서 접수번호를 찾지 못해 AI 서술을 만들 수 없습니다.")
        return
    state_key = f"{key_prefix}_narr_{code}_{rep['rcept_no']}"
    res = st.session_state.get(state_key) or rnar.load_cached(rep)       # 같은 보고서·같은 숫자면 이전 결과를 바로 보여준다(디스크 캐시)
    if st.button("다시 생성" if res else "AI 서술 생성", key=f"{key_prefix}_narr_btn_{code}",
                 help="처음엔 DART 본문 내려받기 포함 20초 안팎. Groq 무료 한도(분당 8,000토큰) 때문에 연달아 누르면 최대 1분 더 기다릴 수 있습니다."):
        try:
            api_key = str(st.secrets["GROQ_API_KEY"])
        except Exception:
            api_key = None
        if not GENAI_AVAILABLE or not api_key:
            st.info("💡 Groq 패키지 또는 GROQ_API_KEY(Secrets)가 없어 AI 서술을 만들 수 없습니다.")
        else:
            with st.spinner("사업보고서 본문을 읽고 서술을 만드는 중입니다(최대 1분)..."):
                try:
                    sections = ftext.fetch_sections(rep["rcept_no"])
                    new = rnar.generate(rep, sections, rnar.groq_llm(api_key))
                except (rnar.NarrativeError, dart_core.DartError) as e:
                    st.warning(f"AI 서술을 만들지 못했습니다: {e}")
                except Exception as e:
                    st.warning(f"AI 서술을 만들지 못했습니다({type(e).__name__}).")
                else:
                    rnar.save_cached(rep, new)
                    st.session_state[state_key] = res = new
                    try:
                        st.rerun(scope="fragment")      # 버튼 라벨('다시 생성')을 새 상태로 다시 그린다. 조각 재실행 중에만 허용된다
                    except StreamlitAPIException:       # 전체 재실행 중에 불린 경우 — 지금 상태 그대로 그린다(RerunException은 BaseException이라 여기서 잡히지 않는다)
                        pass
    if res:
        render_narrative_result(res)

def render_dart_probe(key):
    """DART 접속이 안 될 때, 이 서버에서 어느 단계(이름 확인·포트·HTTPS)가 막히는지 보여준다. 키는 쓰지 않는다."""
    if not st.button("DART 접속 점검", key=key, help="이 서버에서 DART까지 접속이 되는지 단계별로 확인합니다(키는 쓰지 않습니다)."):
        return
    with st.spinner("접속을 단계별로 시험하는 중입니다(최대 30초)..."):
        rows = dart_core.probe_connection()
    st.dataframe(
        pd.DataFrame({"단계": [r["step"] for r in rows], "결과": ["성공" if r["ok"] else "실패" for r in rows],
                      "소요(초)": [r["sec"] for r in rows], "상세": [r["detail"] for r in rows]}),
        hide_index=True, width="stretch")
    dart = [r for r in rows if not r["step"].startswith("대조군")]
    control = next((r for r in rows if r["step"].startswith("대조군")), None)
    if all(r["ok"] for r in dart):
        st.caption("DART까지 접속은 됩니다. 일시적인 지연이었을 수 있으니 잠시 뒤 다시 시도해 주세요.")
    elif control and control["ok"]:
        st.caption("국내 사이트(네이버)는 되는데 DART만 막혀 있습니다. 이 서버(해외)에서의 DART 접속이 제한된 것으로 보입니다.")
    else:
        st.caption("대조군도 실패했습니다. 이 서버의 네트워크 문제일 수 있으니 잠시 뒤 다시 시도해 주세요.")

@st.fragment
def render_company_report(code, name, price, price_date, key_prefix):
    """종목 분석 보고서. 슬라이더를 움직여도 이 조각만 다시 그린다(진단 화면 전체를 다시 계산하지 않음)."""
    today = now_kst().date()
    with st.spinner("DART에서 재무·주주·공시를 불러오는 중입니다(처음 한 번은 10초 안팎, 이후엔 캐시)..."):
        status, payload = fetch_company_report_raw(code, today.strftime("%Y-%m-%d"))
        if status == "ok":
            naver = fetch_naver_annual(code)
            r_status, flags = fetch_risk_flags(code, today.strftime("%Y-%m-%d"))
            risk_levels = {f["rno"]: f["level"] for f in flags} if r_status == "ok" else {}
            rep = crep.build_report(payload, price, price_date, naver, risk_levels)
    if status == "nodata":
        st.info(payload)
        return
    if status == "err":
        if dart_unreachable(payload) or "ConnectionError" in payload:
            st.warning(f"종목 분석 보고서를 만들 수 없습니다. {DART_UNREACHABLE}")
            render_dart_probe(f"{key_prefix}_dartprobe_{code}")
        else:
            st.warning(f"DART에서 보고서 자료를 받지 못했습니다({payload}). Secrets에 DART_API_KEY가 있는지, 일일 요청 한도를 넘지 않았는지 확인해 주세요.")
        return

    interim = f" · {rep['interim']['year']} {rep['interim']['label']}보고서" if rep["interim"] else ""
    st.caption(f"{rep['name']} · {rep['fs']}재무제표 · {rep['annual_year']} 사업보고서{interim}까지 반영 · 모든 숫자는 DART 공시와 종가로 코드가 계산합니다(AI 추정·전망 없음).")
    if rep["currency"] != "KRW":
        st.warning(f"이 회사는 재무제표가 {rep['currency']} 단위입니다. 아래 금액은 원화가 아닙니다.")
    render_report_header(rep)

    st.markdown("##### 5개년 실적")
    st.plotly_chart(_rep_annual_figure(rep["annual"]["rows"], rep["currency"]), theme=None, key=f"{key_prefix}_rep_chart_annual_{code}")
    st.dataframe(_rep_annual_dataframe(rep), width="stretch")
    if not rep["financial_like"] and any(r["fcf"] is not None for r in rep["annual"]["rows"]):
        st.markdown("##### 현금흐름")
        st.plotly_chart(_rep_cash_figure(rep["annual"]["rows"], rep["currency"]), theme=None, key=f"{key_prefix}_rep_chart_cash_{code}")
        st.caption("FCF = 영업활동현금흐름 − (유형자산 취득 + 무형자산 취득). 리스·임대 자산 투자와 사업 인수는 반영하지 않습니다.")
    render_report_mix(rep, code, key_prefix)
    render_report_holders(rep)
    render_report_filings(rep, code, key_prefix)
    peer_summary = render_report_peers(rep, code, name, key_prefix)
    render_report_simulator(rep, code, key_prefix, peer_summary)
    render_report_narrative(rep, code, key_prefix)

    with st.expander("계산 기준과 데이터 한계"):
        st.markdown(
            "- **후행 12개월** = 직전 사업연도 + 올해 누적 − 전년 동기 누적(올해 분기·반기 보고서가 아직 없으면 직전 사업연도).\n"
            "- **순이익·ROE** = 지배기업 소유주 몫(지배·비지배 구분이 없으면 당기순이익 전체). ROE는 평균 자본 기준, 부채비율 = 부채총계 ÷ 자본총계.\n"
            "- **시가총액** = 종가 × 최신 보고서의 보통주 발행주식수. PER·PBR·PSR은 이 시가총액을 쓰고, 우선주가 있으면 보통주 가격으로 환산해 합친 값을 씁니다.\n"
            "- 같은 지표를 이 화면의 다른 곳에서 다시 계산하지 않습니다. 네이버·증권사 화면의 PER과 다른 것은 이익 기준(후행 12개월 vs 전년 확정 vs 추정)이 달라서입니다.\n"
            "- 분기·반기 보고서는 감사 전 숫자이고, 정정 공시가 나오면 값이 바뀔 수 있습니다. 결산월이 12월이 아닌 회사는 '사업연도' 표기가 회계연도 기준입니다.")
        if rep["notes"]:
            st.markdown("**이 종목에서 비어 있거나 대체된 항목**")
            for n in rep["notes"]:
                st.markdown(f"- {n}")

def render_diagnosis(diag, name, mode, key_prefix="diag", risk_asof=None):
    """진단 결과 한 종목의 상세 화면. 같은 화면에 두 번 그려질 수 있어(종목 진단 탭 / 관심종목 탭) 위젯 키에 key_prefix를 쓴다.
    risk_asof: 과거 날짜 진단일 때 그 날짜(위험 공시를 그 날짜까지만 본다). None이면 오늘."""
    close, prev_close = diag.df['close'].iloc[-1], diag.df['close'].iloc[-2]
    st.markdown(f"### {name} ({diag.code}) · {diag.as_of:%Y-%m-%d} 종가 {close:,.0f}원 ({(close / prev_close - 1) * 100:+.2f}%)")
    render_risk_flags(diag.code, risk_asof)
    # 보고서 탭은 열었을 때만 DART를 부른다(on_change="rerun" + .open). 매매 진단은 항상 그려 위젯 상태가 탭을 오가도 유지된다.
    tab_signal, tab_report = st.tabs(["📈 매매 진단", "📑 종목 분석 보고서"], key=f"{key_prefix}_detail_tabs", on_change="rerun")
    with tab_signal:
        render_diagnosis_signals(diag, name, mode, key_prefix)
    with tab_report:
        if risk_asof is not None:
            st.info("재무·주주·공시는 현재 시점 자료라 과거 날짜 진단에서는 보고서를 보여주지 않습니다. 진단 시점을 '현재'로 바꿔 주세요.")
        elif tab_report.open:
            render_company_report(diag.code, name, float(close), diag.as_of, key_prefix)

def render_diagnosis_signals(diag, name, mode, key_prefix):
    """매매 진단 본문(등급·체크리스트·차트·재료). 종목 헤더와 위험 공시 경고는 render_diagnosis가 위에 그린다."""
    res = diag.results[mode]

    with st.container(border=True):
        grade_col, text_col = st.columns([1, 4])
        big = res.grade or res.score_text
        sub = f"{mode} 진단" + (f" · 핵심 통과 {res.score_text}" if res.grade else "")   # 매도는 큰 글씨가 이미 '약세 신호 n/m'
        grade_col.markdown(f"<div style='font-size:3rem; font-weight:800; line-height:1.1;'>{big}</div>"
                           f"<div style='color:gray; font-size:0.8rem;'>{sub}</div>",
                           unsafe_allow_html=True)
        text_col.markdown(f"#### :{DIAG_VERDICT_COLORS.get(res.verdict, 'gray')}[{res.verdict}]")
        text_col.markdown(f"**{res.action}**")

    if mode == "보유" and res.extra.get("pnl") is not None:
        m1, m2, m3 = st.columns(3)
        m1.metric("평단 대비", f"{res.extra['pnl'] * 100:+.1f}%")
        m2.metric("평단가", f"{res.extra['buy_price']:,.0f}원")
        m3.metric("손절선(평단 -8%)", f"{res.extra['stop_line']:,.0f}원")
    elif mode == "매수":
        if diag.risk:
            st.caption(f"참고 손절 {diag.risk['stop']:,.0f}원 · 목표 {diag.risk['target']:,.0f}원 (현재가 -2×ATR / +3×ATR, 변동성 기반 근사치)")
        st.caption("※ 등급 A·B = 매수 고려, C = 지켜보기, D = 사지 않기. 종가가 240일선 아래면 점수와 무관하게 C 이하로 제한됩니다.")

    st.markdown("##### 핵심 체크리스트 (점수 반영)")
    for c in (c for c in res.checks if c.scored):
        render_check_line(c)
    if diag.flow_net is None:
        st.caption("※ 수급은 네이버가 최근 5거래일만 제공해, 조회에 실패했거나 과거 시점을 진단할 때는 평가에서 빠지고 등급은 나머지 항목의 비율로 계산됩니다.")

    st.markdown("##### 참고 신호 (점수 미반영)")
    st.caption("10일선·20일선·정배열·패턴은 과거 검증에서 시장·기간마다 부호가 바뀌거나 오히려 음(-)이어서 점수에 넣지 않고 참고로만 보여줍니다."
               + (" 6~2개월 모멘텀(지수 대비)은 2020년 이후에는 뚜렷했지만 2006~2019년 독립 구간에서는 방향만 같고 통계적으로 유의하지 않아 역시 참고로만 둡니다."
                  if mode == "매수" else ""))
    for c in (c for c in res.checks if not c.scored):
        render_check_line(c)

    pillar_cols = st.columns(4)
    for col, axis in zip(pillar_cols, ("추세", "수급", "패턴", "거래량")):
        render_diag_pillar(col, axis, diag.pillars[axis]["state"], diag.pillars[axis]["detail"])

    st.markdown("##### 차트")
    tf_col, period_col = st.columns(2)
    timeframe = tf_col.radio("차트 봉", dchart.TIMEFRAMES, horizontal=True, key=f"{key_prefix}_timeframe")
    period_label = period_col.radio("표시 기간", list(dchart.VIEW_MONTHS), index=1, horizontal=True, key=f"{key_prefix}_period")
    fig = dchart.build_figure(diag.df, timeframe, dchart.VIEW_MONTHS[period_label],
                              diag.overlay_patterns(mode), title=f"{name} ({diag.code})")
    st.plotly_chart(fig, theme=None, key=f"{key_prefix}_chart")
    st.caption("※ 진단은 일봉 기준이며, 주봉·월봉은 같은 패턴을 겹쳐 보여주는 표시용입니다. ①②③… 은 패턴 꼭짓점, 점선은 넥라인(박스권은 상·하단)입니다.")

    st.markdown("##### 📰 요즘 이 종목의 재료")
    st.caption("※ 최근 7일 기사 기준이며 진단 시점과 무관합니다. 재료는 AI 요약이라 참고용으로만 보여주고 등급에는 반영하지 않습니다.")
    news = fetch_headlines_rss(name)
    if not news:
        st.write("최근 관련 뉴스가 없습니다.")
        return
    if GENAI_AVAILABLE:
        try:
            counts = get_news_sentiment_counts(news)
            st.caption(f"기사 감성 — 긍정 {counts['긍정']} · 중립 {counts['중립']} · 부정 {counts['부정']}")
        except Exception:
            pass
        try:
            st.info(get_material_summary_ai(name, news))
        except RuntimeError as e:
            st.caption(f"⚠️ AI 재료 요약을 불러오지 못했습니다: {e}")
    render_news_links(news, "📰 참고한 관련 기사 보기")

def render_diag_single():
    input_col, btn_col = st.columns([4, 1], vertical_alignment="bottom")
    stock_input = input_col.text_input("종목명 또는 6자리 종목코드", placeholder="예: POSCO홀딩스 또는 005490", key="diag_input")
    run_clicked = btn_col.button("진단 시작", key="diag_go", width="stretch")
    if run_clicked and stock_input.strip():
        st.session_state["diag_query"] = stock_input.strip()

    mode = st.radio("진단 모드", ["매수", "보유", "매도"], horizontal=True, key="diag_mode",
                    format_func=lambda m: f"{m} 진단",
                    help="매수: 지금 사도 되는가 / 보유: 들고 있는 종목을 어떻게 할 것인가(평단가 입력) / 매도: 팔아야 할 신호가 있는가")
    buy_price = None
    if mode == "보유":
        price_input = st.number_input("평단가(원) — 입력하면 손익 기준으로 손절·익절·물타기를 판정합니다", min_value=0, value=0, step=100, key="diag_buy_price")
        buy_price = float(price_input) if price_input > 0 else None

    today = now_kst().date()
    when_col, date_col = st.columns(2)
    when = when_col.radio("진단 시점", ["현재", "과거 날짜"], horizontal=True, key="diag_when")
    as_of = None
    if when == "과거 날짜":
        as_of = date_col.date_input("날짜", value=today - timedelta(days=30), max_value=today, key="diag_date")

    query = st.session_state.get("diag_query")
    if query:
        code, name, suggestions = resolve_diag_stock(query)
        if not code:
            st.warning("올바른 종목명 또는 6자리 종목코드를 입력해주세요." + (f" 혹시 이 종목인가요? {', '.join(suggestions)}" if suggestions else ""))
        else:
            days_back = (today - as_of).days if as_of else 0
            lookback = 700 + ((days_back // 100) + 1) * 100 if days_back > 0 else 700   # MA240이 계산될 만큼 진단 시점 이전 이력 확보
            with st.spinner("시세와 수급을 불러와 진단하는 중입니다..."):
                try:
                    df_raw = fetch_diag_ohlcv(code, lookback)
                except Exception as e:
                    df_raw = None
                    st.error(f"시세 데이터를 불러오지 못했습니다: {e}")
                if df_raw is not None:
                    try:
                        flow = fetch_diag_flow(code)
                    except Exception:
                        flow = None   # 수급만 평가에서 빠지고 나머지 항목은 그대로 진단
                    diag = dc.diagnose(code, as_of=as_of, buy_price=buy_price, df_raw=df_raw, flow=flow,
                                       index_close=get_diag_index(code, lookback))
                    if diag is None:
                        st.error("진단에 필요한 시세 데이터(최소 30거래일)가 부족합니다.")
                    else:
                        render_diagnosis(diag, name, mode, risk_asof=as_of)

# --- 관심종목 일괄 진단 -----------------------------------------------------------------
# 관심종목은 session_state에만 보관한다(Streamlit Cloud는 디스크가 휘발성). JSON으로 내려받아 두었다가 다시 불러온다.
# 위젯 상태를 지우거나 바꿔야 하는 동작(입력창 비우기, 선택 그룹 바꾸기)은 on_click 콜백에서 처리한다 —
# 위젯이 이미 그려진 뒤의 스크립트 본문에서는 그 위젯의 session_state 값을 바꿀 수 없다.
WL_SIGNAL_ICONS = {"매수 고려": "🟢", "지켜보기": "🟠", "사지 않기": "⚪", "매도 고려": "🔴"}

def _wl_get():
    if "watchlist" not in st.session_state:
        st.session_state["watchlist"] = wlc.empty_watchlist()
    return st.session_state["watchlist"]

def _wl_changed(message=None, kind="success"):
    """목록이 바뀌면 이전 일괄 진단 결과는 무효이고, 편집 표는 새 키로 다시 만든다."""
    st.session_state["wl_version"] = st.session_state.get("wl_version", 0) + 1
    st.session_state.pop("wl_results", None)
    if message:
        st.session_state["wl_msg"] = (kind, message)

def _wl_add_cb():
    wl, group = _wl_get(), st.session_state.get("wl_group")
    added, dup, failed = [], [], []
    for token in [t.strip() for t in st.session_state.get("wl_add_text", "").split(",") if t.strip()]:
        code, name, _ = resolve_diag_stock(token)
        if not code:
            failed.append(token)
        elif wlc.add_stock(wl, group, code, name):
            added.append(name)
        else:
            dup.append(name)
    st.session_state["wl_add_text"] = ""
    parts = ([f"추가: {', '.join(added)}"] if added else []) + ([f"이미 있거나 그룹이 가득 참: {', '.join(dup)}"] if dup else []) \
        + ([f"찾지 못함: {', '.join(failed)}"] if failed else [])
    kind = "success" if added and not (dup or failed) else "warning"
    if added:
        _wl_changed(" / ".join(parts), kind)
    elif parts:
        st.session_state["wl_msg"] = (kind, " / ".join(parts))

def _wl_new_group_cb():
    name = st.session_state.get("wl_new_group", "").strip()
    if wlc.add_group(_wl_get(), name):
        st.session_state["wl_group"] = name
        _wl_changed(f"그룹을 만들었습니다: {name}")
    else:
        st.session_state["wl_msg"] = ("warning", "그룹 이름이 비었거나 이미 있습니다(그룹은 최대 20개).")
    st.session_state["wl_new_group"] = ""

def _wl_delete_group_cb():
    wl, group = _wl_get(), st.session_state.get("wl_group")
    if wlc.delete_group(wl, group):
        st.session_state["wl_group"] = next(iter(wl["groups"]))
        _wl_changed(f"그룹을 삭제했습니다: {group}")
    else:
        st.session_state["wl_msg"] = ("warning", "마지막 하나 남은 그룹은 삭제할 수 없습니다.")

def _wl_remove_cb(group, key):
    codes = st.session_state.get(key, [])
    wlc.remove_stocks(_wl_get(), group, codes)
    st.session_state[key] = []
    if codes:
        _wl_changed(f"{len(codes)}개 종목을 삭제했습니다.")

def run_watchlist_diagnosis(items, max_workers=8):
    """관심종목 전체를 병렬로 진단. 반환: [(item, Diagnosis 또는 None, 실패 사유 또는 None)]. 시세·수급은 5분 캐시를 공유."""
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        code = item["code"]
        try:
            df_raw = fetch_diag_ohlcv(code, 700)
        except Exception:
            return item, None, "시세 조회 실패"
        try:
            flow = fetch_diag_flow(code)
        except Exception:
            flow = None   # 수급만 평가에서 빠지고 나머지는 그대로 진단
        diag = dc.diagnose(code, buy_price=item.get("buy_price"), df_raw=df_raw, flow=flow,
                           index_close=get_diag_index(code, 700))
        return (item, diag, None) if diag is not None else (item, None, "시세 이력 부족(30거래일 미만)")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(one, items))

def render_diag_watchlist():
    wl = _wl_get()
    version = st.session_state.get("wl_version", 0)
    st.caption("※ 관심종목은 이 브라우저 세션에만 보관됩니다(Streamlit Cloud는 저장 공간이 휘발성). 아래 JSON으로 내려받아 두었다가 다시 불러오세요.")

    msg = st.session_state.pop("wl_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])

    # JSON 입출력은 편집 표가 갱신된 뒤의 값을 내려받도록 맨 아래(편집 표 다음)에서 채우고, 자리는 먼저 잡아 둔다.
    io_box = st.container()

    uploaded = st.file_uploader("관심종목 JSON 불러오기(현재 목록을 덮어씁니다)", type="json", key="wl_upload")
    if uploaded is not None and st.session_state.get("wl_loaded_id") != uploaded.file_id:
        try:
            st.session_state["watchlist"] = wlc.loads(uploaded.getvalue().decode("utf-8"))
            st.session_state["wl_loaded_id"] = uploaded.file_id   # 같은 파일을 rerun마다 다시 적용해 편집 내용을 덮어쓰지 않도록
            st.session_state["wl_group"] = next(iter(st.session_state["watchlist"]["groups"]))
            _wl_changed(f"불러오기 완료: 그룹 {len(st.session_state['watchlist']['groups'])}개")
            st.rerun()
        except (ValueError, UnicodeDecodeError) as e:
            st.error(f"불러오기에 실패했습니다: {e}")

    groups = list(wl["groups"])
    if st.session_state.get("wl_group") not in groups:
        st.session_state["wl_group"] = groups[0]
    g1, g2, g3, g4 = st.columns([2, 2, 1, 1], vertical_alignment="bottom")
    group = g1.selectbox("그룹", groups, key="wl_group")
    g2.text_input("새 그룹 이름", key="wl_new_group", placeholder="예: 반도체")
    g3.button("그룹 만들기", key="wl_new_group_btn", on_click=_wl_new_group_cb, width="stretch")
    g4.button("그룹 삭제", key="wl_del_group_btn", on_click=_wl_delete_group_cb, width="stretch")

    a1, a2 = st.columns([4, 1], vertical_alignment="bottom")
    a1.text_input("종목 추가 — 종목명 또는 6자리 코드(콤마로 여러 개)", key="wl_add_text", placeholder="예: POSCO홀딩스, 005930, 삼성전자")
    a2.button("추가", key="wl_add_btn", on_click=_wl_add_cb, width="stretch")

    items = wl["groups"][group]
    if not items:
        io_box.download_button("⬇️ 관심종목 JSON 내려받기", wlc.dumps(wl), file_name="watchlist.json", mime="application/json", key="wl_download")
        st.info("관심종목이 비어 있습니다. 위에서 종목을 추가하거나 JSON을 불러오세요.")
        return

    editor_df = pd.DataFrame({
        "종목명": [i["name"] for i in items], "코드": [i["code"] for i in items],
        "평단가": pd.to_numeric([i["buy_price"] for i in items], errors="coerce"),
        "수량": pd.to_numeric([i["qty"] for i in items], errors="coerce"),
        "메모": [i["memo"] for i in items],
    })
    edited = st.data_editor(
        editor_df, hide_index=True, width="stretch", key=f"wl_editor_{group}_{version}", disabled=["종목명", "코드"],
        column_config={"평단가": st.column_config.NumberColumn("평단가(원)", min_value=0, step=100, format="%d"),
                       "수량": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
                       "메모": st.column_config.TextColumn(max_chars=wlc.MAX_MEMO_LEN)})
    for item, (_, row) in zip(items, edited.iterrows()):
        wlc.update_item(item, row["평단가"], row["수량"], row["메모"])
    io_box.download_button("⬇️ 관심종목 JSON 내려받기", wlc.dumps(wl), file_name="watchlist.json", mime="application/json", key="wl_download")

    rm_key = f"wl_remove_{group}"
    r1, r2 = st.columns([4, 1], vertical_alignment="bottom")
    names = {i["code"]: i["name"] for i in items}
    r1.multiselect("삭제할 종목", list(names), format_func=lambda c: f"{names[c]} ({c})", key=rm_key)
    r2.button("선택 삭제", key=f"{rm_key}_btn", on_click=_wl_remove_cb, args=(group, rm_key), width="stretch")

    st.divider()
    run_clicked = st.button(f"📊 '{group}' 일괄 진단 실행 ({len(items)}종목)", key="wl_run", type="primary")
    if run_clicked:
        with st.spinner("시세와 수급을 불러와 진단하는 중입니다..."):
            results = run_watchlist_diagnosis(items)
        with st.spinner("DART에서 최근 위험 공시를 확인하는 중입니다..."):
            flags = run_risk_flags([item["code"] for item, diag, _ in results if diag is not None])
        st.session_state["wl_results"] = {"group": group, "ts": now_kst().strftime('%H:%M'), "results": results, "flags": flags,
                                          "sig": tuple((i["code"], i["buy_price"]) for i in items)}

    saved = st.session_state.get("wl_results")
    if not saved or saved["group"] != group:
        st.info("일괄 진단을 실행하면 종목별 등급·신호 표가 나옵니다. 평단가를 입력한 종목은 보유 판정도 함께 계산됩니다.")
        return
    if saved["sig"] != tuple((i["code"], i["buy_price"]) for i in items):
        st.warning("평단가가 바뀌어 아래 결과가 오래되었습니다. 일괄 진단을 다시 실행하세요.")
    st.caption(f"마지막 실행 {saved['ts']} · 시세·수급은 5분간 캐시됩니다.")

    ok = [(item, diag) for item, diag, _ in saved["results"] if diag is not None]
    failed = [f"{item['name']}({reason})" for item, diag, reason in saved["results"] if diag is None]
    if failed:
        st.caption(f"⚠️ 진단하지 못한 종목: {', '.join(failed)}")
    if not ok:
        return

    table = pd.DataFrame([dc.to_row(diag, item["name"]) for item, diag in ok])
    flags = saved.get("flags")      # 이 기능이 생기기 전에 만든 결과에는 없다
    flags_ok = bool(flags) and any(s == "ok" for s, _ in flags.values())
    if flags_ok:
        table["위험 공시"] = table["코드"].map(lambda c: rdisc.short_text(flags[c][1]) if flags.get(c, ("err",))[0] == "ok" else "확인 불가")
    elif flags:
        reason = next(iter(flags.values()))[1]
        if dart_unreachable(reason):
            st.caption(f"⚠️ 위험 공시를 확인하지 못했습니다. '위험 공시 없음'이 아니라 '확인 못 함'입니다. {DART_UNREACHABLE}")
        else:
            st.caption(f"⚠️ DART 위험 공시를 확인하지 못했습니다({reason}). Secrets에 DART_API_KEY가 있는지 확인해 주세요.")
    counts = table["신호"].value_counts()
    metric_cols = st.columns(5)
    metric_cols[0].metric("전체", len(table))
    for col, signal in zip(metric_cols[1:], dc.SIGNAL_ORDER):
        col.metric(f"{WL_SIGNAL_ICONS[signal]} {signal}", int(counts.get(signal, 0)))

    f1, f2 = st.columns([3, 2])
    picked = f1.multiselect("신호 필터", list(dc.SIGNAL_ORDER), default=list(dc.SIGNAL_ORDER), key="wl_filter")
    sort_by = f2.radio("정렬", ["등급순", "등락률순", "이름순"], horizontal=True, key="wl_sort")
    view = table[table["신호"].isin(picked)]
    if sort_by == "등급순":
        view = view.sort_values(["_grade_rank", "_ratio", "종목명"], ascending=[True, False, True])
    elif sort_by == "등락률순":
        view = view.sort_values("등락률%", ascending=False)
    else:
        view = view.sort_values("종목명")
    view = view.reset_index(drop=True)
    if view.empty:
        st.info("선택한 신호에 해당하는 종목이 없습니다.")
        return

    shown = view.assign(신호=view["신호"].map(lambda s: f"{WL_SIGNAL_ICONS[s]} {s}"))[
        ["종목명", "신호", "매수 등급", "핵심 통과", "매도 판정", "보유 판정", "손익%", "종가", "등락률%", "추세", "수급", "거래량", "패턴"]
        + (["위험 공시"] if flags_ok else [])]
    event = st.dataframe(
        shown, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="wl_table",
        column_config={"종가": st.column_config.NumberColumn(format="%d"),
                       "등락률%": st.column_config.NumberColumn(format="%+.2f"),
                       "손익%": st.column_config.NumberColumn(format="%+.1f")})
    st.caption("※ 매수 등급·핵심 통과 = 240일선·수급·거래량 기준. 신호는 위험 우선(매도 고려 > 매수 고려 > 사지 않기 > 지켜보기)으로 하나만 표시합니다. 행을 클릭하면 상세 진단이 아래에 열립니다.")
    if flags_ok:
        n_flag = int(table["위험 공시"].str.startswith(("🔴", "🟠")).sum())
        st.caption(f"※ 위험 공시 = DART 공시 후 30일 이내의 🔴 경고(감자 결정, 관리종목 지정·상장폐지 사유)·🟠 주의(감사의견 부적정·거절·자본잠식 사실확인, 관리종목 지정 우려 안내). 지금 {n_flag}종목. "
                   "과거 통계상 이런 공시 뒤 평균적으로 시장보다 약했다는 뜻이며 신호·등급과는 별개입니다. 상세 진단에서 근거와 한계를 볼 수 있습니다.")

    selected = event.selection.rows
    if not selected:
        return
    code = view.iloc[selected[0]]["코드"]
    item, diag = next((it, d) for it, d in ok if d.code == code)
    st.divider()
    mode = st.radio("상세 진단 모드", ["매수", "보유", "매도"], horizontal=True, key="wl_mode", format_func=lambda m: f"{m} 진단")
    render_diagnosis(diag, item["name"], mode, key_prefix="wl")

if menu == "AI 종목 진단":
    st.subheader("🩺 AI 종목 진단")
    st.markdown("240일선·수급·거래량 3개 핵심 항목으로 등급을 매기고, 10일선·20일선·정배열·패턴·6~2개월 모멘텀은 참고 신호로 보여줍니다. 판정은 모두 규칙 기반이라 같은 데이터면 항상 같은 결과가 나옵니다.")
    st.caption("※ 투자 참고용 점검표입니다. 과거 검증(코스피 상위 300·코스닥 상위 100, 20일·60일 보유, 수급은 과거 데이터가 없어 제외)에서 "
               "'사지 않기'(종가가 240일선 아래)는 2020년 이후 세 시장 모두 시장 대비 열위였고 2006~2014년에도 같은 방향이었지만, "
               "2015~2019년에는 효과가 없었습니다(장세 의존). '매수 고려'가 '지켜보기'보다 낫다는 근거는 확인되지 않았습니다. "
               "매수 신호보다는 피해야 할 종목을 거르는 용도에 가깝습니다. 투자 판단과 책임은 본인에게 있습니다.")
    tab_single, tab_watch = st.tabs(["🔎 종목 진단", "⭐ 관심종목 일괄 진단"])
    with tab_single:
        render_diag_single()
    with tab_watch:
        render_diag_watchlist()

# --- 투자사이트 ---------------------------------------------------------------------------
# 버핏연구소·Stock Issue Service·SMIC의 분석 글을 모아 언급 종목 현황, 핵심 요약, 글 이후 주가 영향도를 보여준다.
# 글 수집은 GitHub Actions(update_invest_sites.py)가 매일 하고 invest_sites_data.json에 쌓인다. 시세·현황은 화면을 열 때마다 새로 계산한다.
INVEST_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invest_sites_data.json")
INVEST_TONE_ICON = {"긍정": "🟢", "중립": "⚪", "부정": "🔴"}
INVEST_SOURCE_NAMES = {k: v["name"] for k, v in isc.SOURCES.items()}
INVEST_HIDDEN_BY_DEFAULT = ("eduinfom",)    # 'AI 보조 작성'이 하루 수십 건씩 쏟아지는 출처 — 기본은 숨기고 출처 필터에서 켠다
INVEST_MAX_PRICED = 150     # 한 화면에서 시세를 새로 조회하는 최대 종목 수(5분 캐시, 8개 병렬)

@st.cache_data(ttl=600)
def load_invest_data(mtime):
    return isc.load_data(INVEST_DATA_PATH)

def invest_data():
    try:
        mtime = os.path.getmtime(INVEST_DATA_PATH)
    except OSError:
        mtime = 0
    return load_invest_data(mtime)

def invest_prices(codes):
    """언급 종목의 최근 종가 시리즈(5분 캐시 공유). 조회 실패 종목은 None."""
    from concurrent.futures import ThreadPoolExecutor

    def one(code):
        try:
            return code, fetch_diag_ohlcv(code, 300)["Close"].astype(float).dropna()
        except Exception:
            return code, None

    with ThreadPoolExecutor(8) as ex:
        return dict(ex.map(one, sorted(codes)))

def invest_stock_table(arts, prices):
    """종목별로 묶은 언급 현황. 현재가·전일비·글 이후 수익률·목표가 여력은 지금 시세로 계산한다."""
    rows = []
    by_code = {}
    for a in arts:
        for s in a.get("stocks", []):                           # 주 종목과 제목에 함께 나온 관련 종목 모두
            by_code.setdefault(s["code"], []).append(a)
    for code, items in by_code.items():
        items.sort(key=lambda a: (a["date"] or "", a["id"]), reverse=True)
        last = items[0]
        name = next(s["name"] for s in last["stocks"] if s["code"] == code)
        close = prices.get(code)
        row = {"종목명": name, "코드": code, "최근 언급일": last["date"], "언급 글": len(items),
               "출처": " · ".join(sorted({INVEST_SOURCE_NAMES[a["source"]] for a in items})),
               "논조": " ".join(f"{INVEST_TONE_ICON[t]}{sum(1 for a in items if a.get('tone') == t)}" for t in ("긍정", "중립", "부정")),
               "_date": last["date"]}
        tgt = next((a for a in items if a.get("target_price")), None)
        if tgt:
            row["최근 의견"] = f"{tgt.get('broker', '')} {tgt.get('opinion', '')} {tgt['target_price']:,}원" + (f" ({tgt['target_dir']})" if tgt.get("target_dir") else "")
        if close is not None and len(close) > 2:
            now = float(close.iloc[-1])
            row["현재가"], row["전일비%"] = now, (now / float(close.iloc[-2]) - 1) * 100
            pos = int(close.index.searchsorted(pd.Timestamp(last["date"])))
            if 0 < pos < len(close) - 1:                       # 최근 글이 나온 날 종가 → 지금
                row["글 이후%"] = (now / float(close.iloc[pos]) - 1) * 100
            if tgt:
                row["목표가 여력%"] = (tgt["target_price"] / now - 1) * 100
        row["핵심"] = (isc.BYLINE.sub("", last["summary"][0])[:90] + "…") if last.get("summary") else ""
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.sort_values(["_date", "언급 글"], ascending=False).reset_index(drop=True) if len(df) else df

def render_invest_article(a, key):
    stock_txt = ", ".join(s["name"] for s in a.get("stocks", [])) or "(상장 종목 아님)"
    flags = " 🤖 AI 보조 작성" if a.get("ai_assisted") else ""
    with st.container(border=True):
        st.markdown(f"**[{a['title']}]({a['url']})**  \n"
                    f"<span style='color:gray; font-size:0.85rem;'>{INVEST_SOURCE_NAMES[a['source']]} · {a['kind']} · {a['date']} · {stock_txt}{flags}</span>",
                    unsafe_allow_html=True)
        facts = [f"{INVEST_TONE_ICON.get(a.get('tone'), '⚪')} 논조 {a.get('tone', '중립')}(제목 키워드 기준)"]
        if a.get("broker"):
            facts.append(a["broker"])
        if a.get("opinion"):
            facts.append(f"의견 {a['opinion']}")
        if a.get("target_price"):
            facts.append(f"목표가 {a['target_price']:,}원" + (f" ← {a['target_prev']:,}원" if a.get("target_prev") else "") + (f" ({a['target_dir']})" if a.get("target_dir") else ""))
        st.caption(" · ".join(facts))
        for s in a.get("summary", []):
            st.markdown(f"- {isc.BYLINE.sub('', s)}")
        if a.get("summary_by") == "rule":
            st.caption("※ 원문 앞부분을 발췌한 요약입니다.")
        rx = a.get("reaction")
        if rx:
            parts = [f"공표 전 20일 {rx['abn_pre20'] * 100:+.1f}%p"] if "abn_pre20" in rx else []
            if "abn_d0" in rx:
                parts.append(f"당일 {rx['abn_d0'] * 100:+.1f}%p")
            for k in isc.POST_DAYS:
                if f"abn_post{k}" in rx:
                    parts.append(f"공표 후 {k}일 {rx[f'abn_post{k}'] * 100:+.1f}%p")
            if parts:
                st.caption("📈 지수 대비 초과수익: " + " · ".join(parts))

def render_invest_chart(code, name, arts, close):
    import plotly.graph_objects as go
    c = close.tail(160)
    fig = go.Figure(go.Scatter(x=c.index, y=c.values, mode="lines", line=dict(color="#1857e0", width=1.6), name="종가"))
    for a in arts:
        pos = int(c.index.searchsorted(pd.Timestamp(a["date"])))
        if pos >= len(c):
            continue
        fig.add_trace(go.Scatter(x=[c.index[pos]], y=[c.iloc[pos]], mode="markers", name=INVEST_SOURCE_NAMES[a["source"]],
                                 marker=dict(size=11, symbol="diamond", color={"긍정": "#d60000", "부정": "#1857e0"}.get(a.get("tone"), "#6b7280"), line=dict(width=1, color="white")),
                                 text=[a["title"][:50]], hoverinfo="text+x+y", showlegend=False))
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), title=f"{name}({code}) — ◆는 분석 글이 나온 날(빨강=긍정, 파랑=부정, 회색=중립)", hovermode="x unified")
    st.plotly_chart(fig, theme=None, key=f"invest_chart_{code}")

def render_invest_sites():
    st.markdown("버핏연구소·Stock Issue Service·SMIC의 분석 글에서 **언급된 종목**을 모아 현황을 보여주고, 글의 **핵심을 요약**하고, 글이 나온 뒤 **주가가 어떻게 움직였는지**를 봅니다.")
    st.caption("※ 원문은 저장하지 않고 제목·날짜·링크와 짧은 요약만 보관하며, 모든 글에 원문 링크를 붙입니다. 요약은 원문 발췌 또는 AI 요약이라 원문과 다를 수 있습니다. "
               "논조는 제목 키워드·목표가 방향으로 매긴 거친 분류이고, Stock Issue Service 글은 'AI 보조 작성'으로 표기돼 있습니다. 투자 판단과 책임은 본인에게 있습니다.")
    data = invest_data()
    arts_all = data.get("articles", [])
    if not arts_all:
        st.info("아직 수집된 글이 없습니다. 아래 '지금 수집'을 누르거나 `python update_invest_sites.py`를 실행하세요.")
    top = st.columns([2, 2, 2, 1.4])
    days = top[0].selectbox("기간", [7, 14, 30, 60], index=1, format_func=lambda d: f"최근 {d}일", key="invest_days")
    srcs = top[1].multiselect("출처", list(INVEST_SOURCE_NAMES), default=[k for k in INVEST_SOURCE_NAMES if k not in INVEST_HIDDEN_BY_DEFAULT],
                              format_func=INVEST_SOURCE_NAMES.get, key="invest_srcs")
    tones = top[2].multiselect("논조", ["긍정", "중립", "부정"], default=["긍정", "중립", "부정"], key="invest_tones")
    if top[3].button("지금 수집", key="invest_refresh", help="최근 3일 새 글을 지금 수집합니다(요약은 원문 발췌, 약 1~2분)."):
        with st.spinner("새 글을 수집하고 주가 반응을 계산하는 중입니다..."):
            try:
                isu.run_update(INVEST_DATA_PATH, days=3, use_llm=False, log=lambda *_: None)
                load_invest_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"수집에 실패했습니다: {e}")
    updated = data.get("updated")
    totals = data.get("meta", {}).get("sources", {})
    st.caption(f"마지막 업데이트: {updated[:16].replace('T', ' ') if updated else '-'} (평일 장 마감 후 자동 수집) · 최근 60일 {len(arts_all):,}건 표시 · 누적 {sum(totals.values()):,}건("
               + " · ".join(f"{INVEST_SOURCE_NAMES.get(k, k)} {v:,}" for k, v in totals.items()) + ")")

    since = (now_kst().date() - timedelta(days=days)).strftime("%Y-%m-%d")
    arts = [a for a in arts_all if (a.get("date") or "") >= since and a["source"] in srcs and (a.get("tone", "중립") in tones)]
    hidden = Counter(a["source"] for a in arts_all if (a.get("date") or "") >= since and a["source"] not in srcs)
    if hidden:
        st.caption("※ 출처 필터로 숨겨진 글: " + " · ".join(f"{INVEST_SOURCE_NAMES[k]} {v:,}건" for k, v in hidden.items())
                   + (" (AI 보조 작성 글이 많아 기본은 숨깁니다. '출처'에서 추가하면 보입니다.)" if any(k in INVEST_HIDDEN_BY_DEFAULT for k in hidden) else ""))
    tab_stock, tab_arts, tab_impact = st.tabs(["📌 언급 종목 현황", "📰 글 요약", "📊 주가 영향도"])

    with tab_stock:
        recent_codes = []                                      # 최근 언급 순서로 중복 제거
        for a in sorted(arts, key=lambda a: (a.get("date") or "", a["id"]), reverse=True):
            for s in a.get("stocks", []):
                if s["code"] not in recent_codes:
                    recent_codes.append(s["code"])
        if not recent_codes:
            st.info("조건에 맞는 언급 종목이 없습니다.")
        else:
            if len(recent_codes) > INVEST_MAX_PRICED:
                st.caption(f"※ 언급 종목이 {len(recent_codes)}개라 시세·현황은 최근 언급 {INVEST_MAX_PRICED}개 종목만 계산합니다(기간이나 출처를 좁히면 더 볼 수 있습니다).")
                keep = set(recent_codes[:INVEST_MAX_PRICED])
                arts = [dict(a, stocks=[s for s in a.get("stocks", []) if s["code"] in keep]) for a in arts]
                arts = [a for a in arts if a["stocks"] or a["source"] == "smic"]
            with st.spinner("현재 시세를 불러오는 중입니다..."):
                prices = invest_prices(set(recent_codes[:INVEST_MAX_PRICED]))
            table = invest_stock_table(arts, prices)
            m = st.columns(4)
            m[0].metric("언급 종목", len(table))
            m[1].metric("글", len({a["id"] for a in arts if a.get("stocks")}))
            m[2].metric("2곳 이상이 다룬 종목", int(sum(1 for _, r in table.iterrows() if r["출처"].count("·") >= 1)))
            up = table["전일비%"].dropna() if "전일비%" in table else pd.Series(dtype=float)
            m[3].metric("전일 상승 종목", f"{int((up > 0).sum())}/{len(up)}")
            show = table.drop(columns=["_date"])
            event = st.dataframe(show, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="invest_table",
                                 column_config={"현재가": st.column_config.NumberColumn(format="localized"), "전일비%": st.column_config.NumberColumn(format="%+.2f"),
                                                "글 이후%": st.column_config.NumberColumn(format="%+.1f", help="가장 최근 글이 나온 날 종가 → 현재가"),
                                                "목표가 여력%": st.column_config.NumberColumn(format="%+.1f", help="가장 최근 목표가 ÷ 현재가 − 1"),
                                                "핵심": st.column_config.TextColumn(width="large")})
            sel = event.selection.rows if event and event.selection else []
            if sel:
                row = table.iloc[sel[0]]
                code = row["코드"]
                st.divider()
                mine = sorted([a for a in arts if any(s["code"] == code for s in a.get("stocks", []))], key=lambda a: a["date"], reverse=True)
                if prices.get(code) is not None:
                    render_invest_chart(code, row["종목명"], mine, prices[code])
                for i, a in enumerate(mine[:10]):
                    render_invest_article(a, f"stock_{code}_{i}")
            else:
                st.caption("종목 행을 클릭하면 아래에 주가 차트와 글 요약이 열립니다.")

    with tab_arts:
        ordered = sorted(arts, key=lambda a: (a.get("date") or "", a["id"]), reverse=True)
        st.caption(f"{len(ordered)}건 중 최신 60건")
        for i, a in enumerate(ordered[:60]):
            render_invest_article(a, f"art_{i}")

    with tab_impact:
        impact = pd.DataFrame(data.get("impact") or [])
        if impact.empty:
            st.info("영향도를 계산할 표본이 아직 없습니다. `python update_invest_sites.py --backfill-days 365`로 과거 글을 모아 주세요.")
        else:
            st.markdown("글이 나온 날을 기준으로 종목 수익률에서 **같은 기간 시장 지수(코스피/코스닥) 수익률을 뺀 초과수익**의 평균입니다.")
            st.caption("공표 전 20일 = 글이 나오기 전 이미 움직였는지 · 공표 당일 = 글과 같은 날의 반응(글을 읽고 따라 살 수 있는 수익이 아님) · 공표 후 N일 = 글이 나온 날 종가 이후의 수익(읽고 난 뒤 기준). "
                       "같은 날 나온 글은 하루 평균으로 묶고 그 날짜 시계열로 t값(Newey-West)을 냈습니다. 날짜가 30개 미만인 칸은 비웁니다.")
            n = data.get("meta", {}).get("with_reaction", 0)
            st.caption(f"주가 반응을 계산한 글 {n:,}건 기준(같은 종목·같은 날·같은 출처의 중복은 1건으로 집계).")
            fmt = {c: "{:+.2f}" for c in impact.columns if c.endswith("%") and c != "5일 승률%"}
            fmt["글 수"], fmt["날짜 수"] = "{:,.0f}", "{:,.0f}"
            fmt.update({c: "{:.1f}" for c in impact.columns if c.endswith(" t")})
            fmt["5일 승률%"] = "{:.0f}"
            st.dataframe(impact.style.format(fmt, na_rep="-"), hide_index=True, width="stretch")
            import plotly.graph_objects as go
            cols = ["공표 전 20일 초과%", "공표 당일 초과%", "공표 후 1일 초과%", "공표 후 5일 초과%", "공표 후 20일 초과%"]
            g = impact[impact["그룹"].isin(["전체"] + [f"source={k}" for k in INVEST_SOURCE_NAMES])]
            fig = go.Figure()
            for _, r in g.iterrows():
                label = "전체" if r["그룹"] == "전체" else INVEST_SOURCE_NAMES.get(r["그룹"].split("=")[1], r["그룹"])
                fig.add_trace(go.Bar(name=f"{label} ({int(r['글 수'])}건)", x=[c.replace(" 초과%", "") for c in cols], y=[r[c] for c in cols]))
            fig.update_layout(barmode="group", height=340, margin=dict(l=10, r=10, t=30, b=10), yaxis_title="지수 대비 초과수익 평균(%p)", title="글 전후 평균 초과수익")
            st.plotly_chart(fig, theme=None, key="invest_impact_chart")
            st.warning("해석할 때 주의: ① 증권사 리포트·이슈 글은 대개 이미 일어난 실적·주가 변동을 뒤따라 나옵니다(공표 전 20일·당일 값을 보세요). ② 평균은 t값(|t|<2면 우연과 구별이 어려움)과 함께 보세요. "
                       "③ 현재 시총 상위·언급 종목 위주라 생존편향이 있고, 거래비용은 반영하지 않았습니다.")

if menu == "투자사이트":
    st.subheader("📚 투자사이트")
    render_invest_sites()

if menu == "개별종목분석":  # 💡 화면의 사이드바 메뉴명과 완벽히 일치시켰습니다.
    st.subheader("🤖 개별종목분석")
    st.markdown("기술적 지표(10%), 최신 뉴스 및 수급(40%), 경영지표(20%), 밸류에이션(10%), 시장 트렌드(20%)를 종합 분석합니다.")

    # 1. 탭 정의
    tab1, tab2, tab3 = st.tabs(["개별종목 List", "ETF List", "개별종목 분석"])

    # ==========================================
    # Tab 1: 개별종목 List
    # ==========================================
    with tab1:
        st.info("💡 분석을 원하는 **개별종목 최대 10개**의 **종목명**(예: 삼성전자) 또는 **6자리 종목코드**(예: 005930)를 입력하세요. 콤마(,)로 구분해주세요.")
        
        stock_list_input = st.text_area("종목명 또는 종목코드 입력 (콤마로 구분, 최대 10개)", height=100)
        
        if st.button("개별종목 분석 시작", key="stock_list_analyze") and stock_list_input:
            inputs = [x.strip() for x in stock_list_input.split(',')]
            inputs = inputs[:10]  # 최대 10개로 제한
            
            code_map = get_stock_code_map()
            stock_codes = []
            failed_inputs = []  # 종목코드 매핑에 실패한 입력값 추적용

            for inp in inputs:
                if inp.isdigit() and len(inp) == 6:
                    stock_codes.append(inp)
                else:
                    code = code_map.get(inp)
                    if code:
                        stock_codes.append(code)
                    else:
                        failed_inputs.append(inp)

            if failed_inputs:
                st.warning(
                    f"⚠️ 다음 종목을 찾지 못해 분석에서 제외했습니다: **{', '.join(failed_inputs)}**\n\n"
                    f"KRX 정식 종목명(예: '현대차'가 안 되면 '현대자동차')이나 6자리 종목코드로 다시 입력해보세요."
                )

            if not stock_codes:
                st.warning("올바른 종목명 또는 6자리 종목코드를 입력해주세요.")
            else:
                analysis_results = []
                progress_bar = st.progress(0)
                
                for idx, code in enumerate(stock_codes):
                    with st.spinner(f"분석 중... ({idx+1}/{len(stock_codes)})"):
                        fundamentals = fetch_stock_name_and_fundamentals(code)
                        if fundamentals:
                            fundamentals['sector_per'] = fetch_sector_average_per(code)
                            tech = analyze_stock_technical(code)
                            if tech:
                                news = fetch_headlines_rss(fundamentals['name'])
                                total_score, _, _, _, _, _ = calculate_stock_score(fundamentals, tech, news)
                                macd_signal = '매수' if tech['macd'] > tech['signal'] else '매도'
                                risk_levels = compute_risk_levels(tech['price'], tech.get('atr'))
                                analysis_results.append({
                                    "종목명": fundamentals['name'],
                                    "총점": total_score,
                                    "현재주가": f"{int(tech['price']):,}원",
                                    "참고 손절가": f"{int(risk_levels['stop_loss']):,}원" if risk_levels else "-",
                                    "참고 목표가": f"{int(risk_levels['target_price']):,}원" if risk_levels else "-",
                                    "영업이익률": f"{fundamentals['op_margin']}%",
                                    "ROE": f"{fundamentals['roe']}%",
                                    "PBR": f"{fundamentals['pbr']}배",
                                    "RSI": f"{tech['rsi']:.2f}",
                                    "MACD": macd_signal,
                                    "볼린저밴드(%B)": f"{tech['bb_pct_b']:.2f}" if pd.notna(tech.get('bb_pct_b')) else "-",
                                    "스토캐스틱(%K)": f"{tech['stoch_k']:.1f}" if pd.notna(tech.get('stoch_k')) else "-",
                                    "주간추세": tech.get('weekly_trend') or "-",
                                    "1개월수익률": f"{tech['one_month_return']:.2f}%"
                                })
                    progress_bar.progress((idx + 1) / len(stock_codes))

                if analysis_results:
                    st.markdown("---")
                    st.markdown("### 📊 분석 결과")
                    df_results = pd.DataFrame(analysis_results)
                    st.dataframe(df_results, use_container_width=True)
                    st.caption("※ 참고 손절가/목표가는 ATR(변동성) 기반 근사치입니다 (현재가 ± 2~3×ATR14). 실제 지지/저항선을 반영한 값이 아니므로 투자 판단의 참고 자료로만 활용하세요.")

    # ==========================================
    # Tab 2: ETF List
    # ==========================================
    with tab2:
        st.markdown("### 📊 ETF 시장 실시간 핫 트렌드 (Hot Trends)")
        st.write("시장 내 거래량, 수익률, 신규 상장 트렌드를 분석하여 상위 10선 리스트를 제공합니다.")
        
        with st.spinner("ETF 시장 트렌드 데이터를 수집 중입니다..."):
            df_etf, etf_ts = fetch_etf_market_data()

        if df_etf.empty:
            st.error("ETF 데이터를 불러올 수 없습니다. 잠시 후 다시 시도해주세요.")
        else:
            render_data_ts_caption(etf_ts, cache_minutes=30)
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.markdown("#### 💰 자금유입 (거래대금) 상위 10선")
                with st.spinner("총보수/분배율/변동성 지표 조회 중..."):
                    df_inflow_display = fetch_etf_inflow_top10(df_etf)
                st.dataframe(
                    df_inflow_display[['종목명', '종목코드', '현재가', '거래대금', '변동성(20일 연율화)', '총보수', '분배율']],
                    hide_index=True, use_container_width=True
                )

            with col2:
                st.markdown("#### 📈 1주일 수익률 상위 10선")
                st.caption("※ 레버리지/인버스 상품은 제외한 순위입니다.")
                with st.spinner("주간 수익률 분석 중..."):
                    df_weekly = fetch_etf_weekly_returns(df_etf)
                if not df_weekly.empty:
                    st.dataframe(
                        df_weekly[['종목명', '종목코드', '현재가', '1주일 수익률', '변동성(20일 연율화)', '총보수', '분배율']],
                        hide_index=True, use_container_width=True
                    )
                else:
                    st.warning("수익률 데이터를 연산할 수 없습니다.")

            with col3:
                st.markdown("#### 🆕 신규 상장 추정 ETF 10선")
                st.caption("※ 실제 상장일 데이터가 아닌, 종목코드가 큰 순서(대체로 최근 상장분에 부여)로 추정한 목록입니다.")
                with st.spinner("총보수/분배율/변동성 지표 조회 중..."):
                    df_new_display = fetch_etf_new_listing_top10(df_etf)
                st.dataframe(
                    df_new_display[['종목명', '종목코드', '현재가', '시가총액', '변동성(20일 연율화)', '총보수', '분배율']],
                    hide_index=True, use_container_width=True
                )

            st.caption("※ 변동성은 최근 20거래일 일간수익률 표준편차를 연율화(연 252거래일 가정)한 근사치이며, 총보수/분배율은 네이버 금융 제공 수치(분배율은 최근 12개월 기준)로 데이터가 없는 종목은 \"-\"로 표시됩니다.")

            st.markdown("---")
            st.markdown("### 🗂️ 카테고리별 ETF 트렌드")
            st.write("종목명 키워드를 기반으로 국내 섹터 / 테마 / 배당 / 원자재 / 해외지수 / 레버리지·인버스로 분류하여, 각 카테고리 내 거래대금 상위 종목들의 1주일 수익률 순위를 보여줍니다.")

            cat_tabs = st.tabs(["🏭 국내 섹터", "🚀 테마", "💵 배당", "🪙 원자재", "🌍 해외지수", "⚡ 레버리지/인버스"])
            cat_names = ["국내 섹터", "테마", "배당", "원자재", "해외지수", "레버리지/인버스"]

            for cat_tab, cat_name in zip(cat_tabs, cat_names):
                with cat_tab:
                    if cat_name == "레버리지/인버스":
                        st.caption("⚠️ 변동성이 매우 큰 고위험 상품입니다. 단기 트레이딩 목적 외에는 신중한 접근이 필요합니다.")
                    with st.spinner(f"{cat_name} ETF 수익률 분석 중..."):
                        df_cat_res = fetch_etf_category_returns(df_etf, cat_name)
                    if not df_cat_res.empty:
                        st.dataframe(
                            df_cat_res[['종목명', '종목코드', '현재가', '1주일 수익률', '변동성(20일 연율화)', '총보수', '분배율']],
                            hide_index=True, use_container_width=True
                        )
                    else:
                        st.warning(f"{cat_name} 카테고리에 해당하는 데이터를 찾을 수 없습니다.")

    # ==========================================
    # Tab 3: 개별종목 분석
    # ==========================================
    with tab3:
        st.info("💡 분석을 원하는 종목의 **종목명**(예: 삼성전자) 또는 **6자리 종목코드**(예: 005930)를 입력하세요.")
        
        user_input = st.text_input("종목명 또는 종목코드 입력", key="single_stock_input")
        
        if st.button("AI 분석 시작", key="single_analyze") and user_input:
            user_input = user_input.strip()
            stock_code = None
            if user_input.isdigit() and len(user_input) == 6:
                stock_code = user_input
            else:
                with st.spinner("종목명을 검색하는 중..."):
                    code_map = get_stock_code_map()
                    stock_code = code_map.get(user_input)
            
            if not stock_code:
                st.warning("올바른 종목명 또는 6자리 종목코드를 입력해주세요.")
            else:
                with st.spinner("해당 종목의 펀더멘털, 차트, 수급 및 뉴스 데이터를 분석 중입니다..."):
                    fundamentals = fetch_stock_name_and_fundamentals(stock_code)

                    if not fundamentals:
                        st.error("종목 정보를 불러올 수 없습니다. 입력값을 확인해주세요.")
                    else:
                        fundamentals['sector_per'] = fetch_sector_average_per(stock_code)
                        tech = analyze_stock_technical(stock_code)
                        news = fetch_headlines_rss(fundamentals['name'])

                        if tech is None:
                            st.error("기술적 분석을 위한 충분한 차트 데이터가 없습니다.")
                        else:
                            total_score, news_score, val_score, trend_score, tech_score, mgmt_score = calculate_stock_score(fundamentals, tech, news)

                            opinion = "매도"
                            color = "red"
                            if total_score >= 80:
                                opinion = "강력 매수"
                                color = "green"
                            elif total_score >= 60:
                                opinion = "매수"
                                color = "blue"
                            elif total_score >= 40:
                                opinion = "관망"
                                color = "orange"

                            st.markdown("---")
                            st.markdown(f"### 📊 [{fundamentals['name']}] AI 매수 분석 결과: **:{color}[{opinion}]** (총점: {total_score}점)")

                            col1, col2, col3, col4 = st.columns(4)
                            col1.metric("종합 점수", f"{total_score}점")
                            col2.metric("뉴스/수급 (40)", f"{news_score}점")
                            col3.metric("경영/밸류 (30)", f"{mgmt_score + val_score}점")
                            col4.metric("기술/트렌드 (30)", f"{tech_score + trend_score}점")
                            st.caption("※ 뉴스/수급 점수는 AI가 각 뉴스 제목의 문맥을 읽고 판정한 감성(긍정/중립/부정)을 기반으로 계산됩니다. (AI 판정 실패 시 키워드 매칭 방식으로 자동 대체)")

                            risk_levels = compute_risk_levels(tech['price'], tech.get('atr'))
                            if risk_levels:
                                r1, r2 = st.columns(2)
                                r1.metric("📉 참고 손절가", f"{int(risk_levels['stop_loss']):,}원")
                                r2.metric("🎯 참고 목표가", f"{int(risk_levels['target_price']):,}원")
                                st.caption("※ ATR(변동성) 기반 근사치입니다 (현재가 − 2×ATR14 / + 3×ATR14). 실제 지지/저항선을 반영한 값이 아니므로 참고 자료로만 활용하세요.")

                            if GENAI_AVAILABLE:
                                st.markdown("#### 🤖 AI 심층 투자 분석 리포트")
                                with st.spinner("AI가 재무·기술·뉴스를 종합한 심층 리포트를 작성하고 있습니다..."):
                                    try:
                                        ai_report = get_individual_stock_ai_analysis(fundamentals, tech, news)
                                    except RuntimeError as e:
                                        ai_report = f"⚠️ AI 분석 리포트 생성 중 오류가 발생했습니다: {e}"
                                st.info(ai_report)

                            st.markdown("#### 🔍 상세 지표 분석")
                            t1, t2, t3 = st.tabs(["재무 및 밸류에이션", "기술적 지표 및 트렌드", "관련 최신 뉴스"])
                            with t1:
                                st.write(f"- **영업이익률**: {fundamentals['op_margin']}%")
                                st.write(f"- **ROE**: {fundamentals['roe']}%")
                                st.write(f"- **추정 PER**: {fundamentals['cns_per']}배 (후행 PER: {fundamentals['per']}배)")
                                st.write(f"- **PBR**: {fundamentals['pbr']}배")
                            with t2:
                                st.write(f"- **현재 주가**: {int(tech['price']):,}원")
                                st.write(f"- **최근 1개월 수익률**: {tech['one_month_return']:.2f}%")
                                st.write(f"- **RSI (14)**: {tech['rsi']:.2f}")
                                st.write(f"- **MACD 신호**: {'매수 우위' if tech['macd'] > tech['signal'] else '매도 우위'}")
                                st.write(f"- **이동평균선**: {'20일선 위 (상승추세)' if tech['price'] > tech['sma20'] else '20일선 아래 (조정/하락)'}")
                                if tech.get('bb_pct_b') is not None and pd.notna(tech['bb_pct_b']):
                                    bb_state = "하단 근접(과매도)" if tech['bb_pct_b'] <= 0.2 else ("상단 근접(과열)" if tech['bb_pct_b'] >= 0.8 else "중립")
                                    st.write(f"- **볼린저밴드 %B**: {tech['bb_pct_b']:.2f} ({bb_state}, 하단 {int(tech['bb_lower']):,}원 / 상단 {int(tech['bb_upper']):,}원)")
                                if tech.get('stoch_k') is not None and pd.notna(tech['stoch_k']):
                                    stoch_state = "과매도" if tech['stoch_k'] <= 20 else ("과매수" if tech['stoch_k'] >= 80 else "중립")
                                    st.write(f"- **스토캐스틱 %K/%D**: {tech['stoch_k']:.1f} / {tech['stoch_d']:.1f} ({stoch_state})")
                                if tech.get('weekly_trend'):
                                    st.write(f"- **주간 추세(주봉 5주/20주 이평)**: {tech['weekly_trend']}")
                            with t3:
                                if news:
                                    for i, n in enumerate(news, 1):
                                        st.markdown(f"{i}. [{n['title']}]({n['link']}) ({n['press']})")
                                else:
                                    st.write("최근 관련 뉴스가 없습니다.")

