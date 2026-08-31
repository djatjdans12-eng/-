# -*- coding: utf-8 -*-
"""
road_addr.py — 지번주소에서 도로명만 뽑아 온다.

    "유산동 159-71"  →  "유산공단7길"
    "하북면 순지리 830"  →  "순지로" (예시)

행정안전부 도로명주소 검색 API 를 쓴다. 외부 라이브러리 없이 표준 라이브러리만
사용한다 (img_click.py, plate_ocr.py 와 같은 방침).

API 스펙은 juso.go.kr 공식 가이드 패키지(guideSearchApi.zip)의
apiSampleJSONController.java / apiSampleJSON.jsp 에서 확인한 것을 그대로 따랐다.

    GET https://business.juso.go.kr/addrlink/addrLinkApi.do
        ?currentPage=1
        &countPerPage=5           (범위: 0 < n <= 100)
        &resultType=json
        &keyword=<UTF-8 URL 인코딩>
        &confmKey=<승인키>

    응답(UTF-8 JSON)
        results.common.errorCode     "0" 이면 정상
        results.common.errorMessage
        results.common.totalCount
        results.juso[].rn            ← 도로명만 따로 들어 있다

원칙
    확실할 때만 값을 돌려준다. 못 찾거나 후보가 갈리면 빈 문자열을 돌려주고,
    부르는 쪽에서 원래 지번주소를 그대로 두게 한다.
    과태료 처분이라 엉뚱한 주소가 들어가면 실제 피해가 생기기 때문이다.

좌표로 찾기 (주 경로)
    도로명주소는 '건물'에 부여되므로 나대지·공터에는 아예 없다. 신고 대상은
    대부분 그런 곳이라 지번 검색만으로는 거의 안 잡힌다. 대신 민원내용에 들어
    있는 좌표를 쓰면, 차가 실제로 서 있던 지점의 도로명이 그대로 나온다.
    카카오 역지오코딩(coord2address)의 road_name 이 도로명만 따로 준다.

단독 테스트 (매크로 안 건드리고 API 만 확인)
    python road_addr.py --coord 35.357891 129.047210
    python road_addr.py "유산동 159-71"
"""

import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request

_ssl_ctx = None


def ssl_context():
    """
    HTTPS 검증용 컨텍스트.

    Python 3.13 부터 인증서 검사에 VERIFY_X509_STRICT 가 기본으로 켜졌는데,
    사무실·관공서 망이 SSL 을 중간에서 들여다보는 장비를 쓰면 그 장비가
    발급한 인증서에 Authority Key Identifier 같은 확장이 빠져 있어
    'CERTIFICATE_VERIFY_FAILED: Missing Authority Key Identifier' 로 막힌다.

    그래서 엄격 검사만 끈다. 인증서 신뢰 사슬과 호스트 이름 확인은 그대로
    남으므로 검증을 없애는 것이 아니다.
    """
    global _ssl_ctx
    if _ssl_ctx is None:
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        _ssl_ctx = ctx
    return _ssl_ctx


def _describe_url_error(e):
    """SSL·연결 오류를 사람이 알아볼 수 있게 풀어 준다."""
    s = str(e)
    if "CERTIFICATE_VERIFY_FAILED" in s:
        return ("인증서 검증 실패 — 사무실 망의 SSL 검사 장비 때문일 수 있습니다. "
                f"({s[:120]})")
    if "getaddrinfo" in s or "Name or service" in s:
        return f"주소를 찾지 못했습니다 — 인터넷 연결을 확인하세요 ({s[:120]})"
    if "timed out" in s.lower():
        return f"응답 시간 초과 ({s[:120]})"
    return s[:160]

DEFAULT_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

# 지번주소에 실제로 쓰이는 글자만 남긴다.
#   공식 샘플이 검색 전에 특수문자(% = > <)와 SQL 예약어를 걸러내는데,
#   한글·숫자·하이픈·공백만 남기면 그 두 가지가 한 번에 해결된다.
#   (OCR 이나 수기 입력으로 이상한 문자가 섞여 들어올 수 있다)
_KEEP = re.compile(r"[^가-힣0-9\-\s]")
_SPACES = re.compile(r"\s+")

# "159-71" 처럼 번지-호 형태가 보이면 아직 지번주소다.
_JIBUN_NUM = re.compile(r"\d+\s*-\s*\d+")

_CACHE_NAME = "도로명캐시.json"
_KEY_NAME = "승인키.txt"
_KAKAO_KEY_NAME = "카카오키.txt"
_mem_cache = {}
_cache_loaded = False


def _base_dir():
    return os.path.dirname(os.path.abspath(sys.argv[0])) or os.getcwd()


def load_api_key(cfg_key="", log=None, filename=_KEY_NAME, env_name="JUSO_API_KEY"):
    """
    키를 찾는다. 앞에서 찾으면 뒤는 안 본다.

      1) buttons.json 의 설정값   (넘겨받은 cfg_key)
      2) 스크립트 폴더의 키 파일   (승인키.txt / 카카오키.txt)
      3) 환경변수

    키 파일을 두는 쪽을 권한다. buttons.json 은 버튼 단계가 바뀔 때마다
    새로 받아 덮어쓰는 파일이라, 거기 적어 두면 갱신할 때마다 키가 지워진다.
    (키가 없으면 조용히 건너뛰도록 되어 있어서 멈춘 걸 알아채기도 어렵다)
    """
    if (cfg_key or "").strip():
        return cfg_key.strip(), "buttons.json"

    path = os.path.join(_base_dir(), filename)
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line, filename
    except FileNotFoundError:
        pass
    except Exception as e:
        if log:
            log(f"[도로명] {filename} 읽기 실패: {e}")

    env = (os.environ.get(env_name) or "").strip()
    if env:
        return env, f"환경변수 {env_name}"

    return "", ""


def load_kakao_key(cfg_key="", log=None):
    """카카오 REST API 키 (역지오코딩용). 승인키와 별개다."""
    return load_api_key(cfg_key, log,
                        filename=_KAKAO_KEY_NAME, env_name="KAKAO_REST_KEY")


def key_search_hint(filename=_KEY_NAME, cfg_name="roadname_api_key",
                    env_name="JUSO_API_KEY"):
    """키를 못 찾았을 때 어디를 봤는지 알려 주는 문구."""
    return (f"키를 찾지 못했습니다. 다음 중 한 곳에 넣어 주세요:\n"
            f"  1) {os.path.join(_base_dir(), filename)}  ← 여기가 편합니다\n"
            f"  2) buttons.json 의 {cfg_name}\n"
            f"  3) 환경변수 {env_name}")


def kakao_key_hint():
    return key_search_hint(_KAKAO_KEY_NAME, "kakao_api_key", "KAKAO_REST_KEY")


# ════════════════════════════════════════════════
# 좌표 → 도로명 (카카오 역지오코딩)
# ════════════════════════════════════════════════
KAKAO_COORD2ADDR = "https://dapi.kakao.com/v2/local/geo/coord2address.json"


def reverse_roadname(lat, lon, kakao_key, timeout=4.0, use_cache=True, log=print):
    """
    좌표가 놓인 지점의 도로명을 돌려준다. 반환: (도로명, 근거)

    지번은 '그 땅'을, 좌표는 '차가 실제로 서 있던 지점'을 가리킨다.
    도로 위 좌표를 거꾸로 주소로 바꾸면 그 도로의 이름이 그대로 나오므로,
    건물이 없어 도로명주소가 부여되지 않은 나대지·공터에서도 답이 나온다.

    외곽이라 도로명주소 자체가 없으면 ("", "") → 부르는 쪽이 지번을 유지한다.
    """
    if not kakao_key:
        log("[도로명] 카카오 키가 없습니다")
        return "", ""

    # 소수점 5자리면 약 1m. 같은 지점을 다시 부르지 않도록 캐시 키로 쓴다.
    ckey = f"@{round(float(lat), 5)},{round(float(lon), 5)}"
    if use_cache:
        _load_cache()
        hit = _mem_cache.get(ckey)
        if hit:
            log(f"[도로명] 캐시: {ckey} → '{hit}'")
            return hit, "캐시(좌표)"

    params = urllib.parse.urlencode({
        "x": lon,               # 카카오는 x=경도, y=위도
        "y": lat,
        "input_coord": "WGS84",
    })
    req = urllib.request.Request(
        KAKAO_COORD2ADDR + "?" + params,
        headers={"Authorization": f"KakaoAK {kakao_key}",
                 "User-Agent": "parking-macro"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        detail = ""
        body = getattr(e, "read", None)
        if body:
            try:
                detail = " " + body().decode("utf-8", "replace")[:200]
            except Exception:
                pass
        why = _describe_url_error(e)
        if "401" in str(e) or "appKey" in detail:
            # 서버가 답을 준 것이므로 망은 멀쩡하다. 키만 틀렸다.
            why = ("카카오가 키를 거부했습니다 (401). 'REST API 키' 가 맞는지 "
                   "확인하세요 — 영문·숫자 32자리입니다. 액세스 토큰이나 "
                   "JavaScript 키를 넣으면 이 오류가 납니다")
        # '실패' 와 '도로명이 없는 곳' 은 전혀 다른 이야기다. 뭉뚱그리면
        # 연결 문제를 두고 "좌표로도 못 찾는구나" 로 오해하게 된다.
        log(f"[도로명] 좌표 조회 실패({type(e).__name__}): {why}{detail}")
        return "", f"실패: {why}"

    docs = data.get("documents") or []
    if not docs:
        log(f"[도로명] 좌표에 해당하는 주소 없음: {ckey}")
        return "", ""

    road = docs[0].get("road_address") or {}
    rn = (road.get("road_name") or "").strip()
    if not rn:
        jibun = (docs[0].get("address") or {}).get("address_name", "")
        log(f"[도로명] 이 지점에는 도로명주소가 없습니다 ({jibun}) → 지번주소 유지")
        return "", ""

    note = f"좌표 {round(float(lat), 6)},{round(float(lon), 6)}"
    log(f"[도로명] {ckey} → '{rn}' ({road.get('address_name','')})")
    if use_cache:
        _mem_cache[ckey] = rn
        _save_cache()
    return rn, note


def clean_keyword(text):
    """검색어에서 API 가 거부하는 문자를 걸러내고 공백을 정리한다."""
    s = _KEEP.sub(" ", text or "")
    return _SPACES.sub(" ", s).strip()


def _pick_token(text, suffixes):
    """'하북면 순지리 830' 에서 '하북면' 처럼 지역을 가리키는 낱말을 뽑는다."""
    for tok in (text or "").split():
        if any(tok.endswith(s) for s in suffixes):
            return tok
    return ""


def matches_query(juso, dong, sgg):
    """
    돌아온 결과가 정말 우리가 물어본 동네인지 확인한다.

    API 는 못 찾으면 엉뚱한 동네를 그럴듯하게 돌려주기도 한다.
    (시험용 키 TESTJUSOGOKR 은 검색어와 무관하게 서울 양재동 샘플만 돌려준다)
    이걸 안 거르면 '경남 양산시 유산동' 을 물어보고 '서울 강남대로12길' 을
    받아서, 그것도 여러 건이 전부 같은 도로명이니 확실하다고 착각하게 된다.
    과태료가 엉뚱한 위치로 나가는 사고라 반드시 막아야 한다.
    """
    if sgg and sgg not in (juso.get("sggNm") or ""):
        return False
    if dong:
        emd = juso.get("emdNm") or ""
        li = juso.get("liNm") or ""
        if dong not in emd and dong not in li:
            return False
    return True


# 읍·면·동 은 남기고, 리(里)와 번지는 도로명으로 갈아 끼운다.
#   '물금읍 물금리 82'  →  '물금읍' + '물금중앙길'
_ADMIN_SUFFIX = ("읍", "면", "동")
_BONBUN = re.compile(r"(\d+)")


def admin_prefix(jibun):
    """앞에서부터 마지막 읍/면/동 토큰까지를 그대로 돌려준다. 없으면 빈 문자열."""
    toks = (jibun or "").split()
    last = -1
    for i, t in enumerate(toks):
        if any(t.endswith(s) for s in _ADMIN_SUFFIX):
            last = i
    return " ".join(toks[:last + 1]) if last >= 0 else ""


def compose(jibun, rn):
    """칸에 실제로 넣을 값을 만든다. '물금읍' + '물금중앙길' → '물금읍 물금중앙길'"""
    if not rn:
        return ""
    head = admin_prefix(jibun)
    return f"{head} {rn}" if head else rn


def _bonbun(text):
    """'82-3' 또는 '산 82-3' 에서 본번 82 를 뽑는다. 없으면 None."""
    toks = (text or "").split()
    for t in reversed(toks):
        m = _BONBUN.search(t)
        if m:
            return int(m.group(1))
    return None


def is_mountain(jibun):
    """'산 82-3' 처럼 산 지번인지. 산과 일반은 번호 체계가 달라 섞으면 안 된다."""
    return any(t == "산" or t.startswith("산") and t[1:2].isdigit()
               for t in (jibun or "").split())


def looks_like_roadname(text):
    """
    이미 도로명으로 바뀐 칸인지 본다.
    같은 건을 두 번 실행했을 때 멀쩡한 도로명을 다시 조회하지 않기 위한 방어.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _JIBUN_NUM.search(t):        # 159-71 이 남아 있으면 아직 지번
        return False
    return t.endswith("로") or t.endswith("길")


# ────────────────────────────────────────────────
# 캐시 — 같은 동네 위반건이 연달아 들어오므로 적중률이 높다
# ────────────────────────────────────────────────
def _cache_path():
    return os.path.join(_base_dir(), _CACHE_NAME)


def _load_cache():
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _mem_cache.update({k: v for k, v in data.items() if isinstance(v, str)})
    except Exception:
        pass


def _save_cache():
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(_mem_cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ────────────────────────────────────────────────
# 조회
# ────────────────────────────────────────────────
def search(keyword, api_key, api_url=DEFAULT_API_URL, timeout=4.0, count=5):
    """
    API 를 호출해 (juso 목록, 오류메시지) 를 돌려준다.
    오류면 목록은 빈 리스트, 오류메시지에 사유가 담긴다.
    """
    params = urllib.parse.urlencode({
        "currentPage": 1,
        "countPerPage": count,
        "resultType": "json",
        "confmKey": api_key,
        "keyword": keyword,
    })
    req = urllib.request.Request(
        api_url + "?" + params,
        headers={"User-Agent": "Mozilla/5.0 (parking-macro)"},
    )
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl_context()) as resp:
        raw = resp.read().decode("utf-8", "replace")

    data = json.loads(raw)
    results = data.get("results") or {}
    common = results.get("common") or {}
    code = str(common.get("errorCode", ""))
    if code != "0":
        return [], f"{code}={common.get('errorMessage', '')}"
    return (results.get("juso") or []), ""


def _single_roadname(rows):
    """후보들의 도로명이 하나로 모이면 그것을, 갈리면 빈 값을 돌려준다."""
    names = []
    for j in rows:
        rn = (j.get("rn") or "").strip()
        if rn and rn not in names:
            names.append(rn)
    return names[0] if len(names) == 1 else "", names


def lookup_roadname(jibun, region="", api_key="", api_url=DEFAULT_API_URL,
                    timeout=4.0, use_cache=True, log=print):
    """
    지번주소에서 도로명만 뽑는다. 확실하지 않으면 "" 를 돌려준다.
    반환: (도로명, 근거설명)   — 실패하면 ("", "")

    후보가 여러 개 나와도 도로명(rn)이 전부 같으면 채택한다.
    같은 길의 다른 건물번호일 뿐이므로 안전하다.
    도로명이 서로 갈리면 어느 길인지 단정할 수 없으므로 "" 를 돌려준다.

    ※ 이건 건물이 있는 지번에서만 답이 나온다. 도로명주소는 건물에 부여되므로
      나대지·공터에는 아예 없다. 그런 곳은 좌표를 쓰는 reverse_roadname() 이
      맡는다 — 이쪽이 주 경로다.
    """
    jibun = clean_keyword(jibun)
    if not jibun:
        return "", ""
    if not api_key:
        log("[도로명] 승인키가 없습니다")
        return "", ""

    keyword = clean_keyword(f"{region} {jibun}")
    dong = _pick_token(jibun, ("동", "리", "면", "읍", "가"))
    sgg = _pick_token(region, ("시", "군", "구"))

    if use_cache:
        _load_cache()
        hit = _mem_cache.get(keyword)
        if hit:
            log(f"[도로명] 캐시: '{keyword}' → '{hit}'")
            return hit, "캐시"

    try:
        juso, err = search(keyword, api_key, api_url, timeout)
    except Exception as e:
        log(f"[도로명] 조회 실패({type(e).__name__}): {e}")
        return "", ""

    if err:
        log(f"[도로명] API 오류: {err}")
        return "", ""
    if not juso:
        log(f"[도로명] 검색 결과 없음: '{keyword}' → 지번주소 유지")
        return "", ""

    # 물어본 동네가 맞는 결과만 남긴다
    kept = [j for j in juso if matches_query(j, dong, sgg)]
    if not kept:
        got = juso[0]
        log(f"[도로명] 다른 동네가 왔습니다 (찾는 곳: {sgg} {dong} / "
            f"받은 곳: {got.get('sggNm','')} {got.get('emdNm','')}) → 지번주소 유지")
        return "", ""

    rn, names = _single_roadname(kept)
    if not names:
        log(f"[도로명] 결과에 도로명이 없음: '{keyword}' → 지번주소 유지")
        return "", ""
    if not rn:
        # 붙은 길이 여러 개다. 사람이 봐야 하므로 원본을 그대로 둔다.
        log(f"[도로명] 후보가 갈림 {names} → 지번주소 유지")
        return "", ""

    note = f"지번검색, {len(kept)}/{len(juso)}건"
    log(f"[도로명] '{keyword}' → '{rn}' ({note})")
    if use_cache:
        _mem_cache[keyword] = rn
        _save_cache()
    return rn, note


# ────────────────────────────────────────────────
# 단독 실행 : 매크로 없이 API 만 확인
# ────────────────────────────────────────────────
def _load_config():
    """buttons.json 이 옆에 있으면 설정을 읽어 온다."""
    try:
        with open(os.path.join(_base_dir(), "buttons.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return (cfg.get("roadname_api_key", ""),
                cfg.get("roadname_region", ""),
                cfg.get("roadname_api_url", DEFAULT_API_URL),
                cfg.get("kakao_api_key", ""))
    except Exception:
        return "", "", DEFAULT_API_URL, ""


def _coord_test(lat, lon, kakao_key):
    """좌표 하나로 카카오 역지오코딩만 확인한다."""
    print("=" * 55)
    print(f"좌표     : 위도 {lat}  경도 {lon}")
    if not kakao_key:
        print()
        print("[안내] " + kakao_key_hint())
        print()
        print("  카카오 REST 키 받는 법:")
        print("    developers.kakao.com → 로그인 → 내 애플리케이션 → 애플리케이션 추가")
        print("    → 앱 설정/앱 키 → 'REST API 키' 복사")
        print(f"    → 메모장에 그 키만 한 줄 붙여넣고 '{_KAKAO_KEY_NAME}' 로 저장")
        return
    # REST API 키는 영문·숫자 32자리다. 액세스 토큰(60자 넘고 _ - 섞임)을
    # 잘못 넣는 일이 잦아서 부르기 전에 미리 짚어 준다.
    looks_ok = len(kakao_key) == 32 and kakao_key.isalnum()
    print(f"카카오키 : {kakao_key[:8]}… ({len(kakao_key)}자)"
          + ("" if looks_ok else "  ← REST API 키는 32자리입니다. 형식 확인 필요"))
    print("-" * 55)
    rn, note = reverse_roadname(lat, lon, kakao_key, use_cache=False)
    print("-" * 55)
    if rn:
        print(f"도로명      : '{rn}'  ({note})")
        print(f"최종 입력값 : '<읍/면/동> {rn}' 형태로 들어갑니다")
    elif note.startswith("실패"):
        # 연결이 안 된 것과 '그 지점에 도로명이 없는 것' 은 전혀 다르다.
        print("[조회 실패] 카카오 서버에 물어보지도 못했습니다.")
        print(f"  {note[3:].lstrip(': ')}")
        print("  → 도로명이 없다는 뜻이 아닙니다. 연결 문제입니다.")
    else:
        print("도로명 : 이 지점에는 도로명주소가 없습니다 → 지번주소 그대로 둠")
    print("=" * 55)


def main(argv):
    cfg_key, region, api_url, cfg_kakao = _load_config()
    key, key_from = load_api_key(cfg_key)
    kakao_key, _ = load_kakao_key(cfg_kakao)

    coord = None
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--key" and i + 1 < len(argv):
            key = argv[i + 1]
            key_from = "--key"
            i += 2
        elif a == "--kakao" and i + 1 < len(argv):
            kakao_key = argv[i + 1]
            i += 2
        elif a == "--coord" and i + 2 < len(argv):
            coord = (float(argv[i + 1]), float(argv[i + 2]))
            i += 3
        elif a == "--region" and i + 1 < len(argv):
            region = argv[i + 1]
            i += 2
        elif a == "--url" and i + 1 < len(argv):
            api_url = argv[i + 1]
            i += 2
        else:
            args.append(a)
            i += 1

    if coord:
        _coord_test(coord[0], coord[1], kakao_key)
        return

    if not args:
        print(__doc__)
        print("사용법:")
        print('  python road_addr.py "유산동 159-71"                    (지번으로 검색)')
        print('  python road_addr.py --coord 35.357891 129.047210      (좌표로 검색)')
        return

    if not key:
        print("[안내] " + key_search_hint())
        print()
        print("  승인키.txt 만드는 법 — 메모장에 키만 한 줄 붙여넣고")
        print(f"  '{_KEY_NAME}' 이름으로 이 폴더에 저장하면 됩니다.")
        return

    jibun = " ".join(args)
    keyword = clean_keyword(f"{region} {jibun}")

    print("=" * 55)
    print(f"입력   : {jibun}")
    print(f"지역   : {region or '(없음)'}")
    print(f"검색어 : {keyword}")
    print(f"주소   : {api_url}")
    print(f"승인키 : {key[:8]}…  (출처: {key_from})")
    print("-" * 55)

    try:
        juso, err = search(keyword, key, api_url)
    except Exception as e:
        print(f"[실패] 호출 오류: {type(e).__name__}: {e}")
        print("  → 인터넷 연결 / 방화벽 / --url 을 확인하세요.")
        return

    if err:
        print(f"[실패] API 오류: {err}")
        print("  → 승인키가 맞는지, 신청한 API 가 '검색 API' 인지 확인하세요.")
        return

    dong = _pick_token(jibun, ("동", "리", "면", "읍", "가"))
    sgg = _pick_token(region, ("시", "군", "구"))

    print(f"검색 결과 {len(juso)}건")
    hit = 0
    for n, j in enumerate(juso, 1):
        ok = matches_query(j, dong, sgg)
        hit += 1 if ok else 0
        print(f"  {n}. {'○' if ok else '✕'} rn='{j.get('rn','')}'  |  {j.get('roadAddr','')}")
        print(f"       (지번: {j.get('jibunAddr','')})")

    if juso and hit == 0:
        print()
        print(f"  ※ 물어본 곳({sgg} {dong})과 다른 동네만 왔습니다.")
        if key == "TESTJUSOGOKR":
            print("     시험용 키(TESTJUSOGOKR)는 검색어와 상관없이 똑같은 샘플만")
            print("     돌려줍니다. 통신이 되는지만 확인되는 것이고, 실제 조회는")
            print("     본인 승인키를 발급받아야 됩니다.")

    print("-" * 55)
    rn, note = lookup_roadname(jibun, region, key, api_url, use_cache=False)
    if rn:
        print(f"앞부분 유지 : '{admin_prefix(jibun)}'")
        print(f"도로명      : '{rn}'  ({note})")
        print(f"최종 입력값 : '{compose(jibun, rn)}'")
        if note.startswith("추정"):
            print("  ※ 추정값입니다. 지도에서 한 번 확인해 보세요.")
    else:
        print("최종 입력값 : (없음 → 지번주소 그대로 둠)")
    print("=" * 55)


if __name__ == "__main__":
    main(sys.argv[1:])
