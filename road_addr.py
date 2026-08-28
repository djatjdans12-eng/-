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

단독 테스트 (매크로 안 건드리고 API 만 확인)
    python road_addr.py "유산동 159-71"
    python road_addr.py --key TESTJUSOGOKR --region "경남 양산시" "유산동 159-71"
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request

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
_mem_cache = {}
_cache_loaded = False


def clean_keyword(text):
    """검색어에서 API 가 거부하는 문자를 걸러내고 공백을 정리한다."""
    s = _KEEP.sub(" ", text or "")
    return _SPACES.sub(" ", s).strip()


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
    base = os.path.dirname(os.path.abspath(sys.argv[0])) or os.getcwd()
    return os.path.join(base, _CACHE_NAME)


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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")

    data = json.loads(raw)
    results = data.get("results") or {}
    common = results.get("common") or {}
    code = str(common.get("errorCode", ""))
    if code != "0":
        return [], f"{code}={common.get('errorMessage', '')}"
    return (results.get("juso") or []), ""


def lookup_roadname(jibun, region="", api_key="", api_url=DEFAULT_API_URL,
                    timeout=4.0, use_cache=True, log=print):
    """
    지번주소에서 도로명만 뽑는다. 확실하지 않으면 "" 를 돌려준다.

    후보가 여러 개 나와도 도로명(rn)이 전부 같으면 채택한다.
    같은 길의 다른 건물번호일 뿐이므로 안전하다.
    도로명이 서로 갈리면 어느 길인지 단정할 수 없으므로 "" 를 돌려준다.
    """
    jibun = clean_keyword(jibun)
    if not jibun:
        return ""
    if not api_key:
        log("[도로명] 승인키가 없습니다")
        return ""

    keyword = clean_keyword(f"{region} {jibun}")

    if use_cache:
        _load_cache()
        hit = _mem_cache.get(keyword)
        if hit:
            log(f"[도로명] 캐시: '{keyword}' → '{hit}'")
            return hit

    try:
        juso, err = search(keyword, api_key, api_url, timeout)
    except Exception as e:
        log(f"[도로명] 조회 실패({type(e).__name__}): {e}")
        return ""

    if err:
        log(f"[도로명] API 오류: {err}")
        return ""
    if not juso:
        log(f"[도로명] 검색 결과 없음: '{keyword}'")
        return ""

    names = []
    for j in juso:
        rn = (j.get("rn") or "").strip()
        if rn and rn not in names:
            names.append(rn)

    if not names:
        log(f"[도로명] 결과에 도로명이 없음: '{keyword}'")
        return ""
    if len(names) > 1:
        # 붙은 길이 여러 개다. 사람이 봐야 하므로 원본을 그대로 둔다.
        log(f"[도로명] 후보가 갈림 {names} → 지번주소 유지")
        return ""

    rn = names[0]
    log(f"[도로명] '{keyword}' → '{rn}' ({len(juso)}건)")
    if use_cache:
        _mem_cache[keyword] = rn
        _save_cache()
    return rn


# ────────────────────────────────────────────────
# 단독 실행 : 매크로 없이 API 만 확인
# ────────────────────────────────────────────────
def _load_key_and_region_from_config():
    """buttons.json 이 옆에 있으면 승인키와 지역을 거기서 읽어 온다."""
    base = os.path.dirname(os.path.abspath(sys.argv[0])) or os.getcwd()
    try:
        with open(os.path.join(base, "buttons.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return (cfg.get("roadname_api_key", ""),
                cfg.get("roadname_region", ""),
                cfg.get("roadname_api_url", DEFAULT_API_URL))
    except Exception:
        return "", "", DEFAULT_API_URL


def main(argv):
    key, region, api_url = _load_key_and_region_from_config()

    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--key" and i + 1 < len(argv):
            key = argv[i + 1]
            i += 2
        elif a == "--region" and i + 1 < len(argv):
            region = argv[i + 1]
            i += 2
        elif a == "--url" and i + 1 < len(argv):
            api_url = argv[i + 1]
            i += 2
        else:
            args.append(a)
            i += 1

    if not args:
        print(__doc__)
        print("사용법: python road_addr.py [--key 승인키] [--region \"경남 양산시\"] \"유산동 159-71\"")
        return

    if not key:
        print("[안내] 승인키가 없습니다.")
        print("  buttons.json 의 roadname_api_key 에 넣거나 --key 로 넘기세요.")
        print("  시험용: python road_addr.py --key TESTJUSOGOKR \"유산동 159-71\"")
        return

    jibun = " ".join(args)
    keyword = clean_keyword(f"{region} {jibun}")

    print("=" * 55)
    print(f"입력   : {jibun}")
    print(f"지역   : {region or '(없음)'}")
    print(f"검색어 : {keyword}")
    print(f"주소   : {api_url}")
    print(f"승인키 : {key[:8]}…")
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

    print(f"검색 결과 {len(juso)}건")
    for n, j in enumerate(juso, 1):
        print(f"  {n}. rn='{j.get('rn','')}'  |  {j.get('roadAddr','')}")
        print(f"     (지번: {j.get('jibunAddr','')})")

    print("-" * 55)
    rn = lookup_roadname(jibun, region, key, api_url, use_cache=False)
    print(f"최종 입력값 : '{rn}'" if rn else "최종 입력값 : (없음 → 지번주소 그대로 둠)")
    print("=" * 55)


if __name__ == "__main__":
    main(sys.argv[1:])
