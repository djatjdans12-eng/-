# -*- coding: utf-8 -*-
"""
불법주정차 과태료 입력 매크로 (Windows 전용)

사용법
  1) pip install keyboard pyperclip
  2) python parking_macro.py       (관리자 권한 권장)
  3) 위반자료 화면에서 입력할 칸에 커서를 두고 → 이 창의 버튼 클릭

동작  (※ 모든 버튼은 맨 마지막에 Enter 한 번을 추가로 누름)
  [교차로/횡단보도/인도/버스정류장/기타5분]
      이름 입력 → Alt+T → ↓ ↓ → Alt+G → Enter
  [어린이보호구역]
      이름 입력 → Shift+Tab ×2 → ↓ ×1 → Alt+T → ↓ ↓ → Alt+G → Enter
  [소방시설]
      이름 입력 → Shift+Tab ×2 → ↓ ×2 → Alt+T → ↓ ↓ → Alt+G → Enter
  [시간간격 / 각도차이]  ← 루트 추가분
      이름 입력 → Alt+T → ↓ ×5 / ×12 → Alt+G → Enter
  [중복건]  ← 시작 전에 Enter 한 번 (※ "중복건입니다" 안내창 닫기)
      Enter → (대기) → 이름 입력 → Alt+T → ↓ ×8 → Alt+G → Enter

특징
  - 창에 WS_EX_NOACTIVATE 적용 → 버튼을 눌러도 포커스가 넘어오지 않음
  - 한글은 IME 영향을 받지 않도록 클립보드 붙여넣기(Ctrl+V)로 입력, 클립보드는 자동 원복
  - buttons.json 파일로 버튼/단계 수정 가능 (없으면 자동 생성)
  - 단축키 Ctrl+Alt+1 ~ 9 지원 (체크박스로 켜고 끔)
  - 단축키로 실행하면 Ctrl/Alt 에서 손을 뗀 뒤에 입력이 시작됨
    (Alt 가 눌린 채로 Ctrl+V 를 보내면 붙여넣기가 안 되기 때문)
  - buttons.json 의 "pre_enter_labels" 에 적힌 버튼은 시작하자마자 Enter 를
    한 번 눌러 안내창(예: "중복건입니다")을 닫고 나서 본 동작을 시작함
    (이미 만들어 둔 buttons.json 에도 실행 시 자동으로 적용됨)
  - 차량번호는 ` (백틱) 를 눌러 번호판 영역을 드래그하면 OCR 로 읽어 클립보드에
    복사해 준다. 결과는 확인 팝업에서 사람이 눈으로 보고 Ctrl+V 로 넣는다.
    (사진 위치를 고정해 두고 단축키 한 번에 자동 입력하는 기능도 만들어 봤으나,
     윈도우 내장 OCR 은 문서용이라 야외 차 사진 속 번호판을 읽지 못해 걷어냈다.
     자세한 사정은 plate_ocr.py 맨 위 주석 참고.)
"""

import ctypes
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

try:
    import keyboard
    import pyperclip
except ImportError:
    print("[설치 필요] pip install keyboard pyperclip")
    input("엔터를 누르면 종료합니다...")
    sys.exit(1)

try:
    import img_click
except Exception as _e:
    img_click = None
    print(f"[안내] img_click.py 를 불러오지 못했습니다 ({_e}) — 이미지 클릭 없이 실행합니다")

try:
    import plate_ocr
except Exception as _e:      # plate_ocr.py 가 없어도 나머지 기능은 그대로 동작
    plate_ocr = None
    print(f"[안내] plate_ocr.py 를 불러오지 못했습니다 ({_e}) — OCR 기능 없이 실행합니다")

try:
    import road_addr
except Exception as _e:
    road_addr = None
    print(f"[안내] road_addr.py 를 불러오지 못했습니다 ({_e}) — 도로명 변환 없이 실행합니다")


# ────────────────────────────────────────────────
# 경로 / 로그
# ────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(BASE_DIR, "buttons.json")
LOG_PATH = os.path.join(BASE_DIR, "매크로_로그.txt")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ────────────────────────────────────────────────
# 기본 설정 (buttons.json 이 없으면 이 내용으로 생성됨)
#   steps 종류
#     {"type": "paste", "text": "교차로"}       -> 커서 위치에 텍스트 입력
#     {"type": "key", "key": "down", "repeat": 2} -> 키 입력 (repeat 생략 시 1회)
#     {"type": "wait", "sec": 0.3}              -> 대기
# ────────────────────────────────────────────────
DEFAULT_PRE_ENTER_WAIT = 0.35   # 시작 Enter 로 안내창을 닫은 뒤 기다리는 시간(초)


# 위반내용 칸까지는 Tab 7번이다. 그런데 3번째에서 잡히는 칸이 '위반위치'(지번주소)라,
# 거기서 한 번 멈춰 도로명으로 바꾼 뒤 나머지 4번을 마저 누른다.
def tab_to_content():
    return [
        {"type": "key", "key": "tab", "repeat": 3},
        {"type": "roadname"},
        {"type": "key", "key": "tab", "repeat": 4},
    ]


def make_group_a(name):
    return tab_to_content() + [
        {"type": "paste", "key": None, "text": name},
        {"type": "key", "key": "alt+t"},
        {"type": "key", "key": "down", "repeat": 2},
        {"type": "key", "key": "alt+g"},
        {"type": "key", "key": "enter"},
        {"type": "key", "key": "ctrl+right"},
    ]


def make_group_b(name, down_count):
    return tab_to_content() + [
        {"type": "paste", "key": None, "text": name},
        {"type": "key", "key": "shift+tab", "repeat": 2},
        {"type": "key", "key": "down", "repeat": down_count},
        {"type": "key", "key": "alt+t"},
        {"type": "key", "key": "down", "repeat": 2},
        {"type": "key", "key": "alt+g"},
        {"type": "key", "key": "enter"},
        {"type": "key", "key": "ctrl+right"},
    ]


def make_group_c(name, down_count, pre_enter=False):
    """그룹 A 와 흐름은 같고 Alt+T 뒤 ↓ 횟수만 다름 (루트 선택).

    pre_enter=True 면 맨 앞에 Enter + 대기를 넣는다.
    ※ 중복건은 이 방식을 쓰지 않는다. "중복건입니다" 안내창이 매크로가 보낸
      Enter 로는 닫히지 않아서(타이밍 문제로 보임), 사람이 안내창을 직접 닫고
      원하는 칸에 커서를 둔 다음 버튼을 누르는 방식으로 바꿨다.
    """
    head = []
    if pre_enter:
        head = [
            {"type": "key", "key": "enter"},
            {"type": "wait", "sec": DEFAULT_PRE_ENTER_WAIT},
        ]
    return head + [
        {"type": "paste", "key": None, "text": name},
        {"type": "key", "key": "alt+t"},
        {"type": "key", "key": "down", "repeat": down_count},
        {"type": "key", "key": "alt+g"},
        {"type": "key", "key": "enter"},
    ]


DEFAULT_CONFIG = {
    "key_delay": 0.06,      # 키 하나 보내고 쉬는 시간
    "paste_delay": 0.20,    # Ctrl+V 후 대기 시간
    "start_delay": 0.12,    # 버튼 클릭 후 첫 입력까지 대기
    "clip_restore_delay": 0.35,
    "modifier_wait": 2.0,   # 단축키 사용 시 Ctrl/Alt 에서 손 뗄 때까지 기다리는 최대 시간(초)
    "hotkey_suppress": False,  # True 로 두면 Ctrl+Alt+N 이 원래 프로그램에 전달되지 않음
    "hotkeys_on": True,

    # ── 시작 전 Enter (안내창 닫기) ──
    #   여기에 적힌 이름의 버튼은 동작 시작하자마자 Enter 를 한 번 누른다.
    #   ※ 지금은 비어 있다. 중복건에 쓰던 기능인데, "중복건입니다" 안내창이
    #     매크로가 보낸 Enter 로는 닫히지 않아서 뺐다. 안내창은 사람이 직접 닫고,
    #     원하는 칸에 커서를 둔 뒤 버튼을 누르면 그 자리에서 입력이 시작된다.
    "pre_enter_labels": [],
    "pre_enter_wait": DEFAULT_PRE_ENTER_WAIT,

    # ── 차량번호 OCR ──
    "ocr_on": True,
    "ocr_hotkey": "`",          # 이 키를 누르면 드래그 모드
    "ocr_suppress": True,       # True = ` 가 원래 프로그램에 입력되지 않음
    "ocr_scale": 3,             # 캡처 확대 배율 (작은 글자일수록 3~4가 유리)
    "ocr_lang": "ko",
    "ocr_overlay_alpha": 0.30,
    "ocr_popup_seconds": 0,     # 0 = 직접 닫을 때까지 유지
    "tesseract_path": "",       # 윈도우 내장 OCR 이 안 될 때만 경로 지정

    # ── 지번주소 → 도로명 자동 변환 ──
    #   Tab 3번째에서 잡히는 '위반위치' 칸의 지번주소를 도로명으로 바꿔 넣는다.
    #   승인키는 https://business.juso.go.kr → 개발자센터 에서 무료로 발급받는다.
    #   키가 없으면 이 단계는 건너뛴다 (나머지 매크로는 정상 동작).
    #
    #   ※ 승인키는 아래 roadname_api_key 대신 '승인키.txt' 에 넣기를 권한다.
    #     이 buttons.json 은 버튼 단계가 바뀌면 새로 받아 덮어쓰는 파일이라,
    #     여기 적어 두면 갱신할 때마다 키가 지워진다.
    #     찾는 순서: roadname_api_key → 승인키.txt → 환경변수 JUSO_API_KEY
    "roadname_on": True,
    "roadname_api_key": "",
    "roadname_api_url": "https://business.juso.go.kr/addrlink/addrLinkApi.do",
    "roadname_region": "경남 양산시",   # 지번주소 앞에 붙여서 검색할 지역
    "roadname_timeout": 4.0,
    "roadname_cache": True,

    "buttons": [
        {"label": "교차로",        "hotkey": "ctrl+alt+1", "group": "A", "steps": make_group_a("교차로")},
        {"label": "횡단보도",      "hotkey": "ctrl+alt+2", "group": "A", "steps": make_group_a("횡단보도")},
        {"label": "인도",          "hotkey": "ctrl+alt+3", "group": "A", "steps": make_group_a("인도")},
        {"label": "버스정류장",    "hotkey": "ctrl+alt+4", "group": "A", "steps": make_group_a("버스정류장")},
        {"label": "어린이보호구역", "hotkey": "ctrl+alt+5", "group": "B", "steps": make_group_b("어린이보호구역", 1)},
        {"label": "소방시설",      "hotkey": "ctrl+alt+6", "group": "B", "steps": make_group_b("소방시설", 2)},
        {"label": "시간간격",      "hotkey": "ctrl+alt+7", "group": "C", "steps": make_group_c("시간간격", 5)},
        {"label": "중복건",        "hotkey": "ctrl+alt+8", "group": "C",
         "steps": make_group_c("중복건", 8)},
        {"label": "각도차이",      "hotkey": "ctrl+alt+9", "group": "C", "steps": make_group_c("각도차이", 12)},
        {"label": "기타5분",       "hotkey": "ctrl+alt+`", "group": "A", "steps": make_group_a("기타5분")},
    ],
}

CONFIG = dict(DEFAULT_CONFIG)


def _first_key_step(steps):
    """첫 번째 실제 동작 단계를 돌려준다 (앞쪽 wait 는 건너뜀)."""
    for st in steps:
        if st.get("type") == "wait":
            continue
        return st
    return None


def apply_pre_enter(cfg):
    """
    이미 만들어 둔 buttons.json 에도 '시작 Enter' 를 자동으로 넣어 준다.

    대상 : 버튼에 "pre_enter": true 가 있거나, 이름이 pre_enter_labels 에 있는 경우
    이미 Enter 로 시작하면 중복해서 넣지 않는다.
    """
    labels = set(cfg.get("pre_enter_labels", []) or [])
    wait_sec = float(cfg.get("pre_enter_wait", DEFAULT_PRE_ENTER_WAIT))
    patched = []

    for b in cfg.get("buttons", []):
        if not (b.get("pre_enter") or b.get("label") in labels):
            continue

        steps = b.get("steps") or []
        first = _first_key_step(steps)
        if first and first.get("type") == "key" and first.get("key") == "enter":
            continue      # 이미 Enter 로 시작함

        b["steps"] = [
            {"type": "key", "key": "enter"},
            {"type": "wait", "sec": wait_sec},
        ] + steps
        b["pre_enter"] = True
        patched.append(b.get("label", "?"))

    if patched:
        log(f"[보정] 시작 Enter 추가: {', '.join(patched)}")


def load_config():
    global CONFIG
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            log(f"[생성] {CONFIG_PATH}")
        except Exception as e:
            log(f"[오류] buttons.json 생성 실패: {e}")
        CONFIG = json.loads(json.dumps(DEFAULT_CONFIG))
        apply_pre_enter(CONFIG)
        return

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        if not merged.get("buttons"):
            merged["buttons"] = DEFAULT_CONFIG["buttons"]
        CONFIG = merged
        apply_pre_enter(CONFIG)
        log(f"[로딩] 버튼 {len(CONFIG['buttons'])}개")
    except Exception as e:
        log(f"[오류] buttons.json 읽기 실패, 기본값 사용: {e}")
        CONFIG = json.loads(json.dumps(DEFAULT_CONFIG))
        apply_pre_enter(CONFIG)


# ────────────────────────────────────────────────
# Win32 : 창이 포커스를 가져가지 않게 만들기
# ────────────────────────────────────────────────
user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def make_no_activate(root):
    """클릭해도 이 창이 활성화되지 않도록 한다 (원래 프로그램에 포커스 유지)."""
    try:
        hwnd = int(root.wm_frame(), 16)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        log("[설정] 포커스 비활성 창 적용 완료")
    except Exception as e:
        log(f"[경고] 포커스 비활성 설정 실패: {e} (버튼 클릭 시 포커스가 넘어올 수 있음)")


# ────────────────────────────────────────────────
# 매크로 실행
# ────────────────────────────────────────────────
busy = False
busy_lock = threading.Lock()
status_setter = None    # main 에서 연결


def set_status(msg):
    if status_setter:
        try:
            status_setter(msg)
        except Exception:
            pass


MODIFIER_KEYS = ("ctrl", "alt", "shift", "windows")


def modifiers_down():
    """지금 물리적으로 눌려 있는 수정키 목록."""
    down = []
    for k in MODIFIER_KEYS:
        try:
            if keyboard.is_pressed(k):
                down.append(k)
        except Exception:
            pass
    return down


def wait_modifiers_released():
    """
    단축키(Ctrl+Alt+N)로 실행하면 아직 Ctrl/Alt 가 눌려 있는 상태다.
    이때 Ctrl+V 를 보내면 실제로는 Ctrl+Alt+V 가 되어 붙여넣기가 안 된다.
    → 손을 뗄 때까지 기다린 뒤 진행한다.
    """
    timeout = float(CONFIG.get("modifier_wait", 2.0))
    t0 = time.time()
    warned = False

    while modifiers_down():
        if time.time() - t0 > timeout:
            # 계속 눌려 있으면(끼임 등) 강제로 떼어 준다
            for k in MODIFIER_KEYS:
                try:
                    keyboard.release(k)
                except Exception:
                    pass
            log("[경고] 수정키가 계속 눌려 있어 강제 해제 후 진행합니다")
            time.sleep(0.05)
            return False
        if not warned:
            set_status("Ctrl/Alt 에서 손을 떼면 시작합니다…")
            warned = True
        time.sleep(0.03)

    # 키보드 상태가 실제로 반영될 여유
    time.sleep(0.08)
    return True


def paste_text(text):
    """IME 상태와 무관하게 한글을 넣기 위해 클립보드 경유."""
    try:
        old_clip = pyperclip.paste()
    except Exception:
        old_clip = ""

    pyperclip.copy(text)
    time.sleep(0.05)

    # Ctrl+V 직전에 한 번 더 확인 (Alt 가 남아 있으면 붙여넣기가 실패함)
    if modifiers_down():
        wait_modifiers_released()

    keyboard.send("ctrl+v")
    time.sleep(CONFIG.get("paste_delay", 0.20))

    def restore():
        time.sleep(CONFIG.get("clip_restore_delay", 0.35))
        try:
            pyperclip.copy(old_clip)
        except Exception:
            pass

    threading.Thread(target=restore, daemon=True).start()


_CLIP_MARK = "\x00__매크로_표식__\x00"


def read_selected_text(timeout=0.6):
    """
    지금 블럭 잡혀 있는 텍스트를 Ctrl+C 로 읽어 온다.

    먼저 클립보드에 표식을 넣고, 표식이 바뀔 때까지 기다린다.
    그냥 Ctrl+C 하고 바로 읽으면 복사가 실패했을 때 '이전 클립보드 내용'을
    주소로 착각해서 엉뚱한 값을 검색하게 된다.
    """
    try:
        pyperclip.copy(_CLIP_MARK)
    except Exception:
        return ""

    keyboard.send("ctrl+c")

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.03)
        try:
            v = pyperclip.paste()
        except Exception:
            continue
        if v and v != _CLIP_MARK:
            return v.strip()
    return ""


def convert_roadname():
    """
    블럭 잡힌 지번주소를 도로명으로 바꿔 넣는다.

    확실할 때만 덮어쓴다. 못 찾거나 후보가 갈리면 원래 지번주소를 그대로 둔다.
    (엉뚱한 주소로 과태료가 나가면 실제 피해가 생긴다)

    click_image 와 달리 실패해도 매크로를 중단하지 않는다.
    지번주소가 그대로 남아 있을 뿐이라 이후 단계에 영향이 없기 때문이다.
    """
    if road_addr is None or not CONFIG.get("roadname_on", True):
        return

    api_key, key_from = road_addr.load_api_key(
        CONFIG.get("roadname_api_key", ""), log=log)
    if not api_key:
        # 어디를 찾아봤는지 남긴다. 키가 지워진 걸 모르고 쓰는 상황을 막기 위해서다.
        for line in road_addr.key_search_hint().splitlines():
            log("[도로명] " + line)
        return

    try:
        old_clip = pyperclip.paste()
    except Exception:
        old_clip = ""

    try:
        jibun = read_selected_text()
        if not jibun:
            log("[도로명] 칸에서 주소를 읽지 못했습니다 — 원본 유지")
            return
        if road_addr.looks_like_roadname(jibun):
            log(f"[도로명] 이미 도로명입니다: '{jibun}' — 건너뜀")
            return

        set_status("도로명 조회 중…")
        rn = road_addr.lookup_roadname(
            jibun,
            region=CONFIG.get("roadname_region", ""),
            api_key=api_key,
            api_url=CONFIG.get("roadname_api_url", road_addr.DEFAULT_API_URL),
            timeout=float(CONFIG.get("roadname_timeout", 4.0)),
            use_cache=bool(CONFIG.get("roadname_cache", True)),
            log=log,
        )
        if not rn:
            log(f"[도로명] '{jibun}' → 확실한 도로명 없음, 지번주소 그대로 둠")
            set_status("도로명 못 찾음 — 지번주소 유지")
            return

        # Ctrl+C 후에도 칸의 블럭 선택은 유지되므로 Ctrl+V 가 그대로 덮어쓴다
        pyperclip.copy(rn)
        time.sleep(0.05)
        if modifiers_down():
            wait_modifiers_released()
        keyboard.send("ctrl+v")
        time.sleep(CONFIG.get("paste_delay", 0.20))
        log(f"[도로명] '{jibun}' → '{rn}' 입력")
        set_status(f"도로명: {rn}")

    except Exception as e:
        log(f"[도로명] 오류(무시하고 계속): {e}")
    finally:
        # 뒤에 이어지는 paste 단계와 충돌하지 않도록 복원은 동기로 처리한다
        try:
            pyperclip.copy(old_clip)
        except Exception:
            pass


def run_steps(label, steps):
    global busy
    with busy_lock:
        if busy:
            return
        busy = True

    try:
        wait_modifiers_released()

        set_status(f"{label} 실행 중…")
        time.sleep(CONFIG.get("start_delay", 0.12))

        kd = CONFIG.get("key_delay", 0.06)

        for st in steps:
            stype = st.get("type")

            if stype == "paste":
                paste_text(st.get("text", ""))

            elif stype == "key":
                key = st.get("key")
                if not key:
                    continue
                for _ in range(int(st.get("repeat", 1))):
                    keyboard.send(key)
                    time.sleep(kd)

            elif stype == "wait":
                time.sleep(float(st.get("sec", 0.1)))

            elif stype == "roadname":
                convert_roadname()

            elif stype == "click_image":
                # 화면에서 지정한 그림을 찾아 클릭한다.
                # 못 찾으면 절대 다음 단계로 넘어가지 않는다 —
                # 엉뚱한 칸에 Tab/입력이 들어가면 잘못된 처분이 나간다.
                name = st.get("image", "")
                if img_click is None:
                    raise RuntimeError("img_click.py 를 불러오지 못했습니다")
                path = name if os.path.isabs(name) else os.path.join(BASE_DIR, name)
                if not img_click.find_and_click(
                    path,
                    prefer=st.get("prefer", "rightmost"),
                    log=log,
                    restore_cursor=bool(st.get("restore_cursor", False)),
                ):
                    raise RuntimeError(f"화면에서 '{name}' 을(를) 찾지 못했습니다")
                time.sleep(float(st.get("after", 0.25)))

        log(f"[완료] {label}")
        set_status(f"{label} 완료")

    except Exception as e:
        log(f"[오류] {label} 실행 중 예외: {e}")
        set_status(f"{label} 오류: {e}")
    finally:
        with busy_lock:
            busy = False


def fire(label, steps):
    threading.Thread(target=run_steps, args=(label, steps), daemon=True).start()


# ────────────────────────────────────────────────
# 단축키 등록/해제
# ────────────────────────────────────────────────
registered_hotkeys = []
ocr = None          # main 에서 PlateOCR 인스턴스 연결


def register_hotkeys():
    unregister_hotkeys()

    # 차량번호 OCR 단축키 (기본 ` )
    if ocr is None:
        log("[단축키] OCR 인스턴스가 없어 건너뜀 (plate_ocr 미로딩)")
    elif not CONFIG.get("ocr_on", True):
        log("[단축키] ocr_on=false 이므로 건너뜀")
    else:
        hk = CONFIG.get("ocr_hotkey", "`")
        sup = bool(CONFIG.get("ocr_suppress", True))

        def _ocr_fire(*_a):
            # keyboard 콜백은 별도 스레드에서 온다. 여기서 예외가 나면
            # 아무 일도 안 일어난 것처럼 보이므로 반드시 로그를 남긴다.
            try:
                log("[OCR] 단축키 감지 → 드래그 모드")
                ocr.trigger()
            except Exception as e:
                log(f"[OCR] trigger 실패: {e}")

        ok = False
        # 1순위: 단일 키는 on_press_key 가 add_hotkey 보다 안정적이다
        if "+" not in hk:
            try:
                keyboard.on_press_key(hk, _ocr_fire, suppress=sup)
                registered_hotkeys.append(("__ocr_key__", hk))
                log(f"[단축키] OCR '{hk}' 등록 (on_press_key, suppress={sup})")
                ok = True
            except Exception as e:
                log(f"[경고] on_press_key 실패 {hk}: {e}")
        # 2순위: 조합키이거나 위가 실패한 경우
        if not ok:
            try:
                h = keyboard.add_hotkey(hk, _ocr_fire, suppress=sup)
                registered_hotkeys.append(h)
                log(f"[단축키] OCR '{hk}' 등록 (add_hotkey, suppress={sup})")
                ok = True
            except Exception as e:
                log(f"[경고] add_hotkey 실패 {hk}: {e}")
        # 3순위: 억제 없이라도 등록
        if not ok:
            try:
                h = keyboard.add_hotkey(hk, _ocr_fire, suppress=False)
                registered_hotkeys.append(h)
                log(f"[단축키] OCR '{hk}' 등록 (suppress 없이)")
            except Exception as e:
                log(f"[경고] OCR 단축키 등록 완전 실패 {hk}: {e}")

    for b in CONFIG["buttons"]:
        hk = b.get("hotkey")
        if not hk:
            continue
        try:
            h = keyboard.add_hotkey(
                hk,
                lambda lb=b["label"], stp=b["steps"]: fire(lb, stp),
                suppress=bool(CONFIG.get("hotkey_suppress", False)),
            )
            registered_hotkeys.append(h)
        except Exception as e:
            log(f"[경고] 단축키 등록 실패 {hk}: {e}")
    log(f"[단축키] {len(registered_hotkeys)}개 등록")


def unregister_hotkeys():
    for h in registered_hotkeys:
        try:
            if isinstance(h, tuple) and h and h[0] == "__ocr_key__":
                keyboard.unhook_key(h[1])      # on_press_key 로 건 것
            else:
                keyboard.remove_hotkey(h)
        except Exception:
            pass
    registered_hotkeys.clear()


# ────────────────────────────────────────────────
# UI
# ────────────────────────────────────────────────
def main():
    global status_setter, ocr

    load_config()

    # 캡처 좌표와 화면 좌표를 일치시킨다 (반드시 tk.Tk() 이전)
    if plate_ocr is not None and CONFIG.get("ocr_on", True):
        plate_ocr.enable_dpi_awareness()

    root = tk.Tk()
    root.title("주정차 매크로")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass

    pad = {"padx": 4, "pady": 3}

    frm = ttk.Frame(root, padding=8)
    frm.grid(row=0, column=0, sticky="nsew")

    buttons = CONFIG["buttons"]
    group_a = [b for b in buttons if b.get("group", "A") == "A"]
    group_b = [b for b in buttons if b.get("group", "A") == "B"]
    group_c = [b for b in buttons if b.get("group", "A") == "C"]

    def down_count_of(b):
        """Alt+T 뒤에 오는 ↓ 횟수 (버튼에 표시용)."""
        seen_alt_t = False
        for st in b.get("steps", []):
            if st.get("type") != "key":
                continue
            if st.get("key") == "alt+t":
                seen_alt_t = True
            elif seen_alt_t and st.get("key") == "down":
                return int(st.get("repeat", 1))
        return None

    def add_section(parent, title, items, start_row, show_down=False):
        ttk.Label(parent, text=title, foreground="#555").grid(
            row=start_row, column=0, columnspan=2, sticky="w", pady=(4, 2)
        )
        r = start_row + 1
        c = 0
        for b in items:
            text = b["label"]
            sub = []
            if b.get("pre_enter"):
                sub.append("⏎먼저")
            if show_down and down_count_of(b):
                sub.append(f"↓{down_count_of(b)}")
            if b.get("hotkey"):
                sub.append(b["hotkey"].replace("ctrl+alt+", "Ctrl+Alt+"))
            if sub:
                text += "\n" + " · ".join(sub)
            btn = tk.Button(
                parent,
                text=text,
                width=13,
                height=2,
                command=lambda lb=b["label"], stp=b["steps"]: fire(lb, stp),
            )
            btn.grid(row=r, column=c, sticky="ew", **pad)
            c += 1
            if c == 2:
                c = 0
                r += 1
        return r + (1 if c else 0)

    next_row = add_section(frm, "▷ 이름 + Alt+T + ↓↓ + Alt+G", group_a, 0)
    next_row = add_section(frm, "▷ 이름 + Shift+Tab×2 + ↓ + Alt+T…", group_b, next_row)
    if group_c:
        next_row = add_section(
            frm, "▷ 이름 + Alt+T + ↓×N + Alt+G (루트)", group_c, next_row, show_down=True
        )

    ttk.Separator(frm, orient="horizontal").grid(
        row=next_row, column=0, columnspan=2, sticky="ew", pady=6
    )
    next_row += 1

    # ── 차량번호 OCR ──
    if plate_ocr is not None and CONFIG.get("ocr_on", True):
        ocr = plate_ocr.PlateOCR(root, CONFIG, log=log, status=set_status)
        ocr.warmup()      # 엔진 미리 로딩 → 첫 인식도 빠르게
        tk.Button(
            frm,
            text=f"차량번호 읽기\n{CONFIG.get('ocr_hotkey', '`')} · 드래그",
            width=13, height=2,
            command=lambda: (ocr.trigger()),
        ).grid(row=next_row, column=0, columnspan=2, sticky="ew", **pad)
        next_row += 1

    hk_var = tk.BooleanVar(value=bool(CONFIG.get("hotkeys_on", True)))

    def toggle_hotkeys():
        if hk_var.get():
            register_hotkeys()
            set_status("단축키 켜짐")
        else:
            unregister_hotkeys()
            set_status("단축키 꺼짐")

    ttk.Checkbutton(frm, text="단축키 사용", variable=hk_var, command=toggle_hotkeys).grid(
        row=next_row, column=0, sticky="w", padx=4
    )

    def do_reload():
        load_config()
        set_status("설정 다시 불러옴 (버튼 반영은 재시작 필요)")
        if hk_var.get():
            register_hotkeys()

    ttk.Button(frm, text="설정 새로고침", command=do_reload).grid(
        row=next_row, column=1, sticky="e", padx=4
    )
    next_row += 1

    status = tk.StringVar(value="대기 중 — 입력할 칸에 커서를 두고 버튼을 누르세요")
    lbl = ttk.Label(frm, textvariable=status, foreground="#0a6", wraplength=250)
    lbl.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(6, 0), padx=4)

    status_setter = status.set

    # 창이 뜬 뒤에 포커스 비활성 스타일 적용
    root.update_idletasks()
    root.after(50, lambda: make_no_activate(root))

    if hk_var.get():
        register_hotkeys()

    log("[실행] 매크로 창 시작")

    def on_close():
        unregister_hotkeys()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"[치명적 오류] {e}")
        import traceback
        traceback.print_exc()
        input("엔터를 누르면 종료합니다...")