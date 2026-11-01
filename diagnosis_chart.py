# -*- coding: utf-8 -*-
"""AI 종목 진단용 Plotly 차트(캔들 + 5/10/20/240 이평 + 거래량 + 패턴 오버레이).
streamlit에 의존하지 않는다 — Figure만 만들어 반환하고 화면 출력은 app.py가 담당."""
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from diagnosis_core import CIRCLED

UP, DOWN = "#e03131", "#1971c2"   # 한국식 색: 상승 빨강 / 하락 파랑 (앱의 지수 표시와 동일)
MA_STYLES = {5: "#f59f00", 10: "#2f9e44", 20: "#845ef7", 60: "#c2255c", 240: "#212529"}
PATTERN_COLOR = {"bull": "#f76707", "bear": "#7048e8"}

TIMEFRAMES = ("일봉", "주봉", "월봉")
_RESAMPLE_RULE = {"일봉": None, "주봉": "W-FRI", "월봉": "ME"}
VIEW_MONTHS = {"3개월": 3, "6개월": 6, "1년": 12, "2년": 24}


def resample_ohlcv(df, timeframe):
    """prepare()된 일봉 df → 선택한 봉 단위의 OHLCV + 5/10/20/240 이평(해당 봉 단위 기준)."""
    base = df[["open", "high", "low", "close", "volume"]]
    rule = _RESAMPLE_RULE[timeframe]
    if rule is None:
        out = base.copy()
    else:
        out = base.resample(rule).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["close"])
    for w in MA_STYLES:
        out[f"MA{w}"] = out["close"].rolling(w).mean()
    return out


def build_figure(df, timeframe="일봉", months=6, patterns=(), title=""):
    """df: diagnosis_core.prepare() 결과(진단 시점까지 잘린 일봉). months: 표시할 최근 개월 수(VIEW_MONTHS 값).
    patterns: 오버레이할 Pattern 목록."""
    full = resample_ohlcv(df, timeframe)
    last_date = full.index[-1]
    x0 = last_date - pd.DateOffset(months=months)
    # 패턴이 표시 구간보다 앞에서 시작하면 잘리지 않도록 구간을 넓힌다
    x0 = min([x0] + [p.start - pd.Timedelta(days=7) for p in patterns])
    view = full.loc[x0:]
    if view.empty:
        view = full

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.76, 0.24])
    fig.add_trace(go.Candlestick(
        x=view.index, open=view["open"], high=view["high"], low=view["low"], close=view["close"],
        increasing_line_color=UP, increasing_fillcolor=UP,
        decreasing_line_color=DOWN, decreasing_fillcolor=DOWN,
        name="가격", showlegend=False), row=1, col=1)

    for w, color in MA_STYLES.items():
        col = f"MA{w}"
        if view[col].notna().any():
            fig.add_trace(go.Scatter(x=view.index, y=view[col], mode="lines", name=f"이평{w}",
                                     line=dict(color=color, width=1.4), hoverinfo="skip"), row=1, col=1)

    vol_colors = [UP if c >= o else DOWN for o, c in zip(view["open"], view["close"])]
    fig.add_trace(go.Bar(x=view.index, y=view["volume"], marker_color=vol_colors, name="거래량",
                         showlegend=False), row=2, col=1)

    lows, highs = [float(view["low"].min())], [float(view["high"].max())]
    for p in patterns:
        color = PATTERN_COLOR[p.direction]
        lows.append(p.zone_low)
        highs.append(p.zone_high)
        fig.add_shape(type="rect", xref="x", yref="y", x0=p.start, x1=p.end,
                      y0=p.zone_low * 0.985, y1=p.zone_high * 1.015,
                      fillcolor=color, opacity=0.10, line_width=0, layer="below")
        (n0, n1) = p.neck_line
        fig.add_shape(type="line", xref="x", yref="y", x0=n0[0], y0=n0[1], x1=n1[0], y1=n1[1],
                      line=dict(color=color, width=1.5, dash="dash"))
        fig.add_trace(go.Scatter(
            x=[pt[1] for pt in p.points], y=[pt[2] for pt in p.points],
            mode="lines+markers+text",
            text=[CIRCLED[pt[0] - 1] for pt in p.points],
            textposition=["top center" if pt[3] == "top" else "bottom center" for pt in p.points],
            textfont=dict(size=15, color=color),
            line=dict(color=color, width=2), marker=dict(size=7, color=color),
            name=p.name, showlegend=False, hoverinfo="skip"), row=1, col=1)
        fig.add_annotation(x=p.start, y=p.zone_high * 1.015, xref="x", yref="y", text=f"<b>{p.title}</b>",
                           showarrow=False, xanchor="left", yanchor="bottom",
                           font=dict(color="white", size=12), bgcolor=color, borderpad=3)

    last_close = float(view["close"].iloc[-1])
    fig.add_hline(y=last_close, line_dash="dot", line_color="#868e96", line_width=1,
                  annotation_text=f"{last_close:,.0f}", annotation_position="right", row=1, col=1)

    fig.update_yaxes(range=[min(lows) * 0.97, max(highs) * 1.04], tickformat=",d", row=1, col=1)
    fig.update_yaxes(tickformat="~s", row=2, col=1)
    if timeframe == "일봉":
        # 주말뿐 아니라 휴장일(데이터가 없는 평일)도 축에서 접어 캔들이 끊기지 않게 한다
        missing = pd.bdate_range(view.index[0], view.index[-1]).difference(view.index)
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]),
                                      dict(values=[d.strftime("%Y-%m-%d") for d in missing])])
    fig.update_layout(
        template="plotly_white", height=660, title=dict(text=title, x=0.01, font=dict(size=15)),
        xaxis_rangeslider_visible=False, hovermode="x unified",
        margin=dict(l=10, r=64, t=50, b=10),   # 우측 여백: 현재가 라벨이 잘리지 않도록
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
    )
    return fig
