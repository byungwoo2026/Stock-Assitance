# -*- coding: utf-8 -*-
"""
Streamlit에 의존하지 않는 순수 스크리닝 로직 모음.

app.py(대화형 대시보드, top20만 표시)와 export_tech_scores.py(Stock_Agent 저장소가
교차저장소로 소비하는 전체 풀 export, GitHub Actions 헤드리스 실행)가 이 모듈을 공유해,
"화면에 보이는 기술점수"와 "Stock_Agent STEP4가 필터에 쓰는 기술점수"가 어긋나지 않도록
한다. app.py는 이 함수들을 그대로 import해서 쓴다 — 로직은 원본(app.py 리팩터링 전)과
100% 동일하며, 이 파일 자체는 streamlit을 import하지 않는다(헤드리스 실행 가능해야 하므로).
"""
import threading
from datetime import datetime, timedelta, timezone

import pandas as pd
import FinanceDataReader as fdr
import requests

KST = timezone(timedelta(hours=9))


def now_kst():
    """배포 서버(Streamlit Cloud 등)가 UTC로 동작해도 항상 한국 시간을 반환."""
    return datetime.now(KST)


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


def compute_technical_indicators(df_fdr):
    """일봉 OHLCV(fdr.DataReader 원본 — 날짜 DatetimeIndex, High/Low/Close/Volume 포함)에
    이동평균/RSI/MACD/ATR/볼린저밴드/스토캐스틱/주간추세를 전체 구간에 걸쳐 벡터화 계산해 추가.
    analyze_stock_technical(오늘 스냅샷)과 backtest_technical_signal(과거 전 구간)이 같은 함수를 공유해
    "오늘 화면에 보이는 지표"와 "백테스트가 재현하는 지표"가 어긋나지 않도록 함."""
    df = pd.DataFrame({
        'high': df_fdr['High'],
        'low': df_fdr['Low'],
        'close': df_fdr['Close'],
        'volume': df_fdr['Volume'],
    })

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

    # 볼린저밴드(20일, ±2표준편차) — %B: 0=하단밴드, 1=상단밴드 (구간 밖으로도 벗어날 수 있음)
    bb_std = df['close'].rolling(window=20).std()
    df['BB_UPPER'] = df['SMA20'] + 2 * bb_std
    df['BB_LOWER'] = df['SMA20'] - 2 * bb_std
    bb_range = (df['BB_UPPER'] - df['BB_LOWER']).replace(0, pd.NA)
    df['BB_PCT_B'] = (df['close'] - df['BB_LOWER']) / bb_range

    # 스토캐스틱(%K 14일, %D 3일 이동평균)
    lowest_low = df['low'].rolling(window=14).min()
    highest_high = df['high'].rolling(window=14).max()
    stoch_range = (highest_high - lowest_low).replace(0, pd.NA)
    df['STOCH_K'] = (df['close'] - lowest_low) / stoch_range * 100
    df['STOCH_D'] = df['STOCH_K'].rolling(window=3).mean()

    # 주간 추세: 주봉 5주/20주 이동평균 관계. resample('W')의 라벨은 그 주의 "마지막" 날짜이므로,
    # 아직 끝나지 않은 이번 주 라벨은 일별 날짜보다 항상 미래에 위치 → reindex+ffill 시 자동으로
    # "가장 최근에 확정된 지난 주" 값만 참조되어 미래 데이터 참조(lookahead) 없이 안전하게 정렬됨.
    weekly_close = df['close'].resample('W').last()
    weekly_sma5 = weekly_close.rolling(5).mean()
    weekly_sma20 = weekly_close.rolling(20).mean()
    weekly_trend = pd.Series('혼조', index=weekly_close.index)
    weekly_trend[(weekly_close > weekly_sma5) & (weekly_sma5 > weekly_sma20)] = '상승'
    weekly_trend[weekly_sma5 < weekly_sma20] = '하락'
    weekly_trend[weekly_sma20.isna()] = None
    df['WEEKLY_TREND'] = weekly_trend.reindex(df.index, method='ffill')

    return df


def compute_technical_score_at_row(row):
    """지표 한 행(SMA20/SMA60/MACD/Signal/RSI/BB_PCT_B/STOCH_K/WEEKLY_TREND/close)으로부터
    기술적 타점 점수(100점: 정배열25+MACD20+RSI20+볼린저밴드10+스토캐스틱10+주간추세15)를 계산.
    run_logical_screener(오늘)와 backtest_technical_signal(과거 전 구간)이 채점 로직을 공유해
    "백테스트가 실제로 이 스크리너의 로직을 재현하고 있다"는 것을 보장."""
    price, sma20, sma60 = row['close'], row['SMA20'], row['SMA60']
    macd, sig, rsi = row['MACD'], row['Signal'], row['RSI']
    bb_pct_b, stoch_k, weekly_trend = row.get('BB_PCT_B'), row.get('STOCH_K'), row.get('WEEKLY_TREND')

    score = 0
    trend_str = "역배열/혼조"
    if price > sma20 and sma20 > sma60:
        score += 25
        trend_str = "완벽 정배열 (초강세)"
    elif sma20 > sma60:
        score += 15
        trend_str = "20/60 정배열 (눌림목)"
    elif price > sma20:
        score += 8
        trend_str = "20일선 회복 (반등중)"

    macd_str = "매도 구간"
    if macd > sig:
        score += 20
        macd_str = "매수 우위 (상승세)"
    elif macd > 0:
        score += 7
        macd_str = "조정 중 (0선 위)"

    if pd.notna(rsi):
        if rsi <= 40: score += 20
        elif rsi <= 55: score += 13
        elif rsi <= 70: score += 6
        else: score -= 7

    if pd.notna(bb_pct_b):
        if bb_pct_b <= 0.2: score += 10       # 하단밴드 근접(과매도) — 매수 관심
        elif bb_pct_b <= 0.8: score += 5

    if pd.notna(stoch_k):
        if stoch_k <= 20: score += 10          # 과매도 구간 — 매수 관심
        elif stoch_k <= 80: score += 5

    if weekly_trend == '상승':
        score += 15
    elif weekly_trend == '혼조':
        score += 7

    return score, trend_str, macd_str


def score_full_pool(pool_size=100):
    """KOSPI+KOSDAQ 거래대금 상위 pool_size종목 전량의 기술점수를 계산해 반환한다(정렬/절단 없음).

    2026-09-20 신설 — Stock_Agent(별도 저장소)의 STEP4가 이 점수를 교차저장소로 공급받아
    후보 필터링에 쓰기로 함에 따라, 기존 run_logical_screener()(app.py, top20만 표시)의
    "후보 수집 + 스코어링" 부분을 이 함수로 분리했다. run_logical_screener()와
    export_tech_scores.py(신설, Stock_Agent 공급용)가 이 함수를 공유하므로 두 소비처의
    점수 계산 로직이 어긋날 수 없다.

    top20 슬라이싱과 투자자수급(외국인/기관 연속매매) enrichment는 대화형 화면 전용이라
    이 함수에는 포함하지 않는다(app.py의 run_logical_screener가 이 함수 호출 후 처리) —
    Stock_Agent 쪽은 그 정보가 필요 없고, 전체 풀(최대 100종목)에 대해 수급까지 조회하면
    호출량이 top20 대비 5배로 늘어나 불필요한 부담이 된다.

    Returns:
        [{"code","name","tech_score","trend_str","macd_str","rsi","bb_pct_b","stoch_k",
          "weekly_trend","close","last_date"}, ...] — 점수 미산출(데이터 부족/조회 실패)
          종목은 제외. 조회 자체가 실패하면 빈 리스트.
    """
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
        stocks = [{'code': s['itemCode'], 'name': s['stockName']} for s in pool[:pool_size]]
    except Exception:
        return []

    scored = []
    # 주간추세(20주 이동평균)까지 계산하려면 100영업일보다 더 긴 과거 데이터가 필요해 약 500일 전부터 조회
    start_date = (now_kst() - timedelta(days=500)).strftime('%Y-%m-%d')

    for s in stocks:
        try:
            df_fdr = fdr.DataReader(s['code'], start_date)
            if df_fdr.empty or len(df_fdr) < 60:
                continue

            df = compute_technical_indicators(df_fdr)
            last = df.iloc[-1]
            if pd.isna(last['SMA60']) or pd.isna(last['RSI']) or pd.isna(last['MACD']):
                continue

            score, trend_str, macd_str = compute_technical_score_at_row(last)

            scored.append({
                "code": s['code'],
                "name": s['name'],
                "tech_score": int(score),
                "trend_str": trend_str,
                "macd_str": macd_str,
                "rsi": float(last['RSI']) if pd.notna(last['RSI']) else None,
                "bb_pct_b": float(last['BB_PCT_B']) if pd.notna(last['BB_PCT_B']) else None,
                "stoch_k": float(last['STOCH_K']) if pd.notna(last['STOCH_K']) else None,
                "weekly_trend": last['WEEKLY_TREND'] if pd.notna(last['WEEKLY_TREND']) else None,
                "close": float(last['close']),
                "last_date": df.index[-1].date(),
            })
        except Exception:
            continue

    return scored
