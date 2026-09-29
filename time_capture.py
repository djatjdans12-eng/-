# -*- coding: utf-8 -*-
"""
time_capture.py — 위반자료 상세관리 창을 한 건씩 자동 캡처하고 '>' 로 넘긴다 (Windows 전용)

사용
  pip install pillow keyboard openpyxl
  python time_capture.py            (관리자 권한 권장)
  1) 첫 번째 건의 '위반자료 상세관리' 창을 열고 그 창을 클릭해 앞으로 가져온다
  2) F8 을 누른다  → 자동으로 캡처 → '>' 클릭 → 캡처 … 반복  (Esc 로 중단)
  3) 마지막 건에서 '>' 를 눌러도 화면이 안 바뀌면 끝난 것으로 보고 멈춘다
  4) 끝나면 바로 time_check.py 로 엑셀까지 만든다

옵션
  --max 300      최대 건수 (기본 300)
  --close        끝난 뒤 Alt+F4 로 창을 닫는다 (기본은 열어 둠)
  --no-check     캡처만 하고 엑셀은 만들지 않음
  --out 폴더     저장 위치 (기본: time_check.DEFAULT_BASE 아래 날짜시각 폴더)

안전장치
  · F8 을 누른 순간 맨 앞에 있는 창의 제목에 '위반자료' 가 없으면 아무것도 누르지 않고 종료
  · '>' 를 누르면 창이 닫히고 새 창이 열리므로 매번 '위반자료' 창을 다시 찾음
  · 다른 창이 앞으로 나와 있으면 멈춤 (엉뚱한 곳을 클릭하지 않도록)
  · '>' 버튼 위치는 창 크기에 대한 비율로 계산 (기준 1442x1006 에서 (178, 980))
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
from datetime import datetime

try:
    import keyboard
    from PIL import Image
except ImportError:
    print("[설치 필요] pip install pillow keyboard openpyxl")
    input("엔터를 누르면 종료합니다...")
    sys.exit(1)

import img_click
import plate_ocr
import time_check

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.IsWindow.argtypes = [ctypes.c_void_p]

NEXT_BTN = (178, 980)           # 기준 창(1442x1006) 안의 '>' 버튼 중심
TITLE_HINT = "위반자료"


def win_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]


def find_target():
    """제목에 '위반자료' 가 든 보이는 창 중 맨 위(Z순서 첫째) 창. 없으면 None."""
    found = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and TITLE_HINT in win_title(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), None)
    return found[0] if found else None


def win_rect(hwnd):
    """보이는 창 영역(그림자 제외). 실패하면 GetWindowRect."""
    r = wt.RECT()
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r))   # EXTENDED_FRAME_BOUNDS
        if hr == 0 and r.right > r.left:
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def grab(hwnd):
    x, y, w, h = win_rect(hwnd)
    sw, sh, bgra = plate_ocr.grab_region(x, y, w, h, scale=1.0, pad=0)
    return Image.frombuffer("RGBA", (sw, sh), bgra, "raw", "BGRA", 0, -1).convert("RGB")


def grab_stable(hwnd, tries=6, gap=0.15):
    """화면이 다 그려질 때까지 (연속 두 번 같을 때) 기다렸다가 캡처."""
    prev = grab(hwnd)
    for _ in range(tries):
        time.sleep(gap)
        cur = grab(hwnd)
        if cur.tobytes() == prev.tobytes():
            return cur
        prev = cur
    return prev


def click_next(hwnd):
    x, y, w, h = win_rect(hwnd)
    img_click.click_at(x + w * NEXT_BTN[0] / time_check.BASE_W,
                       y + h * NEXT_BTN[1] / time_check.BASE_H, restore_cursor=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=300)
    ap.add_argument("--close", action="store_true")
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()

    plate_ocr.enable_dpi_awareness()
    print("첫 건의 '위반자료 상세관리' 창을 클릭해 앞으로 가져온 뒤 F8 을 누르세요. (Esc = 중단)")
    keyboard.wait("f8")

    hwnd = user32.GetForegroundWindow()
    title = win_title(hwnd)
    if TITLE_HINT not in title:
        print(f"[중단] 맨 앞 창이 위반자료 화면이 아닙니다: '{title}'")
        return 1

    base = a.out or time_check.DEFAULT_BASE
    folder = os.path.join(base, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    print(f"저장 폴더: {folder}")

    prev, n, reason = None, 0, ""
    while n < a.max:
        if keyboard.is_pressed("esc"):
            reason = "Esc 로 중단"
            break
        # '>' 를 누르면 창이 닫히고 새 창이 열리므로, 매번 창을 새로 찾는다
        hwnd = find_target()
        if hwnd is None:
            reason = "위반자료 창을 찾지 못함 (마지막 건이거나 창이 닫힘)"
            break
        if user32.GetForegroundWindow() != hwnd:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.25)
            if user32.GetForegroundWindow() != hwnd:
                reason = "다른 창이 앞으로 나와서 중단"
                break

        img = grab_stable(hwnd)
        if prev is not None and img.tobytes() == prev.tobytes():
            reason = "화면이 더 이상 바뀌지 않음 (마지막 건)"
            break
        n += 1
        img.save(os.path.join(folder, f"{n:03d}.png"))
        prev = img
        print(f"  캡처 {n}", end="\r")

        click_next(hwnd)
        # 새 창(또는 바뀐 내용)이 나타날 때까지 최대 6초 대기
        t0 = time.time()
        while time.time() - t0 < 6:
            time.sleep(0.2)
            nh = find_target()
            if nh is not None and nh != hwnd:
                time.sleep(0.4)                    # 새 창이 다 그려질 시간
                break
            if nh is not None and grab(nh).tobytes() != prev.tobytes():
                break
    else:
        reason = f"최대 {a.max}건에 도달"

    print(f"\n캡처 {n}건 완료 — {reason}")
    if a.close:
        keyboard.send("alt+f4")
    if n and not a.no_check:
        time_check.run(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
