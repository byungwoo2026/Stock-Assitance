# -*- coding: utf-8 -*-
"""
DART OpenAPI(opendart.fss.or.kr) 클라이언트 — streamlit 비의존 → 검증 스크립트·테스트에서 그대로 쓴다.

- 키는 환경변수 DART_API_KEY 또는 .streamlit/secrets.toml 에서 읽는다. 키는 요청 URL(쿼리)에 실리므로
  requests 예외를 그대로 올리면 키가 로그에 남는다 → 모든 오류는 DartError로 감싸 URL을 버린다.
- 받은 목록·원문은 dart_cache/ 에 캐시한다(.gitignore). 일 한도(약 2만 건)를 아끼고 재실행을 빠르게 한다.
"""
import io
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

try:
    import tomllib          # 3.11+ — 더 낮은 버전에선 환경변수 DART_API_KEY 만 쓴다
except ModuleNotFoundError:
    tomllib = None

BASE ="https://opendart.fss.or.kr/api/"
_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_HERE, "dart_cache")
SECRETS_PATH = os.path.join(_HERE, ".streamlit", "secrets.toml")
CONNECT_TIMEOUT = 10         # 접속 대기(초) — 접속이 안 되는 환경에서 한 요청이 3분씩 매달리지 않게 짧게
READ_TIMEOUT = 60            # 응답 대기(초) — corpCode.xml 같은 큰 zip을 받는 시간
MIN_INTERVAL = 0.12          # 요청 간 최소 간격(초) — 연속 호출이 차단되지 않게
PAGE_COUNT = 100             # list.json 최대 페이지 크기
NO_DATA = "013"
STATUS_MSG = {
    "010": "등록되지 않은 키", "011": "사용할 수 없는 키", "012": "접근 불가 IP", "020": "요청 한도 초과",
    "021": "조회 가능 회사 수 초과", "100": "요청 값 오류", "101": "부적절한 접근", "800": "서비스 점검 중", "900": "정의되지 않은 오류",
    "901": "키 사용 기간 만료",
}

_last_call = 0.0
_lock = threading.Lock()


class DartError(Exception):
    """키가 섞인 URL 대신 상태 코드·메시지만 담는다."""


def load_key():
    key = os.environ.get("DART_API_KEY")
    if not key and tomllib and os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH, "rb") as f:
            key = tomllib.load(f).get("DART_API_KEY")
    if not key:
        raise DartError("DART_API_KEY가 없습니다(환경변수 또는 .streamlit/secrets.toml).")
    return str(key).strip()


def _request(path, **params):
    global _last_call
    with _lock:                                   # 스레드가 여러 개여도 전체 호출 간격을 지킨다
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    params["crtfc_key"] = load_key()
    last = None
    for attempt in range(3):
        try:
            resp = requests.get(BASE + path, params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last = type(e).__name__              # 메시지·URL은 버린다(키 포함)
            time.sleep(1.5 * (attempt + 1))
    raise DartError(f"DART 요청 실패({path}): {last}")


PROBE_CONTROL_URL = "https://m.stock.naver.com/"      # 대조군 — 앱이 이미 쓰는 국내 사이트


def probe_connection(host="opendart.fss.or.kr", timeout=8):
    """이 서버에서 DART까지 어느 단계에서 막히는지 점검한다. 키는 쓰지 않는다(키 없는 요청도 응답은 온다).
    반환: [{"step", "ok", "sec", "detail"}] — 이름 확인 → 443 포트 접속 → HTTPS 요청 → 대조군(국내 사이트) 순."""
    import socket
    out = []

    def run(step, fn):
        t0 = time.time()
        try:
            detail = fn()
            ok = True
        except Exception as e:                      # 요청 URL에 키가 없으므로 메시지를 짧게 남겨도 안전하다
            detail, ok = f"{type(e).__name__}: {str(e)[:120]}", False
        out.append({"step": step, "ok": ok, "sec": round(time.time() - t0, 1), "detail": detail})
        return ok

    addrs = []

    def dns():
        addrs.extend(sorted({a[4][0] for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}))
        return ", ".join(addrs)

    def tcp():
        with socket.create_connection((addrs[0], 443), timeout=timeout):
            return f"{addrs[0]}:443 연결됨"

    def https():
        r = requests.get(f"https://{host}/api/corpCode.xml", timeout=(timeout, timeout))
        return f"HTTP {r.status_code}, {len(r.content):,}바이트(키 없는 요청이라 오류 안내 페이지가 정상)"

    if run("이름 확인(DNS)", dns) and run("443 포트 접속", tcp):
        run("HTTPS 요청", https)
    run("대조군: 네이버 증권", lambda: f"HTTP {requests.get(PROBE_CONTROL_URL, timeout=(timeout, timeout)).status_code}")
    return out


def get_json(path, **params):
    """status 000이면 dict, 013(데이터 없음)이면 빈 dict, 그 외는 DartError."""
    data = _request(path, **params).json()
    status = str(data.get("status"))
    if status == NO_DATA:
        return {}
    if status != "000":
        raise DartError(f"DART 응답 오류 {status}: {STATUS_MSG.get(status, data.get('message', ''))}")
    return data


def get_zip(path, **params):
    resp = _request(path, **params)
    if resp.content[:2] != b"PK":                 # 오류는 zip이 아니라 XML/JSON 본문으로 온다
        raise DartError(f"DART가 zip이 아닌 응답을 보냈습니다({path}).")
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _cache_path(name):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def corp_codes(refresh=False):
    """{종목코드: {"corp_code","corp_name"}} — 상장사(종목코드가 있는 회사)만."""
    path = _cache_path("corp_codes.json")
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    zf = get_zip("corpCode.xml")
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    out = {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if stock:
            out[stock] = {"corp_code": el.findtext("corp_code").strip(), "corp_name": el.findtext("corp_name").strip()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def list_reports(bgn_de, end_de, detail_ty="A001", corp_code=None, last_only=True):
    """공시 목록(YYYYMMDD). corp_code 없이 조회하면 DART가 기간을 제한하므로 호출 쪽에서 기간을 나눠 부른다.
    detail_ty: A001 사업보고서 · A002 반기 · A003 분기 · B001 주요사항(사실상 pblntf_ty=B는 detail 미지정으로)."""
    rows, page = [], 1
    while True:
        params = dict(bgn_de=bgn_de, end_de=end_de, pblntf_detail_ty=detail_ty, page_no=page, page_count=PAGE_COUNT,
                      last_reprt_at="Y" if last_only else "N")
        if corp_code:
            params["corp_code"] = corp_code
        data = get_json("list.json", **params)
        rows += data.get("list", [])
        if page >= int(data.get("total_page", 0) or 0):
            return rows
        page += 1


def _decode(raw):
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def download_document(rcept_no, refresh=False, cache=True):
    """공시 원문(XML 조각들)을 이어붙인 텍스트. cache=False면 디스크에 남기지 않는다(전 종목 훑기용 — 원문이 수 MB)."""
    path = _cache_path(f"doc_{rcept_no}.txt")
    if cache and os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            return f.read()
    zf = get_zip("document.xml", rcept_no=rcept_no)
    text = "\n".join(_decode(zf.read(n)) for n in zf.namelist() if n.lower().endswith((".xml", ".html", ".htm")))
    if cache:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return text


def main_accounts(corp_code, bsns_year, reprt_code="11011"):
    """주요계정(매출액·영업이익·당기순이익 등). reprt_code 11011 사업·11012 반기·11013 1분기·11014 3분기."""
    return get_json("fnlttSinglAcnt.json", corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code).get("list", [])


# --- 종목 분석 보고서용: 응답 목록을 디스크에 캐시하는 조회들 -------------------------------------------------
# 사업보고서 숫자는 한번 공시되면 거의 안 바뀌고, 종목 하나를 열 때 10여 건을 부르므로(일 한도 약 2만 건) 캐시한다.
# 아직 안 나온 보고서(013)도 잠깐 기억해 같은 종목을 다시 열 때 헛조회를 되풀이하지 않는다. 키는 캐시 이름(해시)에 넣지 않는다.
CACHE_TTL_HOURS = 72
EMPTY_TTL_HOURS = 6


def _api_cache_path(path, params):
    import hashlib
    raw = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return _cache_path("api_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24] + ".json")


def cached_list(path, ttl_hours=CACHE_TTL_HOURS, **params):
    """get_json(path, **params)의 'list'를 캐시해 돌려준다(데이터 없음이면 빈 목록, 그 경우는 EMPTY_TTL_HOURS만 기억)."""
    cp = _api_cache_path(path, params)
    try:
        with open(cp, encoding="utf-8") as f:
            hit = json.load(f)
        age_h = (time.time() - hit["t"]) / 3600
        if age_h < (ttl_hours if hit["list"] else min(ttl_hours, EMPTY_TTL_HOURS)):
            return hit["list"]
    except (OSError, ValueError, KeyError):
        pass
    rows = get_json(path, **params).get("list", [])
    try:                                           # 캐시 쓰기 실패(읽기 전용 디스크 등)는 조회 결과에 영향 없음
        tmp = f"{cp}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "list": rows}, f, ensure_ascii=False)
        os.replace(tmp, cp)
    except OSError:
        pass
    return rows


def main_accounts_cached(corp_code, bsns_year, reprt_code="11011", ttl_hours=CACHE_TTL_HOURS):
    return cached_list("fnlttSinglAcnt.json", ttl_hours, corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code)


def all_statements(corp_code, bsns_year, reprt_code="11011", fs_div="CFS", ttl_hours=CACHE_TTL_HOURS):
    """전체 재무제표(BS·IS/CIS·CF·SCE 전 계정). fs_div CFS 연결 · OFS 별도."""
    return cached_list("fnlttSinglAcntAll.json", ttl_hours, corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code, fs_div=fs_div)


def share_totals(corp_code, bsns_year, reprt_code="11011", ttl_hours=CACHE_TTL_HOURS):
    """주식의 총수 현황(보통주·우선주별 발행주식수·자기주식수)."""
    return cached_list("stockTotqySttus.json", ttl_hours, corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code)


def shareholders(corp_code, bsns_year, reprt_code="11011", ttl_hours=CACHE_TTL_HOURS):
    """최대주주 및 특수관계인 지분 현황."""
    return cached_list("hyslrSttus.json", ttl_hours, corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code)


def treasury_stock(corp_code, bsns_year, reprt_code="11011", ttl_hours=CACHE_TTL_HOURS):
    """자기주식 취득·처분·소각 현황."""
    return cached_list("tesstkAcqsDspsSttus.json", ttl_hours, corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code)


def recent_filings(corp_code, bgn_de, end_de, count=100, ttl_hours=1):
    """회사의 전체 공시 목록(최신순, 한 페이지). 정정 공시도 포함해 있는 그대로 돌려준다."""
    return cached_list("list.json", ttl_hours, corp_code=corp_code, bgn_de=bgn_de, end_de=end_de, last_reprt_at="N",
                       sort="date", sort_mth="desc", page_no=1, page_count=count)
