# -*- coding: utf-8 -*-
"""
img_click.py — 화면에서 지정한 PNG 와 똑같은 부분을 찾아 클릭한다.

외부 라이브러리 없이 표준 라이브러리(zlib, ctypes)만 사용한다.

  find_and_click("제외여부.png")  →  찾으면 True, 못 찾으면 False

동작
  1) PNG 를 직접 해독 (zlib + 언필터)
  2) 가상 화면 전체를 GDI 로 캡처
  3) 템플릿 첫 줄 바이트열을 각 화면 줄에서 bytes.find 로 훑는다 (C 속도)
     → 후보가 나오면 나머지 줄 전체를 대조
  4) 정확히 일치하는 게 없으면 색을 4비트로 뭉개서 한 번 더 (안티에일리어싱 대비)
  5) 여러 곳에서 발견되면 가장 오른쪽 것을 고른다 (오른쪽 모니터 우선)
"""

import ctypes
import ctypes.wintypes as wt
import os
import struct
import time
import zlib

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                         ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                            ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_uint]

SRCCOPY = 0x00CC0020
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ════════════════════════════════════════════════
# PNG 해독 (표준 라이브러리만)
# ════════════════════════════════════════════════
def load_png_rgb(path):
    """
    PNG 를 읽어 (width, height, RGB 바이트열) 로 돌려준다. 위→아래 순서.
    8비트 비인터레이스만 지원 (캡처도구/그림판 저장본은 모두 여기 해당).
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 파일이 아닙니다")

    w = h = depth = ctype = interlace = None
    idat = []
    palette = b""
    trns = b""

    i = 8
    while i + 8 <= len(data):
        ln = struct.unpack(">I", data[i:i + 4])[0]
        tag = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = body
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
        i += 12 + ln

    if depth != 8:
        raise ValueError(f"{depth}비트 PNG 는 지원하지 않습니다 (8비트로 저장하세요)")
    if interlace:
        raise ValueError("인터레이스 PNG 는 지원하지 않습니다")

    raw = zlib.decompress(b"".join(idat))

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError(f"알 수 없는 색상 방식: {ctype}")
    bpp = channels
    stride = w * bpp

    # ── 필터 해제 ──
    out = bytearray(h * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride

        if ftype == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ftype == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        elif ftype != 0:
            raise ValueError(f"알 수 없는 필터: {ftype}")

        out[y * stride:(y + 1) * stride] = line
        prev = line

    # ── RGB 로 변환 ──
    rgb = bytearray(w * h * 3)
    if ctype == 2:
        rgb[:] = out
    elif ctype == 6:
        rgb[0::3] = out[0::4]
        rgb[1::3] = out[1::4]
        rgb[2::3] = out[2::4]
    elif ctype == 0:
        rgb[0::3] = out
        rgb[1::3] = out
        rgb[2::3] = out
    elif ctype == 4:
        g = out[0::2]
        rgb[0::3] = g
        rgb[1::3] = g
        rgb[2::3] = g
    elif ctype == 3:
        for idx, v in enumerate(out):
            rgb[idx * 3:idx * 3 + 3] = palette[v * 3:v * 3 + 3]

    return w, h, bytes(rgb)


# ════════════════════════════════════════════════
# 화면 캡처
# ════════════════════════════════════════════════
def capture_screen_rgb():
    """가상 화면 전체를 캡처해 (x0, y0, w, h, RGB 바이트열 위→아래) 로 반환."""
    g = user32.GetSystemMetrics
    x0, y0, w, h = g(76), g(77), g(78), g(79)

    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    hbmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, hbmp)
    try:
        gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x0, y0, SRCCOPY)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = h          # 양수 = 아래→위
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
        bgra = buf.raw
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, hdc)

    # BGRA(아래→위) → RGB(위→아래)
    pitch = w * 4
    rows = []
    for y in range(h - 1, -1, -1):
        line = bgra[y * pitch:(y + 1) * pitch]
        r = bytearray(w * 3)
        r[0::3] = line[2::4]
        r[1::3] = line[1::4]
        r[2::3] = line[0::4]
        rows.append(bytes(r))
    return x0, y0, w, h, rows


# ════════════════════════════════════════════════
# 템플릿 찾기
# ════════════════════════════════════════════════
_QUANT = bytes((i & 0xF0) for i in range(256))    # 색을 4비트로 뭉개는 표


def _search(screen_rows, sw, tpl_rows, tw, th, quant=False):
    """일치하는 위치를 모두 찾아 [(x, y), ...] 로 반환."""
    if quant:
        screen_rows = [r.translate(_QUANT) for r in screen_rows]
        tpl_rows = [r.translate(_QUANT) for r in tpl_rows]

    head = tpl_rows[0]
    hits = []
    limit = len(screen_rows) - th

    for y in range(limit + 1):
        row = screen_rows[y]
        start = 0
        while True:
            pos = row.find(head, start)
            if pos < 0:
                break
            if pos % 3 == 0:                       # 픽셀 경계에 맞는 후보만
                x = pos // 3
                ok = True
                for dy in range(1, th):
                    off = x * 3
                    if screen_rows[y + dy][off:off + tw * 3] != tpl_rows[dy]:
                        ok = False
                        break
                if ok:
                    hits.append((x, y))
            start = pos + 1
    return hits


def find_on_screen(png_path, prefer="rightmost", log=print):
    """
    화면에서 png_path 와 같은 그림을 찾는다.
    반환: (중심x, 중심y) 절대좌표  /  못 찾으면 None

    1차: 그림 전체가 픽셀 단위로 똑같은 곳
    2차: 테두리를 15% 잘라낸 중앙부만 똑같은 곳
         (선택 표시나 테두리 강조 때문에 가장자리만 다른 경우가 흔하다)
    3차: 색을 4비트로 뭉개서 대조
    """
    if not os.path.exists(png_path):
        log(f"[이미지] 파일이 없습니다: {png_path}")
        return None

    t0 = time.time()
    tw, th, trgb = load_png_rgb(png_path)
    tpl_rows = [trgb[y * tw * 3:(y + 1) * tw * 3] for y in range(th)]

    x0, y0, sw, sh, rows = capture_screen_rgb()
    if tw > sw or th > sh:
        log(f"[이미지] 템플릿({tw}x{th})이 화면({sw}x{sh})보다 큽니다")
        return None

    # 1차
    hits = _search(rows, sw, tpl_rows, tw, th)
    mode, offx, offy = "정확", 0, 0

    # 2차 : 테두리 15% 잘라낸 중앙부
    if not hits:
        mx, my = max(1, tw * 15 // 100), max(1, th * 15 // 100)
        cw, ch = tw - mx * 2, th - my * 2
        if cw > 3 and ch > 2:
            crop = [tpl_rows[my + dy][mx * 3:(mx + cw) * 3] for dy in range(ch)]
            hits = _search(rows, sw, crop, cw, ch)
            if hits:
                mode, offx, offy = "중앙부", -mx, -my
                tw_eff, th_eff = cw, ch

    # 3차 : 색을 뭉개서
    if not hits:
        hits = _search(rows, sw, tpl_rows, tw, th, quant=True)
        mode = "근사"

    ms = int((time.time() - t0) * 1000)
    if not hits:
        log(f"[이미지] 못 찾음 ({os.path.basename(png_path)} {tw}x{th}, {ms}ms)")
        return None

    if prefer == "rightmost":
        hx, hy = max(hits, key=lambda p: p[0])
    elif prefer == "leftmost":
        hx, hy = min(hits, key=lambda p: p[0])
    else:
        hx, hy = hits[0]

    # 중앙부로 찾았으면 원래 그림 기준으로 좌표를 되돌린다
    cx = x0 + hx + offx + tw // 2
    cy = y0 + hy + offy + th // 2
    log(f"[이미지] {mode}일치 {len(hits)}곳 → 클릭 ({cx}, {cy}) {ms}ms")
    return cx, cy


def click_at(x, y, restore_cursor=True, settle=0.08):
    """지정 좌표를 왼쪽 클릭한다."""
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


def find_and_click(png_path, prefer="rightmost", log=print, restore_cursor=False):
    pt = find_on_screen(png_path, prefer=prefer, log=log)
    if pt is None:
        return False
    click_at(pt[0], pt[1], restore_cursor=restore_cursor)
    return True
