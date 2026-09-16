import streamlit as st
import pandas as pd
import FinanceDataReader as fdr
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime, timedelta
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

# Groq가 특정 모델을 예고 없이 단종시킨 전례가 있어(예: llama-3.3-70b-versatile),
# 최우선 모델이 실패하면 다음 모델로 자동 전환. 가끔 갱신이 필요할 수 있음(2026-09-16 기준 확인).
GROQ_MODEL_CANDIDATES = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

def call_groq_chat(messages, temperature=None, max_retries=2):
    """Groq 채팅 완성 호출 공통 헬퍼.
    모델별로 최대 max_retries회 짧게 재시도하고, 그래도 실패하면 다음 후보 모델로 넘어간다.
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
                last_err = e
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
    return False, str(last_err)

_thread_local = threading.local()

def get_naver_session():
    """스레드별로 재사용되는 requests.Session.
    m.stock.naver.com은 매 요청마다 새 TLS 연결을 맺으면 요청당 약 1~6초가 걸리지만,
    연결을 재사용(keep-alive)하면 이후 요청은 수십 ms로 줄어든다.
    (같은 스레드 안에서 여러 페이지/종목을 순차 조회하는 함수들에서 반드시 이 세션을 사용할 것)
    """
    session = getattr(_thread_local, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _thread_local.session = session
    return session

st.set_page_config(page_title="나만의 투자 조수", layout="wide")

st.title("📈 나만의 AI 투자 조수 대시보드")
st.markdown("시장의 자금 쏠림, 주주 수급, 주요 뉴스 및 기술적 매수 신호를 분석합니다.")

# 화면 상단 가로 버튼(탭)형 메뉴 — st.radio를 버튼처럼 보이도록 CSS로 스타일링
MENU_OPTIONS = ["종합 대시보드", "시장 자금 & 업종 분석", "주요 기업 헤드라인 뉴스", "외인 수급 & 기술적 조건 스크리너", "최우수 애널리스트 추천 종목", "가치재평가주", "퀀트 투자 리스트", "개별종목분석"]

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
    start_date = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')
    
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
    return results[:5], datetime.now().strftime('%H:%M')

@st.cache_data(ttl=3600)
def fetch_top_market_cap(market_type="KOSPI", top_n=20):
    # NOTE(2026-09-16): finance.naver.com의 sise_market_sum.naver 등 메인 시세 페이지가
    # Next.js 기반으로 전면 개편되어 서버 렌더링 HTML이 사라짐 → 모바일 웹이 쓰는
    # m.stock.naver.com JSON API로 대체 (pageSize 최대 100)
    url = f"https://m.stock.naver.com/api/stocks/marketValue/{market_type}?page=1&pageSize={min(top_n, 100)}"
    try:
        res = get_naver_session().get(url, timeout=5)
        data = res.json()
        return [s['stockName'] for s in data.get('stocks', [])], datetime.now().strftime('%H:%M')
    except Exception as e:
        st.error(f"시가총액 데이터 수집 오류: {e}")
        return [], datetime.now().strftime('%H:%M')

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

    return results, datetime.now().strftime('%H:%M')

@st.cache_data(ttl=3600)
def fetch_net_buying_top(investor_type="foreign", market_type="KOSPI", top_n=10):
    gubun = 9000 if investor_type == "foreign" else 1000
    sosok = "01" if market_type == "KOSPI" else "02"  # 반드시 2자리 문자열("01"/"02") — 정수 0/1은 404 발생
    # NOTE(2026-07-27): 겉page(sise_deal_rank.naver)는 실제 데이터가 없는 껍데기이고,
    # 진짜 표 데이터는 iframe으로 별도 로드되는 sise_deal_rank_iframe.naver 에 있음 (개발자도구 Network 탭으로 확인)
    url = f"https://finance.naver.com/sise/sise_deal_rank_iframe.naver?sosok={sosok}&investor_gubun={gubun}&type=buy"

    try:
        res = get_naver_session().get(url, timeout=5)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, 'html.parser')

        # 이 페이지는 "이전 영업일"과 "최근 영업일" 두 날짜의 순매수 상위가 나란히(날짜별 박스 2개) 표시되는 구조.
        # NOTE(2026-09-16): 표 class가 기존 type_5에서 type_1로 변경됨. 날짜 박스(.box_type_ms) 기준으로
        # 가장 마지막(=가장 최근 날짜) 박스 안의 표만 사용.
        date_boxes = soup.select('.box_type_ms')
        target_table = date_boxes[-1].select_one('table.type_1') if date_boxes else None
        if target_table:
            stocks = []
            for a in target_table.find_all('a'):
                href = a.get('href', '')
                if 'main.naver?code=' in href:
                    name = a.text.strip()
                    if name and name not in stocks:
                        stocks.append(name)
                    if len(stocks) >= top_n:
                        break
            return stocks, datetime.now().strftime('%H:%M')

        # 표 구조가 예상과 다르면(방어적 처리), 페이지 전체에서 종목 링크만 모아 상위 top_n개 사용
        stocks = []
        for a in soup.find_all('a'):
            href = a.get('href', '')
            if 'main.naver?code=' in href:
                name = a.text.strip()
                if name and name not in stocks:
                    stocks.append(name)
                if len(stocks) >= top_n:
                    break
        return stocks, datetime.now().strftime('%H:%M')
    except Exception:
        return [], datetime.now().strftime('%H:%M')

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

        return results, datetime.now().strftime('%H:%M')
    except Exception:
        return [], datetime.now().strftime('%H:%M')

@st.cache_data(ttl=3600)
def run_logical_screener():
    """
    개선된 논리적 스크리닝 기법 (점수 기반 랭킹 시스템):
    엄격한 AND 조건(0개 종목 검출 방지) 대신, 주도주(거래량 상위)를 대상으로 기술적 타점 점수(100점 만점)를 매겨 상위 20개를 항상 제시.
    """
    # NOTE(2026-09-16): finance.naver.com/sise/sise_quant.naver가 Next.js 개편으로
    # 서버 렌더링 표를 더 이상 제공하지 않아, m.stock.naver.com 시가총액 API로 KOSPI/KOSDAQ
    # 상위 종목 풀을 모은 뒤 거래대금 기준으로 재정렬해 '거래 활발한 주도주' 상위 100개를 추출.
    session = get_naver_session()
    try:
        pool = []
        for market in ("KOSPI", "KOSDAQ"):
            res = session.get(
                f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page=1&pageSize=100",
                timeout=5
            )
            pool.extend(res.json().get('stocks', []))

        pool.sort(key=lambda s: int(s.get('accumulatedTradingValueRaw') or 0), reverse=True)
        stocks = [{'code': s['itemCode'], 'name': s['stockName']} for s in pool[:100]]
    except Exception:
        return [], datetime.now().strftime('%H:%M')

    scored_stocks = []
    
    # 100 영업일 분량의 데이터를 충분히 확보하기 위해 약 150일 전 날짜부터 조회
    start_date = (datetime.now() - timedelta(days=150)).strftime('%Y-%m-%d')
    
    for s in stocks:
        try:
            df_fdr = fdr.DataReader(s['code'], start_date)
            if df_fdr.empty or len(df_fdr) < 60:
                continue
            
            df_fdr = df_fdr.tail(100) # 최근 100개 데이터 사용
            df = pd.DataFrame({'close': df_fdr['Close']}).reset_index(drop=True)
            
            # 이동평균선
            df['SMA20'] = df['close'].rolling(window=20).mean()
            df['SMA60'] = df['close'].rolling(window=60).mean()
            
            # RSI(14)
            delta = df['close'].diff()
            up = delta.clip(lower=0)
            down = -delta.clip(upper=0)
            ema_up = up.ewm(com=13, adjust=False).mean()
            ema_down = down.ewm(com=13, adjust=False).mean()
            rs = ema_up / ema_down
            df['RSI'] = 100 - (100 / (1 + rs))
            
            # MACD(12, 26, 9)
            exp1 = df['close'].ewm(span=12, adjust=False).mean()
            exp2 = df['close'].ewm(span=26, adjust=False).mean()
            df['MACD'] = exp1 - exp2
            df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
            
            last = df.iloc[-1]
            price = last['close']
            sma20 = last['SMA20']
            sma60 = last['SMA60']
            macd = last['MACD']
            sig = last['Signal']
            rsi = last['RSI']
            
            # 종합 타점 점수 산정 (최대 100점)
            score = 0
            
            # 1. 배열 상태 점수 (최대 40점)
            trend_str = "역배열/혼조"
            if price > sma20 and sma20 > sma60:
                score += 40
                trend_str = "완벽 정배열 (초강세)"
            elif sma20 > sma60:
                score += 25
                trend_str = "20/60 정배열 (눌림목)"
            elif price > sma20:
                score += 15
                trend_str = "20일선 회복 (반등중)"
                
            # 2. MACD 상태 (최대 30점)
            macd_str = "매도 구간"
            if macd > sig:
                score += 30
                macd_str = "매수 우위 (상승세)"
            elif macd > 0:
                score += 10
                macd_str = "조정 중 (0선 위)"
                
            # 3. RSI 상태 (최대 30점)
            if rsi <= 40:
                score += 30 # 강력한 과매도(눌림목) 타점
            elif 40 < rsi <= 55:
                score += 20 # 안정적인 상승 여력
            elif 55 < rsi <= 70:
                score += 10 # 강세 유지
            else:
                score -= 10 # 70 이상 과열권 감점
                
            scored_stocks.append({
                "종목명": s['name'],
                "타점 점수": int(score),
                "배열 상태": trend_str,
                "MACD 신호": macd_str,
                "RSI 지수": round(rsi, 2),
                "현재가": f"{int(price):,}",
                "주요 수급": "시장 주도주(Top 100)"
            })
        except Exception:
            continue
            
    # 점수 높은 순으로 정렬 후 상위 20개 추출
    scored_stocks = sorted(scored_stocks, key=lambda x: x['타점 점수'], reverse=True)
    return scored_stocks[:20], datetime.now().strftime('%H:%M')

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
    start_date = (datetime.now() - timedelta(days=150)).strftime('%Y-%m-%d')
    try:
        df_fdr = fdr.DataReader(code, start_date)
        if df_fdr.empty or len(df_fdr) < 60:
            return None
        
        df_fdr = df_fdr.tail(100) # 최근 100개 데이터 사용

# --- [수정] 거래량 데이터도 함께 수집 ---
        df = pd.DataFrame({
            'high': df_fdr['High'],
            'low': df_fdr['Low'],
            'close': df_fdr['Close'],
            'volume': df_fdr['Volume']
        }).reset_index(drop=True)

        df['SMA20'] = df['close'].rolling(window=20).mean()
        df['SMA60'] = df['close'].rolling(window=60).mean()

        delta = df['close'].diff()
        up = delta.clip(lower=0)
        down = -delta.clip(upper=0)
        ema_up = up.ewm(com=13, adjust=False).mean()
        ema_down = down.ewm(com=13, adjust=False).mean()
        rs = ema_up / ema_down
        df['RSI'] = 100 - (100 / (1 + rs))

        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()

        # ATR(14) — 손절/목표가 산정에 쓰는 변동성 지표. True Range의 Wilder 평활 이동평균.
        prev_close = df['close'].shift(1)
        true_range = pd.concat([
            df['high'] - df['low'],
            (df['high'] - prev_close).abs(),
            (df['low'] - prev_close).abs(),
        ], axis=1).max(axis=1)
        df['ATR'] = true_range.ewm(com=13, adjust=False).mean()

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
        start_date = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')
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
    ts = datetime.now().strftime('%H:%M')
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
    start_date = (datetime.now() - timedelta(days=100)).strftime('%Y-%m-%d')

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

    [관련 최신 뉴스 및 시장 평가]
    {news_text}

    보고서는 가독성 있게 작성되어야 하며, 다음 내용을 포함해 주세요:
    1. **재무 건전성 및 밸류에이션 평가**: 영업이익률/ROE를 통한 수익성 평가, PER/PBR 기준 고평가/저평가 여부 분석.
    2. **차트 및 기술적 흐름 해석**: 이동평균선 추세, RSI 과매도/과열 강도, MACD 신호를 조합한 매수 타점 분석.
    3. **시장 모멘텀 및 뉴스 분석**: 최근 뉴스들의 호재/악재 성격 및 수급 영향 평가.
    4. **최종 AI 투자 전략 & 가이드**: 추천 매매 전략(분할 매수, 관망, 매도 등)과 목표/손절 대응 팁.
    """
    ok, content = call_groq_chat([{"role": "user", "content": prompt}])
    if ok:
        return content
    return f"⚠️ AI 분석 리포트 생성 중 오류가 발생했습니다: {content}"

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
    start_date = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
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
    return results, datetime.now().strftime('%H:%M')

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
    ts = datetime.now().strftime('%H:%M')
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
    if ok:
        return content, kospi_news, semi_news, meta
    return f"⚠️ AI 등락 원인 분석 중 오류가 발생했습니다: {content}", kospi_news, semi_news, meta

@st.cache_data(ttl=3600)  # 1시간 캐싱
def get_market_ai_briefing(kospi_data, kosdaq_data, top_sectors):
    """반환값: (브리핑 텍스트, 근거로 사용한 시장 뉴스 리스트, meta)
    meta = {"ts": "이 분석이 실제로 계산된 시각(HH:MM)", "sentiment": 뉴스 감성 집계 dict 또는 None,
            "foreign_top": 외국인 순매수 상위 종목명 리스트, "institution_top": 기관 순매수 상위 종목명 리스트}"""
    ts = datetime.now().strftime('%H:%M')
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
        return fdr.StockListing('ETF/KR'), datetime.now().strftime('%H:%M')
    except Exception:
        return pd.DataFrame(), datetime.now().strftime('%H:%M')

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
    start_date = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')

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
    start_date = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')

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
    start_date = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')

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
    start_date = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')

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
        st.write("외국인 및 기관이 7일간 연속/집중 매수하는 코스피/코스닥 상위 10개 종목을 도출하고 매수 사유(관련 최신 뉴스)를 분석합니다.")
        
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
                
                kpi_stocks, kpi_ts = fetch_net_buying_top(kpi_type, "KOSPI", 10)
                kdq_stocks, kdq_ts = fetch_net_buying_top(kdq_type, "KOSDAQ", 10)

                st.markdown("---")
                c1, c2 = st.columns(2)
                with c1:
                    st.success(f"코스피 {kospi_investor} 집중 매수 상위 10선")
                    for idx, stock in enumerate(kpi_stocks, 1):
                        news = fetch_headlines_rss(stock)
                        reason = news[0]['title'] if news else "관련 기사 없음 (기술적/패시브 자금 매수 추정)"
                        link = news[0]['link'] if news else "#"
                        st.markdown(f"**{idx}. {stock}**\n- 🔍 **매수 사유 분석**: [{reason}]({link})")
                        st.write("")
                    if kpi_stocks:
                        render_data_ts_caption(kpi_ts, cache_minutes=60)

                with c2:
                    st.success(f"코스닥 {kosdaq_investor} 집중 매수 상위 10선")
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
    
    st.info("""
    💡 **추천 기법 반영 (Scoring & Ranking System)**: 
    엄격한 필터링(AND 조건)으로 인해 시장이 과열되거나 침체되었을 때 종목이 하나도 나오지 않는 현상을 방지합니다. 
    대신, **'가장 트렌드와 일치하는 수급 주도주(거래량 상위 100개)'**를 대상으로 사용자가 요구한 지표(RSI 40이하, MACD 매수, 20/60 정배열)의 충족 여부에 따라 **기술적 타점 점수(100점 만점)**를 매깁니다. 
    점수가 가장 높은 **상위 20개 종목을 각 지표 상태와 함께 리스트업**하여, 투자자가 데이터를 직접 보고 최적의 매수/매도 시점을 검증 및 판단할 수 있도록 고도화했습니다.
    """)
    
    st.markdown("---")
    
    search_btn = st.button("🚀 실시간 타점 랭킹 분석 시작")
    
    if search_btn:
        with st.spinner("시장 주도주 100개의 데이터를 수집하고 기술적 타점 점수를 계산 중입니다. (약 10~20초 소요)..."):
            screener_results, screener_ts = run_logical_screener()

        if screener_results:
            st.success(f"현재 시장에서 가장 기술적 타점이 우수한 상위 {len(screener_results)}개 종목입니다!")
            df_screen = pd.DataFrame(screener_results)
            df_screen.index = range(1, len(df_screen) + 1)
            st.dataframe(df_screen, width='stretch')
            render_data_ts_caption(screener_ts, cache_minutes=60)
        else:
            st.warning("데이터를 수집하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")

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
    # 이동평균 배열(8) + MACD(6) + RSI(6) — run_logical_screener의 배열40:MACD30:RSI30 비율과 동일한 취지 유지
    tech_score = 0
    if tech['price'] > tech['sma20'] and tech['sma20'] > tech['sma60']:
        tech_score += 8   # 완벽 정배열
    elif tech['sma20'] > tech['sma60']:
        tech_score += 5   # 20/60 정배열(눌림목)
    elif tech['price'] > tech['sma20']:
        tech_score += 3   # 20일선 회복(반등중)

    if tech['macd'] > tech['signal']:
        tech_score += 6   # 매수 우위
    elif tech['macd'] > 0:
        tech_score += 2   # 조정 중(0선 위)

    if tech['rsi'] <= 40:
        tech_score += 6   # 과매도(눌림목) 매수 타점
    elif tech['rsi'] <= 55:
        tech_score += 4
    elif tech['rsi'] <= 70:
        tech_score += 2   # 70 초과 과열권은 가점 없음

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
                                    ai_report = get_individual_stock_ai_analysis(fundamentals, tech, news)
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
                            with t3:
                                if news:
                                    for i, n in enumerate(news, 1):
                                        st.markdown(f"{i}. [{n['title']}]({n['link']}) ({n['press']})")
                                else:
                                    st.write("최근 관련 뉴스가 없습니다.")

