# -*- coding: utf-8 -*-
"""
win_text.py — 창 안에 그려진 글자를 그대로 읽어 온다 (Windows 전용)

민원내용에 이런 줄이 들어 있다:

    * 발생지역 위도:35.35789108276367 경도:129.04721069335938

이 좌표만 얻으면 카카오 역지오코딩으로 그 지점의 도로명을 바로 알 수 있다.
지번은 '그 땅'을 가리키지만 좌표는 '차가 실제로 서 있던 지점'이라, 건물이
없는 나대지·공터여도 도로명이 나온다.

화면에 그려진 진짜 텍스트이므로 Win32 로 컨트롤에서 그대로 긁어 온다.
클릭도 포커스 이동도 없고, OCR 처럼 잘못 읽을 일도 없다.
외부 라이브러리 없이 ctypes 만 쓴다 (img_click.py, plate_ocr.py 와 같은 방침).

※ 업무 프로그램이 표준 윈도우 컨트롤을 쓰지 않으면 텍스트가 안 잡힐 수 있다.
  그래서 아래 진단 기능을 먼저 돌려 확인한다.

진단 (매크로 안 건드림)
    python win_text.py
    → 5초 안에 위반자료 상세관리 창을 클릭
    → 컨트롤 목록과 텍스트, 좌표를 찾았는지 출력
"""

import ctypes
import ctypes.wintypes as wt
import re
import sys
import time

user32 = ctypes.windll.user32

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_void_p]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]

ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumChildWindows.argtypes = [ctypes.c_void_p, ENUMPROC, ctypes.c_void_p]

user32.GetFocus.restype = ctypes.c_void_p
user32.SetFocus.restype = ctypes.c_void_p
user32.SetFocus.argtypes = [ctypes.c_void_p]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, ctypes.c_bool]

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", wt.RECT)]


user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def focused_control():
    """
    지금 글자를 입력받고 있는 컨트롤의 핸들.

    GetFocus 는 자기 스레드 것만 알려주므로, 다른 프로그램의 포커스를 알려면
    GetGUIThreadInfo 로 그 창의 스레드에게 물어봐야 한다.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return None
    return info.hwndFocus or None


def restore_focus(hwnd):
    """
    아까 그 컨트롤로 포커스를 돌려준다.

    SetFocus 는 '입력 큐를 공유하는 스레드' 안에서만 먹으므로,
    AttachThreadInput 으로 잠깐 붙었다가 뗀다.
    """
    if not hwnd:
        return False
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    try:
        if target_tid and target_tid != my_tid:
            attached = bool(user32.AttachThreadInput(my_tid, target_tid, True))
        user32.SetFocus(hwnd)
        return True
    except Exception:
        return False
    finally:
        if attached:
            try:
                user32.AttachThreadInput(my_tid, target_tid, False)
            except Exception:
                pass


def click_at(x, y, settle=0.12, restore_cursor=True):
    """지정 좌표를 왼쪽 클릭한다 (img_click.click_at 과 같은 방식)."""
    before = POINT()
    if restore_cursor:
        user32.GetCursorPos(ctypes.byref(before))

    user32.SetCursorPos(int(x), int(y))
    time.sleep(settle)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(settle)

    if restore_cursor:
        user32.SetCursorPos(before.x, before.y)

# 위도:35.357891  경도:129.047210   (콜론·공백·등호 어떤 조합이든)
_LAT = re.compile(r"위\s*도\s*[:=]?\s*(-?\d{1,3}\.\d+)")
_LON = re.compile(r"경\s*도\s*[:=]?\s*(-?\d{1,3}\.\d+)")

# 대한민국 안에 있는 좌표인지 (엉뚱한 숫자를 좌표로 오인하지 않도록)
LAT_RANGE = (33.0, 39.5)
LON_RANGE = (124.0, 132.0)


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _control_text(hwnd):
    """컨트롤의 글자를 읽는다. WM_GETTEXT 가 비면 창 제목이라도 읽어 본다."""
    n = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, None)
    if n and n > 0:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.SendMessageW(hwnd, WM_GETTEXT, n + 1,
                            ctypes.cast(buf, ctypes.c_void_p))
        if buf.value:
            return buf.value

    n = user32.GetWindowTextLengthW(hwnd)
    if n and n > 0:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    return ""


def iter_child_texts(hwnd):
    """(핸들, 클래스명, 텍스트) 를 창 안의 모든 자식 컨트롤에 대해 돌려준다."""
    found = []

    def cb(child, _lparam):
        try:
            found.append((child, _class_name(child), _control_text(child)))
        except Exception:
            pass
        return True

    user32.EnumChildWindows(hwnd, ENUMPROC(cb), None)
    return found


def _valid(lat, lon):
    return (LAT_RANGE[0] <= lat <= LAT_RANGE[1]
            and LON_RANGE[0] <= lon <= LON_RANGE[1])


def find_coords_in_text(text):
    """글자 뭉치에서 위도·경도를 뽑는다. 못 찾거나 범위 밖이면 None."""
    if not text:
        return None
    mlat, mlon = _LAT.search(text), _LON.search(text)
    if not (mlat and mlon):
        return None
    try:
        lat, lon = float(mlat.group(1)), float(mlon.group(1))
    except ValueError:
        return None
    return (lat, lon) if _valid(lat, lon) else None


def find_coords(hwnd=None, log=None):
    """
    창 안에서 위도·경도를 찾는다. 반환: (위도, 경도) 또는 None.
    hwnd 를 안 주면 지금 활성 창에서 찾는다.
    """
    if hwnd is None:
        hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    # 창 자체 → 자식 컨트롤 순으로 본다
    hit = find_coords_in_text(_control_text(hwnd))
    if hit:
        return hit

    for child, cls, text in iter_child_texts(hwnd):
        hit = find_coords_in_text(text)
        if hit:
            if log:
                log(f"[좌표] {cls} 에서 찾음: 위도 {hit[0]}, 경도 {hit[1]}")
            return hit
    return None


# ════════════════════════════════════════════════
# 진단
# ════════════════════════════════════════════════
def main():
    wait = 5
    print("=" * 60)
    print(f"{wait}초 안에 '위반자료 상세관리' 창을 클릭해서 맨 앞으로 띄우세요.")
    for i in range(wait, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        time.sleep(1)
    print(" " * 20)

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        print("[실패] 활성 창을 찾지 못했습니다")
        return

    title = _control_text(hwnd)
    print(f"활성 창 : {title!r}  (클래스 {_class_name(hwnd)})")
    print("-" * 60)

    kids = iter_child_texts(hwnd)
    print(f"자식 컨트롤 {len(kids)}개")

    shown = 0
    for child, cls, text in kids:
        if not text.strip():
            continue
        shown += 1
        one = text.replace("\r", " ").replace("\n", " ")
        if len(one) > 90:
            one = one[:90] + "…"
        print(f"  [{cls}] {one}")
    print(f"\n글자가 들어 있는 컨트롤 : {shown}개")
    print("-" * 60)

    hit = find_coords(hwnd)
    if hit:
        print(f"[성공] 좌표를 찾았습니다 →  위도 {hit[0]}  경도 {hit[1]}")
        print("       이제 이 좌표로 도로명을 조회할 수 있습니다.")
    else:
        print("[실패] 좌표를 찾지 못했습니다.")
        if shown == 0:
            print("       컨트롤에서 글자가 하나도 안 읽힙니다 —")
            print("       표준 윈도우 컨트롤이 아닌 것 같습니다(OCR 로 읽어야 함).")
        else:
            print("       글자는 읽히는데 '위도/경도' 형태가 없습니다.")
            print("       민원내용이 보이는 상태였는지 확인하고, 위 목록을")
            print("       그대로 복사해서 알려 주세요.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    input("\n엔터를 누르면 종료합니다...")
