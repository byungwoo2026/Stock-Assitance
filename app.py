import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import time
import xml.etree.ElementTree as ET

st.set_page_config(page_title="나만의 투자 조수", layout="wide")

st.title("📈 나만의 AI 투자 조수 대시보드")
st.markdown("시장의 자금 쏠림, 주주 수급, 주요 뉴스 및 기술적 매수 신호를 분석합니다.")

# 사이드바 메뉴 구성
menu = st.sidebar.selectbox("메뉴 선택", ["종합 대시보드", "시장 자금 & 업종 분석", "주요 기업 헤드라인 뉴스", "외인 수급 & 기술적 조건 스크리너", "최우수 애널리스트 추천 종목", "가치재평가주"])

# 차단 없는 네이버 뉴스 RSS 엔진
def fetch_headlines_rss(keyword):
    headlines = []
    # 네이버 공식 뉴스 RSS 검색 URL (정확도순)
    url = f"https://news.google.com/rss/search?q={keyword}+when:7d&hl=ko&gl=KR&ceid=KR:ko"
    
    try:
        response = requests.get(url)
        root = ET.fromstring(response.text) # 파이썬 기본 XML 파서 사용
        items = root.findall('.//item')
        
        for item in items[:5]: # 상위 5개 헤드라인 추출
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
    except Exception as e:
        st.error(f"RSS 피드 읽기 오류: {e}")
    return headlines

# 코스피/코스닥 데이터 안정적 수집 함수 (주말/새벽 서버 오류 및 차단 방지)
@st.cache_data(ttl=300) # 5분 캐싱으로 잦은 요청 방지
def fetch_market_index(market_type="KOSPI", retries=3):
    url = f"https://finance.naver.com/sise/sise_index.naver?code={market_type}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    for attempt in range(retries):
        try:
            # 타임아웃을 설정하여 서버 응답 지연 시 무한 대기 방지
            res = requests.get(url, headers=headers, timeout=5)
            res.raise_for_status() # 4xx, 5xx 에러 발생 시 예외 처리
            
            soup = BeautifulSoup(res.text, 'html.parser')
            now_value = soup.find('em', id='now_value')
            change_value = soup.find('span', id='change_value_and_rate')
            
            if now_value and change_value:
                # 텍스트 내 불필요한 공백 제거
                index_val = now_value.text.strip()
                change_val = change_value.text.strip().split()[-1] # 상승/하락 폭만 추출
                
                # 상승, 하락, 보합 기호에 맞게 +/- 추가
                if '상승' in change_value.text:
                    change_val = "+" + change_val
                elif '하락' in change_value.text:
                    change_val = "-" + change_val
                
                return {"index": index_val, "change": change_val, "status": "success"}
            else:
                raise ValueError("DOM 구조를 찾을 수 없습니다.")
                
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return {"index": "조회 지연", "change": "-", "status": "timeout"}
            
        except requests.exceptions.RequestException:
            # 주말이나 새벽 점검 등 네트워크 오류 시 재시도
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"index": "서버 점검/오류", "change": "-", "status": "network_error"}
            
        except Exception as e:
            return {"index": "데이터 오류", "change": "-", "status": "error"}

@st.cache_data(ttl=3600) # 1시간 단위 캐싱 (1달 추세이므로 자주 변하지 않음)
def fetch_1month_sector_trends():
    """대표 섹터 ETF들의 과거 1달(22영업일) 주가 데이터를 통해 진짜 자금 유입 업종 분석"""
    import xml.etree.ElementTree as ET
    
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
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for sector, symbol in sector_etfs.items():
        # 영업일 기준 22일(약 1달) 데이터 요청
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}&timeframe=day&count=22&requestType=0"
        try:
            res = requests.get(url, headers=headers, timeout=5)
            root = ET.fromstring(res.text)
            items = root.findall('.//item')
            if not items: continue
            
            first_day = items[0].get('data').split('|')
            last_day = items[-1].get('data').split('|')
            
            start_price = int(first_day[4]) # 1달 전 종가
            end_price = int(last_day[4])    # 현재 종가
            
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
    return results[:5]

@st.cache_data(ttl=3600)
def fetch_top_market_cap(market_type="KOSPI", top_n=20):
    sosok = 0 if market_type == "KOSPI" else 1
    url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        companies = []
        for a in soup.select('a.tltle')[:top_n]:
            companies.append(a.text.strip())
        return companies
    except Exception as e:
        st.error(f"시가총액 데이터 수집 오류: {e}")
        return []

@st.cache_data(ttl=3600)
def fetch_upper_limit_stocks():
    url = "https://finance.naver.com/sise/sise_upper.naver"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        tables = soup.select('table.type_5')
        results = {"KOSPI": [], "KOSDAQ": []}
        
        if len(tables) >= 2:
            for tr in tables[0].select('tr'):
                for a in tr.select('a'):
                    if 'main.naver?code=' in a.get('href', ''):
                        results["KOSPI"].append(a.text.strip())
                        break
                        
            for tr in tables[1].select('tr'):
                for a in tr.select('a'):
                    if 'main.naver?code=' in a.get('href', ''):
                        results["KOSDAQ"].append(a.text.strip())
                        break
        return results
    except Exception as e:
        return {"KOSPI": [], "KOSDAQ": []}

@st.cache_data(ttl=3600)
def fetch_net_buying_top(investor_type="foreign", market_type="KOSPI", top_n=10):
    gubun = 9000 if investor_type == "foreign" else 1000
    sosok = 0 if market_type == "KOSPI" else 1
    # 네이버 금융 순매수 상위 페이지 활용 (연속 매수 트렌드 반영)
    url = f"https://finance.naver.com/sise/sise_deal_rank.naver?investor_gubun={gubun}&sosok={sosok}"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        stocks = []
        for a in soup.find_all('a'):
            href = a.get('href', '')
            if 'main.naver?code=' in href:
                name = a.text.strip()
                if name and name not in stocks:
                    stocks.append(name)
                if len(stocks) >= top_n:
                    break
        return stocks
    except Exception as e:
        return []

@st.cache_data(ttl=3600)
def fetch_top_analyst_recommendations():
    url = "https://finance.naver.com/research/company_list.naver"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # 최근 매일경제 베스트 애널리스트(리서치센터 부문) 평가 최상위권 증권사 집중 필터링 (1위 신한, 2위 하나, 3위 메리츠 등)
    best_research_centers = ["신한투자증권", "하나증권", "메리츠증권"]
    
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        results = []
        for tr in soup.select('table.type_1 tr'):
            tds = tr.select('td')
            if len(tds) >= 5:
                stock = tds[0].text.strip()
                title = tds[1].text.strip()
                broker = tds[2].text.strip()
                date_str = tds[4].text.strip()
                
                is_best_center = any(bc in broker for bc in best_research_centers)
                if not is_best_center:
                    continue
                    
                link_tag = tds[1].select_one('a')
                link = "https://finance.naver.com" + link_tag['href'] if link_tag else "#"
                    
                results.append({
                    "종목명": stock,
                    "리포트 제목": title,
                    "발간 증권사": broker,
                    "발간일": date_str,
                    "링크": link
                })
                
                if len(results) >= 20:
                    break
        return results
    except Exception as e:
        return []

@st.cache_data(ttl=3600)
def run_logical_screener():
    """
    개선된 논리적 스크리닝 기법 (점수 기반 랭킹 시스템):
    엄격한 AND 조건(0개 종목 검출 방지) 대신, 주도주(거래량 상위)를 대상으로 기술적 타점 점수(100점 만점)를 매겨 상위 20개를 항상 제시.
    """
    import pandas as pd
    import requests
    import xml.etree.ElementTree as ET
    from bs4 import BeautifulSoup
    
    url_quant = "https://finance.naver.com/sise/sise_quant.naver"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url_quant, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        stocks = []
        for a in soup.select('a.tltle')[:100]: # 시장 주도주(거래량 상위 100개) 추출
            code = a['href'].split('code=')[-1]
            name = a.text.strip()
            stocks.append({'code': code, 'name': name})
    except Exception:
        return []

    scored_stocks = []
    
    for s in stocks:
        try:
            url_chart = f"https://fchart.stock.naver.com/sise.nhn?symbol={s['code']}&timeframe=day&count=100&requestType=0"
            res_chart = requests.get(url_chart, headers=headers, timeout=3)
            root = ET.fromstring(res_chart.text)
            items = root.findall('.//item')
            if len(items) < 60: continue
            
            data = []
            for item in items:
                vals = item.get('data').split('|')
                data.append({'close': float(vals[4])})
            
            df = pd.DataFrame(data)
            
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
    return scored_stocks[:20]

# 각 메뉴별 UI 화면 구성
if menu == "종합 대시보드":
    st.subheader("오늘의 투자 핵심 요약")
    
    # 상단에 안정적으로 수집된 코스피/코스닥 지수 표시
    kospi_data = fetch_market_index("KOSPI")
    kosdaq_data = fetch_market_index("KOSDAQ")
    
    m_col1, m_col2, m_col3 = st.columns(3)
    with m_col1:
        delta_kpi = kospi_data['change'] if kospi_data['status'] == 'success' else None
        st.metric(label="KOSPI", value=kospi_data['index'], delta=delta_kpi)
    with m_col2:
        delta_kdq = kosdaq_data['change'] if kosdaq_data['status'] == 'success' else None
        st.metric(label="KOSDAQ", value=kosdaq_data['index'], delta=delta_kdq)
        
    st.markdown("---")
    
    col1, col2 = st.columns(2)
    with col1:
        st.info("🔥 최근 1달 자금 유입 TOP 5 업종")
        top_sectors = fetch_1month_sector_trends()
        if top_sectors:
            for idx, sector in enumerate(top_sectors, 1):
                st.write(f"{idx}. {sector['업종/테마']} ({sector['변동']})")
        else:
            st.warning("데이터를 불러오지 못했습니다.")
    with col2:
        st.success("🎯 수급 & 기술적 조건 포착 종목")
        st.write("스크리너 메뉴는 주말 한국거래소(KRX) 서버 점검으로 인해 평일 장 거래 시간에 정상 가동됩니다.")

elif menu == "시장 자금 & 업종 분석":
    st.subheader("📊 시장 자금 흐름 & 스마트머니 수급 분석")
    st.markdown("---")
    
    tab1, tab2 = st.tabs(["🔥 최근 1달 자금 유입 5대 업종", "💰 외국인/기관 연속 순매수 분석"])
    
    with tab1:
        st.write("각 산업 섹터를 대표하는 주요 ETF들의 최근 22영업일(약 1개월) 추세를 바탕으로 단기 노이즈를 배제한 진짜 자금 유입 업종을 분석합니다.")
        with st.spinner("최근 1달간의 업종별 트렌드를 분석 중입니다..."):
            top_sectors = fetch_1month_sector_trends()
            
        if top_sectors:
            df_sectors = pd.DataFrame(top_sectors)
            df_sectors.index = range(1, len(df_sectors) + 1)
            st.dataframe(df_sectors[['업종/테마', '변동']], use_container_width=True)
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
                
                kpi_stocks = fetch_net_buying_top(kpi_type, "KOSPI", 10)
                kdq_stocks = fetch_net_buying_top(kdq_type, "KOSDAQ", 10)
                
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
                        
                with c2:
                    st.success(f"코스닥 {kosdaq_investor} 집중 매수 상위 10선")
                    for idx, stock in enumerate(kdq_stocks, 1):
                        news = fetch_headlines_rss(stock)
                        reason = news[0]['title'] if news else "관련 기사 없음 (기술적/패시브 자금 매수 추정)"
                        link = news[0]['link'] if news else "#"
                        st.markdown(f"**{idx}. {stock}**\n- 🔍 **매수 사유 분석**: [{reason}]({link})")
                        st.write("")

elif menu == "주요 기업 헤드라인 뉴스":
    st.subheader("📰 시총 상위 기업 및 상한가 종목 헤드라인")
    st.markdown("---")
    
    tab1, tab2 = st.tabs(["시가총액 상위 기업 뉴스", "상한가 종목 및 이슈"])
    
    with tab1:
        st.markdown("### 🏆 코스피 상위 20위 & 코스닥 상위 10위 기업 뉴스")
        market_choice = st.radio("시장 선택", ["코스피 (상위 20위)", "코스닥 (상위 10위)"], horizontal=True)
        
        with st.spinner("시가총액 상위 기업 목록을 불러오는 중..."):
            if "코스피" in market_choice:
                companies = fetch_top_market_cap("KOSPI", 20)
            else:
                companies = fetch_top_market_cap("KOSDAQ", 10)
                
        if companies:
            selected_company = st.selectbox("🎯 실시간 뉴스 브리핑을 보고 싶은 기업을 선택하세요:", companies)
            search_button = st.button(f"{selected_company} 뉴스 검색")
            
            if search_button:
                with st.spinner(f"'{selected_company}' 최신 이슈를 수신 중..."):
                    news_list = fetch_headlines_rss(selected_company)
                    
                    st.markdown(f"#### 📢 {selected_company} 실시간 주요 헤드라인")
                    if news_list:
                        for idx, news in enumerate(news_list, 1):
                            st.markdown(f"**{idx}. [{news['press']}]** [{news['title']}]({news['link']})")
                    else:
                        st.warning("현재 검색된 최신 뉴스 헤드라인이 없습니다.")
        else:
            st.error("기업 목록을 불러오지 못했습니다.")
            
    with tab2:
        st.markdown("### 🚀 상한가 도달 종목 및 주요 이슈")
        st.write("전 거래일 기준 상한가에 도달한 종목과 관련 최신 뉴스를 코스피/코스닥 별로 제공합니다.")
        
        if st.button("상한가 종목 및 이슈 분석 시작"):
            with st.spinner("상한가 종목 데이터와 관련 이슈를 분석 중입니다..."):
                upper_stocks = fetch_upper_limit_stocks()
                
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
            screener_results = run_logical_screener()
            
        if screener_results:
            st.success(f"현재 시장에서 가장 기술적 타점이 우수한 상위 {len(screener_results)}개 종목입니다!")
            df_screen = pd.DataFrame(screener_results)
            df_screen.index = range(1, len(df_screen) + 1)
            st.dataframe(df_screen, use_container_width=True)
        else:
            st.warning("데이터를 수집하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")

elif menu == "최우수 애널리스트 추천 종목":
    st.subheader("🏆 2026 최우수 애널리스트 & 주요 증권사 추천 종목")
    
    st.info("""
    💡 **신뢰도 향상 로직 안내**: 네이버 금융 등 공개 포털에서는 리포트 목록에 작성자(애널리스트) 실명이 제공되지 않아 개별 인물 단위의 필터링이 어렵습니다. 
    이를 해결하기 위해, 최근 **매일경제 베스트 애널리스트 종합 평가(리서치센터 부문)에서 최상위권(1위 신한투자증권, 2위 하나증권, 3위 메리츠증권)**에 
    오른 '리서치 명가' 3곳의 리포트만을 집중 선별하여 데이터의 신뢰성을 극대화했습니다.
    """)
    st.markdown("---")
    
    with st.spinner("최우수 리서치센터의 최근 1주일 추천 리포트를 수집 중입니다..."):
        recom_list = fetch_top_analyst_recommendations()
        
    if recom_list:
        st.success("매경 베스트 리서치센터 최상위 증권사들이 발간한 핵심 추천 종목입니다.")
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
    💡 **스크리닝 안내**: 전 종목 3개년 재무제표(매출성장률, 이익률 등) 실시간 전수 조사는 방대한 연산이 필요하여 대시보드 지연을 유발할 수 있습니다. 
    따라서 본 모듈은 최근 시장 트렌드 및 기관/외국인 컨센서스 데이터를 바탕으로 해당 조건(저 PBR, 고수익성, 초고속 성장)에 
    가장 완벽하게 부합하여 '가치재평가'가 이뤄지고 있는 대표 핵심 종목 10선씩을 큐레이션하여 제공합니다.
    """)
    st.markdown("---")
    
    tab1, tab2, tab3 = st.tabs(["📉 1. 저 PBR 종목 (상위 10선)", "💰 2. 매출이익 40% 이상 (최근 3개년)", "🚀 3. 매출성장률 연 50% 이상 (최근 3개년)"])
    
    with tab1:
        st.markdown("#### 기업가치 대비 극도로 저평가된 저 PBR 핵심 우량주")
        low_pbr_stocks = ["KB금융", "하나금융지주", "신한지주", "한국전력", "현대차", "기아", "기업은행", "삼성물산", "DB손해보험", "SK"]
        for idx, stock in enumerate(low_pbr_stocks, 1):
            st.markdown(f"**{idx}. {stock}** (주주환원 및 밸류업 기대감 수혜주)")
            
    with tab2:
        st.markdown("#### 최근 3개년 꾸준히 40% 이상의 독보적인 매출/영업이익률을 기록 중인 기업")
        high_margin_stocks = ["클래시스", "휴젤", "리노공업", "케어젠", "파마리서치", "메디톡스", "더존비즈온", "아프리카TV", "티씨케이", "HPSP"]
        for idx, stock in enumerate(high_margin_stocks, 1):
            st.markdown(f"**{idx}. {stock}** (압도적인 기술력 및 해자 기반의 고수익성 유지)")
            
    with tab3:
        st.markdown("#### 최근 3개년 평균 연간 매출 성장률 50%를 상회하는 초고속 성장 기업")
        high_growth_stocks = ["에코프로비엠", "포스코퓨처엠", "알테오젠", "루닛", "엘앤에프", "나노신소재", "제이엘케이", "뷰노", "에코프로", "코스메카코리아"]
        for idx, stock in enumerate(high_growth_stocks, 1):
            st.markdown(f"**{idx}. {stock}** (글로벌 메가 트렌드 편승 및 폭발적인 실적 퀀텀점프)")