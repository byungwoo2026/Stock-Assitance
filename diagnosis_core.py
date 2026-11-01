# -*- coding: utf-8 -*-
"""
Streamlit에 의존하지 않는 'AI 종목 진단' 규칙 엔진.

일봉 OHLCV + 최근 5거래일 외국인/기관 수급으로 한 종목을 체크리스트(추세·패턴·수급·거래량)로
채점해 등급과 행동 문장을 만든다. 기존 calculate_stock_score(RSI가 낮을수록 가점하는 눌림목 철학)와는
철학이 반대(장기 추세 아래에서는 사지 않는다는 추세·패턴 중심)라 점수를 섞지 않고 이 모듈로 분리했다.

규칙(2026-11-01 validate_grades.py: 코스피 1~300위·코스닥 1~100위, 20일 시장대비 초과수익 검증 결과를 반영)
- 점수·등급에 들어가는 핵심 항목은 3개뿐이다 — 유니버스·기간을 바꿔도 부호가 일관됐던 신호:
    매수: 종가>240일선(게이트: 실패 시 등급 상한 C) · 외국인+기관 5일 순매수 · 거래량 직전 20일 평균의 2배↑ & 종가 상승
    매도: 위의 거울상(종가<240일선 · 순매도 · 거래량 2배↑ & 종가 하락)
- 10일선·20일선 기울기·정배열(5>10>20>60일선)·패턴(박스권 돌파, 쌍바닥, 역헤드앤숄더, 약세 패턴)은
  '참고 신호'(Check.scored=False)로 화면에 보여주되 점수에는 넣지 않는다 — 유니버스마다 부호가 바뀌거나 오히려 음(-)이었다
  (쌍바닥 t≈-4, 박스권 돌파 3/3 이평 위에서도 음).
- 보유 진단의 물타기는 종가가 240일선·10일선 위이고 수급이 순매수일 때만 허용
- 6~2개월 모멘텀(240일선 위 & 6~2개월 전 수익률이 지수 대비 +20%p↑)은 매수 진단의 참고 신호다(validate_confirm.py).
  발견 표본(2020~)에서는 60일 +5.2%p(t 3.2)였지만, 미리 정한 기준으로 본 독립 구간(2006~2014)에서는 +0.7%p(t 0.7),
  (2015~2019) +1.7%p(t 1.9)로 방향만 같고 유의하지 않아 점수에는 넣지 않았다.
  종가>240일선은 같은 독립 구간(2006~2014)에서 60일 +2.1%p(t 2.1)로 통과했지만 2015~2019년에는 효과가 없었다(−0.2%p) → 장세 의존.

설계 원칙
- 판정·등급·행동 문장은 100% 규칙 기반(같은 입력 → 같은 결과). LLM은 이 모듈에 들어오지 않는다.
- 미래 참조 금지: 모든 지표는 rolling/ewm(과거만 참조)이고, 지그재그의 마지막 피벗은 '미확정'으로 표시한다.
  → as_of 이전 데이터만 잘라 넣으면 그 시점에 실제로 볼 수 있었던 진단이 재현된다(등급 검증용).
- 약세 패턴(쌍봉/헤드앤숄더/박스권 이탈)은 강세 패턴 감지기에 sign=-1을 주어 같은 코드를 재사용한다.
- 수치 기준은 모듈 상단 상수로 모아 두었다. 등급 검증(validate_grades.py)으로 조정할 값들이다.

screener_core.py처럼 streamlit을 import하지 않으므로 테스트·백테스트·Stock_Agent에서 재사용할 수 있다.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd
import FinanceDataReader as fdr

from screener_core import now_kst, get_naver_session, compute_technical_indicators

# ---------------------------------------------------------------------------
# 튜닝 상수
# ---------------------------------------------------------------------------
ZIGZAG_DEVIATION = 0.05      # 지그재그 피벗 확정에 필요한 반대방향 되돌림 폭(5%)
VOL_AVG_DAYS = 20            # 거래량 기준선: 직전 20일 평균(오늘 제외)
VOL_SPIKE_MULT = 2.0         # 거래량 항목: 직전 20일 평균의 2배 이상. 전일비 150%는 분모가 하루치라 노이즈가 커 검증에서 유의하지 않았다
MA20_SLOPE_BARS = 5          # 20일선 우상향 = 오늘 MA20 > 5거래일 전 MA20
ALIGN_WINDOWS = (5, 10, 20, 60)   # 정배열 = 5>10>20>60일선 (역배열은 반대)
BOX_BONUS_MAS = (20, 60, 240)     # 박스권 돌파 참고 신호에 '이 이평선들 위인지'를 함께 표시(점수 미반영)
SCORED_KEYS = ("ma240", "flow", "volume")   # 점수·등급에 들어가는 항목(매수·매도 공통). validate_grades.py가 같은 키로 재계산
FLOW_DAYS = 5                # 수급 합산 거래일 수(네이버 API가 제공하는 최대치)
MOM_NEAR_BARS = 42           # 6~2개월 모멘텀: 약 2개월(42거래일) 전 종가 / 약 6개월(126거래일) 전 종가 - 1
MOM_FAR_BARS = 126
MOM_REL_THRESHOLD = 0.20     # 지수 대비 +20%p. 발견 표본에서 '합산 상위 20% 컷오프'의 날짜별 중앙값(0.179)을 반올림한 값이며 독립 구간 검증에서도 조정하지 않았다
INDEX_SYMBOLS = {"KOSPI": "KS11", "KOSDAQ": "KQ11"}

RECENT_BARS = 20             # 돌파가 이 봉 수 이내일 때만 '완성'으로 인정
NECK_NEAR = 0.03             # 넥라인까지 3% 이내로 접근하면 '형성중'
DB_TOLERANCE = 0.04          # 쌍바닥/쌍봉: 두 극점 가격차 ±4% 이내
DB_NECK_RISE = 0.08          # 쌍바닥/쌍봉: 넥라인이 극점 대비 8% 이상 벗어나 있어야 함
DB_MIN_GAP = 10              # 쌍바닥/쌍봉: 두 극점 간격(봉)
HS_SHOULDER_TOLERANCE = 0.10  # 헤드앤숄더: 두 어깨 가격차 10% 이내
HS_HEAD_DEPTH = 0.03         # 헤드앤숄더: 머리가 양 어깨보다 3% 이상 더 극단적
HS_MIN_GAP = 5               # 헤드앤숄더: 어깨-머리 간격(봉)
BOX_WINDOWS = (60, 40, 20)   # 박스권 후보 기간(봉), 긴 것부터 검사
BOX_MIN_RANGE = 0.05         # 박스 폭이 5% 미만이면 그냥 횡보(의미 있는 박스가 아님)
BOX_MAX_RANGE = 0.20         # 박스 상단/하단 폭 20% 이내
BOX_TIGHT_RANGE = 0.12       # 품질 가점: 12% 이내면 촘촘한 박스
BOX_RECENT = 3               # 박스 돌파는 최근 3봉 이내
BOX_TOUCH_TOLERANCE = 0.02   # 상/하단 2% 이내면 터치로 센다
BREAKOUT_VOL_MULT = 1.5      # 돌파일 거래량 ≥ 직전 평균의 1.5배면 품질 가점

HOLD_STOP_PCT = 0.08         # 보유 진단: 평단 -8% 손절선
HOLD_TAKE_PROFIT_PCT = 0.20  # 보유 진단: +20% 이상이면 익절 검토 구간
TRAIL_ATR_MULT = 3.0         # 익절 트레일링: 최근 60봉 고점 - 3×ATR

# (통과율 % 하한, 등급). 수급처럼 평가 불가 항목이 있어 분모가 달라져도 비율로 환산된다.
# 6단계(A~D, +)는 통계적으로 구분되지 않아 4단계로 줄였다. 하한값은 사전에 정한 잠정값이다:
# 2026-11-01 validate_grades.py(코스피 1~300위·코스닥 1~100위)에서 조정용 기간에 유의한 최적 컷오프가 없어 조정하지 않았고,
# 점수 전체의 순위 예측력(IC)도 확인되지 않았다(코스피 101~300위는 오히려 역방향). 등급은 점검표로만 쓸 것.
GRADE_TABLE = [(85, "A"), (65, "B"), (40, "C"), (0, "D")]
GRADE_VERDICT = {"A": "매수 고려", "B": "매수 고려", "C": "지켜보기", "D": "사지 않기"}
GATE_GRADE_CAP = "C"         # 종가가 240일선 아래이면 등급을 이 이하로 제한(→ 지켜보기 이하)
SELL_THRESHOLDS = (60, 20)   # 매도 신호 통과율 % : ≥60 매도 고려, ≥20 지켜보기, 그 미만 보유 유지

MODES = ("매수", "보유", "매도")
CIRCLED = "①②③④⑤⑥⑦⑧⑨"

PATTERN_NAMES = {
    ("double", 1): "쌍바닥", ("double", -1): "쌍봉",
    ("hs", 1): "역헤드앤숄더", ("hs", -1): "헤드앤숄더",
    ("box", 1): "박스권 돌파", ("box", -1): "박스권 이탈",
}


# ---------------------------------------------------------------------------
# 데이터 구조
# ---------------------------------------------------------------------------
@dataclass
class Pivot:
    i: int                 # df 내 위치
    date: pd.Timestamp
    price: float           # 'H'는 고가, 'L'은 저가 기준
    kind: str              # 'H' | 'L'
    confirmed: bool        # 마지막 피벗은 아직 반대 방향 되돌림이 없어 미확정


@dataclass
class Pattern:
    name: str
    direction: str                 # 'bull' | 'bear'
    status: str                    # '형성중' | '완성'
    quality: str                   # 'A' | 'B' | 'C'
    points: list                   # [(번호, 날짜, 가격, 'top'|'bottom')]
    neckline: float                # as_of 시점의 넥라인(박스는 상단/하단) 가격
    neck_line: list                # [(날짜, 가격), (날짜, 가격)] 차트에 그릴 넥라인
    start: pd.Timestamp
    end: pd.Timestamp
    zone_low: float
    zone_high: float
    anchor_i: int = 0              # 정렬용: 패턴 마지막 기저 극점(또는 돌파봉) 위치

    @property
    def title(self):
        return f"{self.name} {self.status} · 품질 {self.quality}"


@dataclass
class Check:
    axis: str                      # '추세' | '패턴' | '수급' | '거래량' | '손익'
    label: str
    passed: Optional[bool]         # None = 평가 불가(데이터 없음) → 총점 분모에서 제외
    detail: str = ""
    key: str = ""                  # 검증 스크립트가 항목을 식별하는 고정 키(label은 문구가 바뀔 수 있음)
    scored: bool = True            # False = 참고 신호(화면에는 보이지만 점수·등급에는 들어가지 않음)


@dataclass
class ModeResult:
    mode: str
    verdict: str
    grade: Optional[str]
    passed: int                    # 점수 반영 항목 중 통과 개수
    total: int                     # 점수 반영 항목 중 평가 가능한 개수
    checks: list
    action: str
    score_text: str
    extra: dict = field(default_factory=dict)


@dataclass
class Diagnosis:
    code: str
    as_of: pd.Timestamp
    df: pd.DataFrame
    flow_net: Optional[dict]       # {'foreign','organ','sum','days'} 또는 None
    results: dict                  # {'매수': ModeResult, '보유': ..., '매도': ...}
    pillars: dict                  # {'추세': {'state','detail'}, ...}
    patterns: dict                 # {'bull': [...], 'bear': [...]} (품질·완성 순 정렬)
    risk: Optional[dict]           # {'stop','target'}

    def overlay_patterns(self, mode):
        """차트에 그릴 패턴 1개 — 모드 방향을 우선하고, 없으면 반대 방향의 최상위 패턴."""
        bull, bear = self.patterns["bull"], self.patterns["bear"]
        order = [bear, bull] if mode == "매도" else [bull, bear]
        if mode == "보유" and bull and bear:
            return [max(bull[0], bear[0], key=_pattern_rank)]
        for lst in order:
            if lst:
                return [lst[0]]
        return []


# ---------------------------------------------------------------------------
# 데이터 조회
# ---------------------------------------------------------------------------
def fetch_ohlcv(code, lookback_days=700):
    """일봉 OHLCV. MA240까지 그리려면 영업일 240개 이상이 필요해 기본 700일(약 480영업일)을 조회."""
    start_date = (now_kst() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return fdr.DataReader(code, start_date)


def _parse_qty(value):
    """네이버 API의 '+1,660,246' 형태 순매수 수량 문자열을 정수로 변환."""
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def fetch_investor_flow(code):
    """최근 5거래일 외국인/기관 순매수 수량(최신순). 조회 실패 시 None.
    [{'date': Timestamp, 'foreign': int, 'organ': int}, ...]"""
    try:
        res = get_naver_session().get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=5)
        if res.status_code != 200:
            return None
        infos = res.json().get("dealTrendInfos") or []
        flow = []
        for d in infos:
            date = pd.to_datetime(str(d.get("bizdate")), format="%Y%m%d", errors="coerce")
            if pd.isna(date):
                continue
            flow.append({
                "date": date,
                "foreign": _parse_qty(d.get("foreignerPureBuyQuant")),
                "organ": _parse_qty(d.get("organPureBuyQuant")),
            })
        flow.sort(key=lambda x: x["date"], reverse=True)
        return flow or None
    except Exception:
        return None


def flow_net(flow, as_of_date, days=FLOW_DAYS):
    """as_of_date까지의 최근 days거래일 외국인/기관 순매수 합. 5일치가 다 없거나 너무 오래됐으면 None
    (네이버가 최근 5거래일만 제공하므로 과거 시점 진단에서는 수급 항목이 '평가 불가'가 된다)."""
    if not flow:
        return None
    as_of_date = pd.Timestamp(as_of_date).normalize()
    eligible = [f for f in flow if f["date"] <= as_of_date][:days]
    if len(eligible) < days:
        return None
    if (as_of_date - eligible[0]["date"]).days > 4:   # 주말·연휴를 감안해도 최신 수급일이 너무 오래됨
        return None
    foreign = sum(f["foreign"] for f in eligible)
    organ = sum(f["organ"] for f in eligible)
    return {"foreign": foreign, "organ": organ, "sum": foreign + organ, "days": days}


# ---------------------------------------------------------------------------
# 지표
# ---------------------------------------------------------------------------
def _clean_ohlcv(df_fdr):
    """거래정지일 등에 시가/고가/저가가 0(또는 결측)으로 들어오는 행은 종가로 대체하고, 종가가 없는 행은 버린다.
    행 단위 처리라 미래 참조가 생기지 않는다."""
    df = df_fdr[df_fdr["Close"] > 0].copy()
    for col in ("Open", "High", "Low"):
        if col in df.columns:
            df[col] = df[col].where(df[col] > 0, df["Close"])
    return df


def fetch_index_close(market, lookback_days=700):
    """종목이 속한 시장 지수(코스피=KS11, 코스닥=KQ11)의 종가 시리즈. 시장을 모르거나 조회에 실패하면 None."""
    sym = INDEX_SYMBOLS.get(str(market).upper())
    if sym is None:
        return None
    start_date = (now_kst() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        close = fdr.DataReader(sym, start_date)["Close"].astype(float).dropna()
    except Exception:
        return None
    return close if len(close) else None


def _momentum_62(close):
    return close.shift(MOM_NEAR_BARS) / close.shift(MOM_FAR_BARS) - 1


def prepare(df_fdr, index_close=None):
    """screener_core.compute_technical_indicators(ATR·SMA20 등)에 5/10/240일선, 거래량 전일비,
    6~2개월 모멘텀(MOM62)과 지수 대비 값(REL62)을 추가. index_close(시장 지수 종가 시리즈)가 없으면 REL62는 NaN.
    지수의 모멘텀은 지수 자신의 거래일 기준으로 계산한 뒤 종목 거래일에 맞춘다(최대 5일 이월) — 모두 과거만 참조한다."""
    df_fdr = _clean_ohlcv(df_fdr)
    df = compute_technical_indicators(df_fdr)
    df["MOM62"] = _momentum_62(df["close"])
    df["REL62"] = np.nan
    if index_close is not None and len(index_close) > MOM_FAR_BARS:
        idx_mom = _momentum_62(index_close.astype(float).sort_index()).reindex(df.index, method="ffill", limit=5)
        df["REL62"] = df["MOM62"] - idx_mom
    df["open"] = df_fdr["Open"] if "Open" in df_fdr.columns else df["close"]
    df["MA5"] = df["close"].rolling(5).mean()
    df["MA10"] = df["close"].rolling(10).mean()
    df["MA20"] = df["SMA20"]
    df["MA60"] = df["SMA60"]
    df["MA240"] = df["close"].rolling(240).mean()
    # 거래량 배수 = 오늘 거래량 / 직전 20일 평균(오늘 제외 → 오늘의 폭증이 기준선을 끌어올리지 않는다)
    vol_avg = df["volume"].rolling(VOL_AVG_DAYS).mean().shift(1).replace(0, np.nan)
    df["VOL_MULT"] = df["volume"] / vol_avg
    return df


def risk_levels(close, atr):
    """ATR 기반 참고 손절/목표가(app.py compute_risk_levels와 동일 공식: -2×ATR / +3×ATR)."""
    if atr is None or pd.isna(atr) or atr <= 0:
        return None
    return {"stop": close - 2 * atr, "target": close + 3 * atr}


# ---------------------------------------------------------------------------
# 지그재그 피벗
# ---------------------------------------------------------------------------
def zigzag_pivots(df, deviation=ZIGZAG_DEVIATION):
    """고가/저가 기준 지그재그. 고점·저점이 번갈아 나오며, 반대 방향으로 deviation 이상 되돌린
    시점에 직전 극점이 확정된다. 마지막 피벗은 아직 되돌림이 없으므로 confirmed=False."""
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    idx = df.index
    n = len(df)
    out = []
    if n < 2:
        return out

    def mk(i, price, kind, confirmed=True):
        return Pivot(i, idx[i], float(price), kind, confirmed)

    hi_i = lo_i = 0
    direction = 0   # 0=미정, 1=고점을 추적 중(직전 피벗이 저점), -1=저점을 추적 중
    for i in range(1, n):
        if direction == 0:
            if highs[i] > highs[hi_i]:
                hi_i = i
            if lows[i] < lows[lo_i]:
                lo_i = i
            if lo_i < hi_i and highs[hi_i] >= lows[lo_i] * (1 + deviation):
                out.append(mk(lo_i, lows[lo_i], "L"))
                direction = 1
            elif hi_i < lo_i and lows[lo_i] <= highs[hi_i] * (1 - deviation):
                out.append(mk(hi_i, highs[hi_i], "H"))
                direction = -1
        elif direction == 1:
            if highs[i] > highs[hi_i]:
                hi_i = i
            elif lows[i] <= highs[hi_i] * (1 - deviation):
                out.append(mk(hi_i, highs[hi_i], "H"))
                direction = -1
                lo_i = i
        else:
            if lows[i] < lows[lo_i]:
                lo_i = i
            elif highs[i] >= lows[lo_i] * (1 + deviation):
                out.append(mk(lo_i, lows[lo_i], "L"))
                direction = 1
                hi_i = i
    if direction == 1:
        out.append(mk(hi_i, highs[hi_i], "H", confirmed=False))
    elif direction == -1:
        out.append(mk(lo_i, lows[lo_i], "L", confirmed=False))
    return out


# ---------------------------------------------------------------------------
# 패턴 감지 (sign=+1 강세: 쌍바닥/역헤드앤숄더/박스권 돌파, sign=-1 약세: 거울상)
# ---------------------------------------------------------------------------
def _pattern_rank(p):
    return (p.status == "완성", {"A": 3, "B": 2, "C": 1}[p.quality], p.anchor_i)


def _quality(missing):
    return "A" if missing == 0 else ("B" if missing == 1 else "C")


def _vol_around(vol, i, r=2):
    return float(np.mean(vol[max(0, i - r): i + r + 1]))


def _breakout_vol_ok(vol, j, lookback=20):
    if j < lookback:
        return False
    return vol[j] >= BREAKOUT_VOL_MULT * float(np.mean(vol[j - lookback: j]))


def _broke_base(df, sign, from_i, base_price):
    """기저 극점(저점/고점) 이후 그 극점을 1% 넘게 깨는 움직임이 있었는가 → 패턴 무효."""
    if sign > 0:
        return float(df["low"].to_numpy(dtype=float)[from_i:].min()) < base_price * 0.99
    return float(df["high"].to_numpy(dtype=float)[from_i:].max()) > base_price * 1.01


def _resolve_status(close, sign, anchor_i, neck_fn, t):
    """anchor_i(마지막 기저 극점) 이후의 종가 경로로 패턴 상태를 판정.
    반환 (status, breakout_i) / 해당 없으면 None.
    - 완성: 넥라인을 종가로 돌파한 지 RECENT_BARS 이내이고 아직 넥라인 바깥에 있음
    - 형성중: 아직 돌파 전이며 넥라인 NECK_NEAR(3%) 이내로 접근
    돌파 후 넥라인 안쪽으로 되돌아왔거나(실패) 돌파가 오래됐으면 현재 신호가 아니므로 None."""
    brk = None
    for j in range(anchor_i + 1, t + 1):
        if sign * (close[j] - neck_fn(j)) > 0:
            brk = j
            break
    if brk is not None:
        if t - brk > RECENT_BARS:
            return None
        if sign * (close[t] - neck_fn(t)) < 0:
            return None
        return "완성", brk
    neck_now = neck_fn(t)
    gap = sign * (neck_now - close[t]) / neck_now
    if 0 <= gap <= NECK_NEAR:
        return "형성중", None
    return None


def _pos(kind):
    return "top" if kind == "H" else "bottom"


def _make_pattern(kind_key, sign, status, quality, pts, neck_now, neck_line, start, end, anchor_i):
    """pts: [(날짜, 가격, 'top'|'bottom')] → 번호를 순서대로 붙여 Pattern 생성."""
    points = [(n + 1, d, p, pos) for n, (d, p, pos) in enumerate(pts)]
    prices = [p for _, _, p, _ in points] + [neck_now]
    return Pattern(
        name=PATTERN_NAMES[(kind_key, sign)],
        direction="bull" if sign > 0 else "bear",
        status=status, quality=quality, points=points,
        neckline=float(neck_now), neck_line=neck_line,
        start=start, end=end,
        zone_low=float(min(prices)), zone_high=float(max(prices)),
        anchor_i=anchor_i,
    )


def _detect_double(df, pivots, sign):
    """쌍바닥(sign=1)/쌍봉(sign=-1): 기저 극점 2개가 ±4% 이내, 사이 넥라인이 극점에서 8% 이상,
    극점 간격 10봉 이상. 종가의 넥라인 돌파 여부로 완성/형성중 판정."""
    base_kind, neck_kind = ("L", "H") if sign > 0 else ("H", "L")
    close = df["close"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    idx = df.index
    t = len(df) - 1
    out = []
    for k in range(max(0, len(pivots) - 9), len(pivots) - 2):
        b1, nk, b2 = pivots[k], pivots[k + 1], pivots[k + 2]
        if (b1.kind, nk.kind, b2.kind) != (base_kind, neck_kind, base_kind):
            continue
        if abs(b2.price - b1.price) / b1.price > DB_TOLERANCE:
            continue
        base_ref = max(b1.price, b2.price) if sign > 0 else min(b1.price, b2.price)
        if sign * (nk.price - base_ref) / base_ref < DB_NECK_RISE:
            continue
        if b2.i - b1.i < DB_MIN_GAP or _broke_base(df, sign, b2.i, b2.price):
            continue
        neck = nk.price
        state = _resolve_status(close, sign, b2.i, lambda j: neck, t)
        if state is None:
            continue
        status, brk = state

        missing = 0
        if abs(b2.price - b1.price) / b1.price > 0.02:           # 좌우 대칭
            missing += 1
        if _vol_around(vol, b2.i) >= _vol_around(vol, b1.i):      # 두 번째 극점에서 거래량 감소
            missing += 1
        if brk is not None and not _breakout_vol_ok(vol, brk):    # 돌파일 거래량
            missing += 1

        pts = []
        if k > 0:
            pre = pivots[k - 1]
            pts.append((pre.date, pre.price, _pos(pre.kind)))
        pts += [(b1.date, b1.price, _pos(b1.kind)), (nk.date, nk.price, _pos(nk.kind)),
                (b2.date, b2.price, _pos(b2.kind))]
        if brk is not None:
            pts.append((idx[brk], float(close[brk]), "top" if sign > 0 else "bottom"))
        start = pts[0][0]
        out.append(_make_pattern(
            "double", sign, status, _quality(missing), pts, neck,
            [(nk.date, neck), (idx[t], neck)], start, idx[t], brk if brk is not None else b2.i))
    return out


def _detect_hs(df, pivots, sign):
    """역헤드앤숄더(sign=1)/헤드앤숄더(sign=-1): 기저 극점 3개(왼쪽 어깨·머리·오른쪽 어깨)와 극점 사이
    넥라인 2점. 머리가 양 어깨보다 3% 이상 극단적이고 어깨 가격차 10% 이내. 넥라인은 두 점을 잇는 직선."""
    base_kind, neck_kind = ("L", "H") if sign > 0 else ("H", "L")
    close = df["close"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    idx = df.index
    t = len(df) - 1
    n = len(pivots)
    out = []
    for k in range(max(0, n - 11), n - 4):
        ls, n1, head, n2, rs = pivots[k:k + 5]
        if tuple(p.kind for p in (ls, n1, head, n2, rs)) != (base_kind, neck_kind, base_kind, neck_kind, base_kind):
            continue
        if sign * (ls.price - head.price) / ls.price < HS_HEAD_DEPTH:
            continue
        if sign * (rs.price - head.price) / rs.price < HS_HEAD_DEPTH:
            continue
        if abs(rs.price - ls.price) / ls.price > HS_SHOULDER_TOLERANCE:
            continue
        if head.i - ls.i < HS_MIN_GAP or rs.i - head.i < HS_MIN_GAP or n2.i <= n1.i:
            continue
        if _broke_base(df, sign, rs.i, rs.price):
            continue
        slope = (n2.price - n1.price) / (n2.i - n1.i)
        neck_fn = lambda j, a=n1, s=slope: a.price + s * (j - a.i)   # noqa: E731 — 루프 안에서 즉시 사용
        state = _resolve_status(close, sign, rs.i, neck_fn, t)
        if state is None:
            continue
        status, brk = state

        missing = 0
        if abs(rs.price - ls.price) / ls.price > 0.05:            # 어깨 대칭
            missing += 1
        if _vol_around(vol, rs.i) >= _vol_around(vol, head.i):    # 오른쪽 어깨 거래량이 머리보다 줄어듦
            missing += 1
        if brk is not None and not _breakout_vol_ok(vol, brk):
            missing += 1

        pts = [(p.date, p.price, _pos(p.kind)) for p in (ls, n1, head, n2, rs)]
        if brk is not None:
            pts.append((idx[brk], float(close[brk]), "top" if sign > 0 else "bottom"))
        out.append(_make_pattern(
            "hs", sign, status, _quality(missing), pts, neck_fn(t),
            [(n1.date, n1.price), (idx[t], neck_fn(t))], ls.date, idx[t], brk if brk is not None else rs.i))
    return out


def _count_touches(mask, min_gap=3):
    """연속으로 붙은 터치는 하나로 합쳐 센다."""
    count, last = 0, -10 ** 9
    for i in np.flatnonzero(mask):
        if i - last >= min_gap:
            count += 1
        last = i
    return count


def _detect_box(df, sign):
    """박스권 돌파(sign=1)/이탈(sign=-1): 직전 W봉의 고저 폭이 20% 이내이고 상·하단을 각각 2회 이상
    터치한 박스를, 최근 3봉 이내에 종가로 처음 벗어남(완성) / 벗어나기 직전 경계 3% 이내(형성중)."""
    n = len(df)
    t = n - 1
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    idx = df.index

    def box_ok(top, bot, hs, ls):
        if not (BOX_MIN_RANGE <= (top - bot) / bot <= BOX_MAX_RANGE):
            return False
        ceil_touch = _count_touches(hs >= top * (1 - BOX_TOUCH_TOLERANCE))
        floor_touch = _count_touches(ls <= bot * (1 + BOX_TOUCH_TOLERANCE))
        return ceil_touch >= 2 and floor_touch >= 2

    for W in BOX_WINDOWS:
        if n < W + BOX_RECENT + 1:
            continue
        # 1) 완성 — 돌파(이탈)봉 j 직전 W봉이 박스
        for b in range(BOX_RECENT):
            j = t - b
            hs, ls = high[j - W:j], low[j - W:j]
            top, bot = float(hs.max()), float(ls.min())
            ref = top if sign > 0 else bot
            if sign * (close[j] - ref) <= 0 or sign * (close[t] - ref) < 0:
                continue
            if not box_ok(top, bot, hs, ls):
                continue
            missing = 0
            if (top - bot) / bot > BOX_TIGHT_RANGE:
                missing += 1
            if W < 40:
                missing += 1
            if vol[j] < BREAKOUT_VOL_MULT * float(np.mean(vol[j - W:j])):
                missing += 1
            s_i = j - W
            pts = [(idx[s_i + int(np.argmin(ls))], bot, "bottom"),
                   (idx[s_i + int(np.argmax(hs))], top, "top"),
                   (idx[j], float(close[j]), "top" if sign > 0 else "bottom")]
            return [_make_pattern("box", sign, "완성", _quality(missing), pts, ref,
                                  [(idx[s_i], ref), (idx[t], ref)], idx[s_i], idx[t], j)]
        # 2) 형성중 — 아직 박스 안, 돌파 경계 3% 이내
        hs, ls = high[t - W + 1:t + 1], low[t - W + 1:t + 1]
        top, bot = float(hs.max()), float(ls.min())
        ref = top if sign > 0 else bot
        gap = sign * (ref - close[t]) / ref
        if 0 <= gap <= NECK_NEAR and box_ok(top, bot, hs, ls):
            missing = (1 if (top - bot) / bot > BOX_TIGHT_RANGE else 0) + (1 if W < 40 else 0)
            s_i = t - W + 1
            pts = [(idx[s_i + int(np.argmin(ls))], bot, "bottom"),
                   (idx[s_i + int(np.argmax(hs))], top, "top")]
            return [_make_pattern("box", sign, "형성중", _quality(missing), pts, ref,
                                  [(idx[s_i], ref), (idx[t], ref)], idx[s_i], idx[t], t)]
    return []


def detect_patterns(df, pivots=None, sign=1):
    """df 마지막 봉 기준으로 유효한 패턴을 품질·완성 순으로 정렬해 반환(없으면 빈 리스트)."""
    if len(df) < 30:
        return []
    if pivots is None:
        pivots = zigzag_pivots(df)
    found = _detect_double(df, pivots, sign) + _detect_hs(df, pivots, sign) + _detect_box(df, sign)
    found.sort(key=_pattern_rank, reverse=True)
    return found


# ---------------------------------------------------------------------------
# 체크리스트 / 등급 / 판정
# ---------------------------------------------------------------------------
GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}
SIGNAL_ORDER = ("매수 고려", "지켜보기", "사지 않기", "매도 고려")   # 관심종목 필터용 4분류


def grade_from_counts(passed, total):
    """통과 개수/평가 가능 항목 수 → 등급(정수 연산이라 경계 오차가 없다). 평가 가능한 항목이 없으면 None."""
    if total <= 0:
        return None
    for floor_pct, grade in GRADE_TABLE:
        if passed * 100 >= total * floor_pct:
            return grade
    return GRADE_TABLE[-1][1]


def _apply_gate(grade, gate_ok):
    """게이트(종가>240일선) 실패 시 등급 상한을 GATE_GRADE_CAP으로 낮춘다 → 등급과 판정이 항상 일치."""
    if grade is None or gate_ok is not False:
        return grade
    order = [g for _, g in GRADE_TABLE]
    return GATE_GRADE_CAP if order.index(grade) < order.index(GATE_GRADE_CAP) else grade


def _won(x):
    return f"{x:,.0f}원"


def _ctx(df):
    row, prev = df.iloc[-1], df.iloc[-2]
    k = MA20_SLOPE_BARS
    return {
        "close": float(row["close"]),
        "prev_close": float(prev["close"]),
        "ma5": row["MA5"], "ma10": row["MA10"], "ma20": row["MA20"],
        "ma60": row["MA60"], "ma240": row["MA240"],
        "ma20_prev": df["MA20"].iloc[-1 - k] if len(df) > k else np.nan,
        "vol_mult": row["VOL_MULT"],
        "atr": row["ATR"],
        "mom62": row["MOM62"], "rel62": row["REL62"],
    }


# passed는 numpy bool이 아닌 순수 bool이어야 한다(호출부가 `is False`로 '실패'와 '평가 불가(None)'를 구분)
def _price_vs_ma(c, w, sign, key, scored=True):
    up = sign > 0
    label = f"종가가 {w}일선 {'위' if up else '아래'}"
    ma, close = c[f"ma{w}"], c["close"]
    if pd.isna(ma):
        return Check("추세", label, None, f"{w}일선 계산 불가(시세 이력 부족)", key, scored)
    return Check("추세", label, bool(sign * (close - ma) > 0), f"종가 {_won(close)} / {w}일선 {_won(ma)}", key, scored)


def _slope_check(c, sign):
    label = "20일선 우상향" if sign > 0 else "20일선 우하향"
    ma20, ma20p = c["ma20"], c["ma20_prev"]
    if pd.isna(ma20) or pd.isna(ma20p):
        return Check("추세", label, None, "20일선 계산 불가", "ma20_slope", False)
    return Check("추세", label, bool(sign * (ma20 - ma20p) > 0),
                 f"20일선 {_won(ma20)} (5거래일 전 {_won(ma20p)})", "ma20_slope", False)


def _align_check(c, sign):
    up = sign > 0
    order = (">" if up else "<").join(str(w) for w in ALIGN_WINDOWS)
    label = f"{'정배열' if up else '역배열'} ({order}일선)"
    mas = [c[f"ma{w}"] for w in ALIGN_WINDOWS]
    if any(pd.isna(m) for m in mas):
        return Check("추세", label, None, "이평선 계산 불가", "align", False)
    passed = all(sign * (a - b) > 0 for a, b in zip(mas, mas[1:]))
    detail = " / ".join(f"{w}일 {_won(m)}" for w, m in zip(ALIGN_WINDOWS, mas))
    return Check("추세", label, bool(passed), detail, "align", False)


def _neck_label(p):
    """박스권은 넥라인이 아니라 박스 상단(돌파)/하단(이탈)이 기준선이다."""
    if p.name.startswith("박스권"):
        return "박스 상단" if p.direction == "bull" else "박스 하단"
    return "넥라인"


def _pattern_ref_check(patterns, c, sign):
    """패턴은 참고 신호다 — 검증에서 강세 패턴은 일관된 초과수익이 없었고(쌍바닥은 오히려 음), 약세 패턴도
    유니버스마다 달랐다. 차트에는 그리되 점수에는 넣지 않는다."""
    up = sign > 0
    label = "강세 패턴" if up else "약세 패턴"
    done = next((p for p in patterns if p.status == "완성"), None)
    if done:
        detail = done.title
        if done.name == "박스권 돌파":   # 박스를 돌파한 위치가 20/60/240일선 위인지도 함께 보여준다
            marks = " ".join(f"{w}일선 {'✓' if pd.notna(c[f'ma{w}']) and c['close'] > c[f'ma{w}'] else '✗'}"
                             for w in BOX_BONUS_MAS)
            detail += f" · {marks}"
        return Check("패턴", f"{label} 완성", True, detail, "pattern", False)
    forming = next((p for p in patterns if p.status == "형성중"), None)
    if forming:
        verb = "돌파" if up else "이탈"
        return Check("패턴", f"{label} 완성", False,
                     f"{forming.name} 형성중 — {_neck_label(forming)} {_won(forming.neckline)} {verb} 대기", "pattern", False)
    return Check("패턴", f"{label} 완성", False, f"{label} 없음", "pattern", False)


def _momentum_ref_check(c):
    """6~2개월 모멘텀은 참고 신호다 — 240일선 위에서만 의미가 있었고(아래에서는 효과 없음), 독립 구간에서는
    방향만 같고 통계적으로 유의하지 않았다(모듈 상단 설명). 매수 진단에만 둔다(매도 쪽은 검증하지 않았다)."""
    label = f"6~2개월 전 수익률이 지수 대비 +{MOM_REL_THRESHOLD * 100:.0f}%p↑ (240일선 위)"
    mom, rel, ma240, close = c["mom62"], c["rel62"], c["ma240"], c["close"]
    if pd.isna(mom) or pd.isna(ma240):
        return Check("모멘텀", label, None, "시세 이력 부족(약 6개월 필요)", "mom62", False)
    if pd.isna(rel):
        return Check("모멘텀", label, None, f"종목 {mom * 100:+.0f}% · 지수 데이터 없음", "mom62", False)
    above = close > ma240
    note = "240일선 위" if above else "240일선 아래(이 경우는 근거 없음)"
    return Check("모멘텀", label, bool(above and rel >= MOM_REL_THRESHOLD),
                 f"종목 {mom * 100:+.0f}% · 지수 대비 {rel * 100:+.0f}%p · {note}", "mom62", False)


def _flow_check(flow_n, sign):
    label = "외국인·기관 5일 순매수" if sign > 0 else "외국인·기관 5일 순매도"
    if flow_n is None:
        return Check("수급", label, None, "수급 데이터 없음(네이버는 최근 5거래일만 제공)", "flow")
    s = flow_n["sum"]
    return Check("수급", label, sign * s > 0,
                 f"외국인 {flow_n['foreign']:+,}주 · 기관 {flow_n['organ']:+,}주 (합 {s:+,}주)", "flow")


def _volume_check(c, sign):
    label = f"거래량 20일평균 {VOL_SPIKE_MULT:g}배↑ & 종가 {'상승' if sign > 0 else '하락'}"
    vm = c["vol_mult"]
    if pd.isna(vm):
        return Check("거래량", label, None, "거래량 계산 불가", "volume")
    chg = c["close"] - c["prev_close"]
    passed = bool(vm >= VOL_SPIKE_MULT and sign * chg > 0)
    return Check("거래량", label, passed,
                 f"직전 20일 평균의 {vm:.1f}배 · 종가 {'상승' if chg > 0 else ('하락' if chg < 0 else '보합')}", "volume")


def _count(checks):
    """점수에 반영되는(scored) 항목 중 평가 가능한 것만 센다. 반환 (통과 개수, 평가 가능 개수)."""
    evaluable = [c for c in checks if c.scored and c.passed is not None]
    return sum(1 for c in evaluable if c.passed), len(evaluable)


def _first_fail(checks):
    return next((c for c in checks if c.scored and c.passed is False), None)


def _sell_verdict(passed, total):
    if total <= 0:
        return "판정 불가"
    if passed * 100 >= total * SELL_THRESHOLDS[0]:
        return "매도 고려"
    if passed * 100 >= total * SELL_THRESHOLDS[1]:
        return "지켜보기"
    return "보유 유지"


def _hold_verdict(pnl, stop_breach, trend_break, flow_ok, above240, above_ma10, take_profit_hit):
    """보유 진단 규칙(우선순위 순). pnl이 None(평단가 미입력)이면 호출하지 않는다.
    손절 > 익절 > 물타기 > 보유 유지. 물타기는 장기(240일선)·단기(10일선) 추세가 살아 있고 수급이 순매수일 때만 —
    검증에서 240일선 아래 종목은 이후에도 시장 대비 열위였고, 쌍바닥 같은 바닥 패턴은 오히려 음(-)의 신호였다."""
    if stop_breach or (pnl < 0 and trend_break):
        return "손절 고려"
    if pnl >= HOLD_TAKE_PROFIT_PCT and take_profit_hit:
        return "분할 익절 고려"
    if pnl < 0 and flow_ok and above240 and above_ma10:
        return "물타기 고려"
    return "보유 유지"


def _buy_action(verdict, checks, c, risk, score_text):
    if verdict == "매수 고려":
        tail = f" 참고 손절 {_won(risk['stop'])} · 목표 {_won(risk['target'])}(ATR 기준)." if risk else ""
        return f"핵심 조건 {score_text}개가 갖춰졌어요.{tail}"
    prefix = "지금은 사지 않아요. " if verdict == "사지 않기" else "조금 더 지켜봐요. "
    f = _first_fail(checks)
    if f is None:
        return prefix + "평가할 수 있는 신호가 부족해요."
    if f.key == "ma240":
        hint = f"종가가 240일선({_won(c['ma240'])})을 회복하길 기다려요."
    elif f.key == "flow":
        hint = "외국인·기관이 순매수로 돌아서길 기다려요."
    else:
        hint = f"거래량이 20일 평균의 {VOL_SPIKE_MULT:g}배 넘게 실린 상승봉이 나오길 기다려요."
    return prefix + hint


def _sell_action(verdict, checks, c, passed, total):
    below240 = checks[0].passed   # SCORED_KEYS 순서: ma240 → flow → volume
    pos = ("종가가 240일선 아래" if below240 else f"종가가 240일선({_won(c['ma240'])}) 위") if below240 is not None else "240일선 판단 불가"
    if verdict == "매도 고려":
        return f"약세 신호가 {passed}/{total}개 겹쳤어요({pos}). 비중 축소를 고려해요."
    if verdict == "지켜보기":
        return f"신호가 엇갈려요({pos}). 수급과 거래량 변화를 지켜봐요."
    if verdict == "보유 유지":
        return f"핵심 약세 신호가 없어요({pos}). 추세가 이어지는 동안은 들고 가도 좋아요."
    return "평가할 수 있는 신호가 부족해요."


def _evaluate_buy(c, flow_n, bull, risk):
    checks = [
        _price_vs_ma(c, 240, +1, "ma240"),     # 게이트 겸 점수 항목(SCORED_KEYS 순서 유지: 맨 앞)
        _flow_check(flow_n, +1),
        _volume_check(c, +1),
        # --- 참고 신호(점수 미반영) ---
        _momentum_ref_check(c),
        _price_vs_ma(c, 10, +1, "ma10", scored=False),
        _slope_check(c, +1),
        _align_check(c, +1),
        _pattern_ref_check(bull, c, +1),
    ]
    passed, total = _count(checks)
    grade = _apply_gate(grade_from_counts(passed, total), checks[0].passed)
    verdict = GRADE_VERDICT.get(grade, "판정 불가")
    score_text = f"{passed}/{total}"
    return ModeResult("매수", verdict, grade, passed, total, checks,
                      _buy_action(verdict, checks, c, risk, score_text), score_text)


def _evaluate_sell(c, flow_n, bear):
    checks = [
        _price_vs_ma(c, 240, -1, "ma240"),
        _flow_check(flow_n, -1),
        _volume_check(c, -1),
        # --- 참고 신호(점수 미반영) ---
        _price_vs_ma(c, 10, -1, "ma10", scored=False),
        _slope_check(c, -1),
        _align_check(c, -1),
        _pattern_ref_check(bear, c, -1),
    ]
    passed, total = _count(checks)
    verdict = _sell_verdict(passed, total)
    return ModeResult("매도", verdict, None, passed, total, checks,
                      _sell_action(verdict, checks, c, passed, total), f"약세 신호 {passed}/{total}")


def _evaluate_hold(df, c, flow_n, bear, buy_price):
    close = c["close"]
    has_buy = buy_price is not None and buy_price > 0
    pnl = close / buy_price - 1 if has_buy else None
    stop_line = buy_price * (1 - HOLD_STOP_PCT) if has_buy else None
    ma10, ma20, ma240 = c["ma10"], c["ma20"], c["ma240"]
    bear_done = any(p.status == "완성" for p in bear)
    flow_ok = bool(flow_n and flow_n["sum"] > 0)
    above240 = bool(pd.notna(ma240) and close > ma240)
    above10 = bool(pd.notna(ma10) and close > ma10)

    checks = [
        Check("손익", "평단 -8% 손절선 위", (close > stop_line) if has_buy else None,
              (f"평단 {_won(buy_price)} → 현재 {pnl * 100:+.1f}% / 손절선 {_won(stop_line)}" if has_buy
               else "평단가를 입력하면 평가해요"), "stop"),
        _price_vs_ma(c, 240, +1, "ma240"),
        _flow_check(flow_n, +1),
        # --- 참고 신호(점수 미반영) ---
        _price_vs_ma(c, 20, +1, "ma20", scored=False),
        Check("패턴", "약세 패턴 없음", not bear_done,
              next((p.title for p in bear if p.status == "완성"), "완성된 약세 패턴 없음"), "bear_pattern", False),
    ]
    passed, total = _count(checks)
    grade = grade_from_counts(passed, total)

    extra = {"pnl": pnl, "stop_line": stop_line, "buy_price": buy_price if has_buy else None}
    if has_buy:
        two_below = bool((df["close"].iloc[-2:] < df["MA20"].iloc[-2:]).all())
        trail = float(df["close"].iloc[-60:].max() - TRAIL_ATR_MULT * c["atr"]) if pd.notna(c["atr"]) else None
        take_profit_hit = bool((pd.notna(ma10) and close < ma10) or (trail is not None and close <= trail))
        verdict = _hold_verdict(pnl, close <= stop_line, two_below, flow_ok, above240, above10, take_profit_hit)
        extra.update({"trail": trail, "two_below_ma20": two_below})
        if verdict == "손절 고려":
            why = (f"손절선({_won(stop_line)})을 이탈했어요" if close <= stop_line
                   else "손실 중에 20일선 아래로 2일 연속 마감했어요")
            action = f"평단 대비 {pnl * 100:+.1f}%예요. {why}. 규칙대로 정리를 고려해요."
        elif verdict == "분할 익절 고려":
            action = (f"평단 대비 {pnl * 100:+.1f}%예요. 10일선 이탈 또는 고점 대비 하락 폭이 커졌어요. "
                      "일부 익절을 고려해요.")
        elif verdict == "물타기 고려":
            action = (f"{pnl * 100:+.1f}% 손실 중이지만 240일선·10일선 위에서 수급이 받쳐줘요. "
                      f"물타기는 1회·분할로 하고 손절선({_won(stop_line)})은 지켜요.")
        elif pnl < 0:
            action = (f"{pnl * 100:+.1f}% 손실 중이에요. 물타기는 종가가 240일선·10일선 위이고 수급이 순매수일 때만 해요. "
                      f"지금은 추가 매수 없이 손절선({_won(stop_line)})만 지켜봐요.")
        else:
            action = f"평단 대비 {pnl * 100:+.1f}%예요. 손절선({_won(stop_line)}) 위에서 추세가 유지되고 있어요."
    else:
        # 평단가가 없으면 추세 건강도만 본다: 장기 추세(240일선)가 살아 있고 점수 항목의 60% 이상 통과해야 '보유 유지'.
        # 상장 1년 미만이라 240일선이 없으면 20일선으로 대신한다.
        trend = checks[1] if checks[1].passed is not None else checks[3]
        ma_name, ma_val = ("240", ma240) if checks[1].passed is not None else ("20", ma20)
        healthy = trend.passed is True and passed * 100 >= total * 60
        verdict = "보유 유지" if healthy else "추세 점검"
        action = ("평단가를 입력하면 손익 기준으로 손절·익절·물타기를 판정해요. "
                  + ("지금 추세는 유지되는 편이에요." if healthy
                     else f"종가가 {ma_name}일선({_won(ma_val)}) 아래라 추세가 약해요." if trend.passed is False
                     else "추세를 판단할 데이터가 부족해요."))
    return ModeResult("보유", verdict, grade, passed, total, checks, action, f"{passed}/{total}", extra)


def _pillars(c, flow_n, bull, bear):
    ma10, ma20, ma20p, ma240, close = c["ma10"], c["ma20"], c["ma20_prev"], c["ma240"], c["close"]
    if pd.isna(ma10) or pd.isna(ma20) or pd.isna(ma20p):
        trend = ("확인 불가", "이동평균 계산에 필요한 데이터가 부족해요")
    else:
        long_up = None if pd.isna(ma240) else bool(close > ma240)
        up = close > ma10 and ma20 > ma20p and long_up is not False
        down = close < ma10 and ma20 < ma20p and long_up is not True
        parts = ([f"240일선 {'위' if long_up else '아래'}"] if long_up is not None else []) + [
            f"10일선 {'위' if close > ma10 else '아래'}", f"20일선 {'우상향' if ma20 > ma20p else '우하향'}"]
        trend = ("상승" if up else ("하락" if down else "혼조"), " · ".join(parts))
    if flow_n is None:
        flow = ("확인 불가", "최근 5거래일 수급 없음")
    else:
        s = flow_n["sum"]
        flow = ("유입" if s > 0 else ("이탈" if s < 0 else "중립"),
                f"외국인 {flow_n['foreign']:+,} · 기관 {flow_n['organ']:+,}")
    top = max(bull[:1] + bear[:1], key=_pattern_rank, default=None)
    if top:
        pattern = (top.title, f"{_neck_label(top)} {_won(top.neckline)} · 참고용(점수 미반영)")
    else:
        pattern = ("없음", "감지된 패턴 없음")
    vm = c["vol_mult"]
    if pd.isna(vm):
        volume = ("확인 불가", "")
    else:
        chg = c["close"] - c["prev_close"]
        spike = vm >= VOL_SPIKE_MULT
        volume = (("급증↑" if chg > 0 else "급증↓") if spike and chg != 0 else ("급증" if spike else "보통"),
                  f"직전 20일 평균의 {vm:.1f}배")
    return {k: {"state": s, "detail": d} for k, (s, d) in
            {"추세": trend, "수급": flow, "패턴": pattern, "거래량": volume}.items()}


def evaluate_all(df, flow_n, buy_price=None):
    """prepare()된 df(마지막 행 = 진단 시점)로 매수/보유/매도 진단을 한 번에 계산.
    반환: (results, pillars, patterns, risk)"""
    pivots = zigzag_pivots(df)
    bull = detect_patterns(df, pivots, +1)
    bear = detect_patterns(df, pivots, -1)
    c = _ctx(df)
    risk = risk_levels(c["close"], c["atr"])
    results = {
        "매수": _evaluate_buy(c, flow_n, bull, risk),
        "보유": _evaluate_hold(df, c, flow_n, bear, buy_price),
        "매도": _evaluate_sell(c, flow_n, bear),
    }
    return results, _pillars(c, flow_n, bull, bear), {"bull": bull, "bear": bear}, risk


def summarize_signal(results):
    """매수·매도 진단을 관심종목 필터용 4분류(SIGNAL_ORDER) 하나로 요약. 위험 우선:
    매도 고려 > 매수 고려 > 사지 않기 > 지켜보기."""
    if results["매도"].verdict == "매도 고려":
        return "매도 고려"
    buy = results["매수"].verdict
    if buy == "매수 고려":
        return "매수 고려"
    if buy == "사지 않기":
        return "사지 않기"
    return "지켜보기"


def to_row(diag, name=""):
    """Diagnosis → 관심종목 표 한 행(평탄한 dict). '_'로 시작하는 키는 정렬용이라 화면에는 쓰지 않는다."""
    buy, sell, hold = diag.results["매수"], diag.results["매도"], diag.results["보유"]
    close, prev = float(diag.df["close"].iloc[-1]), float(diag.df["close"].iloc[-2])
    pnl = hold.extra.get("pnl")
    return {
        "종목명": name or diag.code, "코드": diag.code, "종가": close, "등락률%": (close / prev - 1) * 100,
        "신호": summarize_signal(diag.results),
        "매수 등급": buy.grade or "-", "매수 판정": buy.verdict, "핵심 통과": buy.score_text,
        "매도 판정": sell.verdict,
        "보유 판정": hold.verdict if pnl is not None else "-",
        "손익%": pnl * 100 if pnl is not None else None,
        "추세": diag.pillars["추세"]["state"], "수급": diag.pillars["수급"]["state"],
        "패턴": diag.pillars["패턴"]["state"], "거래량": diag.pillars["거래량"]["state"],
        "_grade_rank": GRADE_RANK.get(buy.grade, 9),
        "_ratio": buy.passed / buy.total if buy.total else -1.0,
    }


def diagnose(code, as_of=None, buy_price=None, df_raw=None, flow=None, min_rows=30, index_close=None):
    """종목 하나를 진단. df_raw/flow를 주면 조회를 건너뛴다(앱에서 캐시된 데이터 재사용).
    as_of(날짜)를 주면 그 날짜까지의 데이터로만 진단 — 과거 시점 진단·등급 검증용.
    index_close(시장 지수 종가 시리즈)를 주면 6~2개월 모멘텀 참고 신호를 지수 대비로 계산한다(없으면 '평가 불가').
    데이터가 부족하면 None."""
    if df_raw is None:
        df_raw = fetch_ohlcv(code)
    if df_raw is None or df_raw.empty:
        return None
    df = prepare(df_raw, index_close)
    if as_of is not None:
        df = df.loc[:pd.Timestamp(as_of)]
    if len(df) < min_rows:
        return None
    as_of_ts = df.index[-1]
    if flow is None:
        flow = fetch_investor_flow(code)
    flow_n = flow_net(flow, as_of_ts)
    results, pillars, patterns, risk = evaluate_all(df, flow_n, buy_price)
    return Diagnosis(code, as_of_ts, df, flow_n, results, pillars, patterns, risk)
