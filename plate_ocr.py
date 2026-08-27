# -*- coding: utf-8 -*-
"""
plate_ocr.py  —  화면 드래그 → 차량번호 OCR → 클립보드 (Windows 전용)

parking_macro.py 에서 import 해서 사용한다.

흐름
  ` (백틱) 누름
    → 현재 활성 창 기억
    → 화면 전체에 반투명 오버레이 → 마우스로 번호판 영역 드래그
    → GDI 로 그 영역만 캡처 (3배 확대 + HALFTONE 보간)
    → OCR (winsdk → Tesseract → 없으면 이미지만 표시)
    → 번호판 패턴 추출 + 숫자칸 오인식 보정
    → 클립보드에 복사 + 확인 팝업(원본 이미지 + 결과, 수정 가능)
    → 아까 기억해 둔 창으로 포커스 복귀

원칙
  결과를 필드에 자동 입력하지 않는다. 사람이 눈으로 보고 Ctrl+V 한다.
  (3/8, 0/O, 가/거 오인식 → 엉뚱한 차주에게 과태료 → 이의신청)

※ 한때 "사진 영역을 고정해 두고 단축키 한 번으로 자동 입력" 기능을 붙였다가
  걷어냈다. 원인은 코드가 아니라 도구가 용도에 안 맞아서다 —
  여기 쓰는 윈도우 내장 OCR 은 '문서' OCR 이라, 야외에서 찍힌 차 사진 속
  번호판을 찾아 읽는 일(ANPR)은 애초에 다른 분야다. 실제로 로그를 보면
  번호판 대신 배경 간판이나 사진 위 '촬영일시' 자막을 읽어 왔고,
  번호판만 딱 잘라 넣은 경우조차 화면 표시 해상도에서는 읽지 못했다.
  배율을 더 키워도 없는 정보가 생기지는 않는다. 다시 시도하려면
  전용 ANPR 엔진/API 가 필요하다.
"""

import ctypes
import ctypes.wintypes as wt
import os
import re
import struct
import sys
import tempfile
import threading
import time
import tkinter as tk
import zlib

try:
    import pyperclip
except ImportError:
    pyperclip = None


# ════════════════════════════════════════════════
# Win32 기본
# ════════════════════════════════════════════════
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.StretchBlt.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_ulong,
]
# ※ 64비트에서 DC/BITMAP 핸들은 2^31 을 넘을 수 있다.
#    argtypes 를 지정하지 않으면 ctypes 가 c_int 로 넘기려다
#    "OverflowError: int too long to convert" 로 죽는다.
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
]

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
HALFTONE = 4

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


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


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
gdi32.PlgBlt.restype = ctypes.c_int
gdi32.PlgBlt.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
]
gdi32.GetStockObject.restype = ctypes.c_void_p
gdi32.GetStockObject.argtypes = [ctypes.c_int]


def enable_dpi_awareness():
    """
    캡처 좌표와 tkinter 좌표를 일치시킨다.
    이 호출을 빼면 배율 125%/150% 환경에서 엉뚱한 영역이 잘린다.
    ※ tk.Tk() 를 만들기 '전에' 호출해야 한다.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
        return True
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def virtual_screen_rect():
    """멀티 모니터 전체를 감싸는 사각형 (x, y, w, h)."""
    g = user32.GetSystemMetrics
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


# ════════════════════════════════════════════════
# 화면 캡처 (GDI, 외부 라이브러리 없음)
# ════════════════════════════════════════════════
def grab_region(x, y, w, h, scale=3.0, pad=None):
    """
    화면의 (x, y, w, h) 영역을 scale 배로 확대해 캡처한다.
    작은 글자는 확대해서 넣어야 OCR 정확도가 눈에 띄게 올라간다.
    글자에 딱 붙게 드래그하면 인식률이 떨어지므로 흰 여백을 둘러 준다.
    반환: (width, height, BGRA bytes  ※ bottom-up 순서)
    """
    iw, ih = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    # 여백은 글자 높이에 비례해야 한다. 고정값을 쓰면 크게 확대했을 때
    # 가장자리 글자가 '잘린 글자' 로 판정돼 통째로 버려진다.
    if pad is None:
        pad = max(24, int(ih * 0.45))
    sw, sh = iw + pad * 2, ih + pad * 2

    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, sw, sh)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        rc = RECT(0, 0, sw, sh)
        user32.FillRect(hdc_mem, ctypes.byref(rc), gdi32.GetStockObject(0))
        gdi32.SetStretchBltMode(hdc_mem, HALFTONE)
        # CAPTUREBLT 를 쓰면 반투명(레이어드) 창까지 함께 찍힌다.
        # 우리 오버레이가 30% 검게 덮인 채로 잡히므로 절대 켜면 안 된다.
        gdi32.StretchBlt(hdc_mem, pad, pad, iw, ih,
                         hdc_screen, x, y, w, h,
                         SRCCOPY)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = sw
        bmi.bmiHeader.biHeight = sh          # 양수 = bottom-up (BMP 파일과 같은 순서)
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB

        buf = ctypes.create_string_buffer(sw * sh * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, sh, buf, ctypes.byref(bmi), 0)
        return sw, sh, buf.raw
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def grab_region_rotated(x, y, w, h, scale=1.0, angle=0.0):
    """
    영역을 캡처하면서 angle(도) 만큼 회전시킨다.
    OCR 엔진은 글자가 수평일 때를 전제로 하므로, 비스듬히 찍힌 번호판은
    몇 도만 돌려줘도 인식률이 크게 달라진다.
    회전은 GDI 의 PlgBlt(평행사변형 전송)로 처리한다 — 외부 라이브러리 없음.
    """
    import math

    iw = max(1, int(round(w * scale)))
    ih = max(1, int(round(h * scale)))
    rad = math.radians(angle)
    cs, sn = math.cos(rad), math.sin(rad)

    # 회전 후 필요한 캔버스 크기 + 여백
    rw = int(abs(iw * cs) + abs(ih * sn))
    rh = int(abs(iw * sn) + abs(ih * cs))
    pad = max(24, int(ih * 0.45))
    dw, dh = rw + pad * 2, rh + pad * 2

    hdc_screen = user32.GetDC(None)
    src_dc = gdi32.CreateCompatibleDC(hdc_screen)
    src_bmp = gdi32.CreateCompatibleBitmap(hdc_screen, iw, ih)
    src_old = gdi32.SelectObject(src_dc, src_bmp)

    dst_dc = gdi32.CreateCompatibleDC(hdc_screen)
    dst_bmp = gdi32.CreateCompatibleBitmap(hdc_screen, dw, dh)
    dst_old = gdi32.SelectObject(dst_dc, dst_bmp)

    try:
        # 1) 화면 → 원본 비트맵 (배율 적용)
        gdi32.SetStretchBltMode(src_dc, HALFTONE)
        gdi32.StretchBlt(src_dc, 0, 0, iw, ih, hdc_screen, x, y, w, h, SRCCOPY)

        # 2) 캔버스를 흰색으로 채운다
        rc = RECT(0, 0, dw, dh)
        user32.FillRect(dst_dc, ctypes.byref(rc), gdi32.GetStockObject(0))

        # 3) 원본 네 귀퉁이 중 셋을 중심 기준으로 회전시켜 평행사변형을 만든다
        cx, cy = dw / 2.0, dh / 2.0
        hw, hh = iw / 2.0, ih / 2.0

        def rot(px, py):
            return (int(round(cx + px * cs - py * sn)),
                    int(round(cy + px * sn + py * cs)))

        pts = (POINT * 3)()
        pts[0] = POINT(*rot(-hw, -hh))   # 좌상
        pts[1] = POINT(*rot(hw, -hh))    # 우상
        pts[2] = POINT(*rot(-hw, hh))    # 좌하

        gdi32.SetStretchBltMode(dst_dc, HALFTONE)
        ok = gdi32.PlgBlt(dst_dc, pts, src_dc, 0, 0, iw, ih, None, 0, 0)
        if not ok:
            raise RuntimeError("PlgBlt 실패")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = dw
        bmi.bmiHeader.biHeight = dh
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(dw * dh * 4)
        gdi32.GetDIBits(dst_dc, dst_bmp, 0, dh, buf, ctypes.byref(bmi), 0)
        return dw, dh, buf.raw
    finally:
        gdi32.SelectObject(src_dc, src_old)
        gdi32.DeleteObject(src_bmp)
        gdi32.DeleteDC(src_dc)
        gdi32.SelectObject(dst_dc, dst_old)
        gdi32.DeleteObject(dst_bmp)
        gdi32.DeleteDC(dst_dc)
        user32.ReleaseDC(None, hdc_screen)


def save_bmp(path, w, h, bgra):
    """OCR 엔진에 넘길 BMP 파일 저장."""
    pitch = w * 4
    header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(bgra), 0, 0, 14 + 40)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, len(bgra), 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header)
        f.write(info)
        f.write(bgra)


def save_png(path, w, h, bgra):
    """
    확인 팝업에 띄울 PNG. tkinter 기본 PhotoImage 가 PNG 는 읽을 수 있어서
    Pillow 없이도 미리보기가 가능하다. (표준 라이브러리 zlib 만 사용)
    """
    rows = []
    pitch = w * 4
    for row in range(h - 1, -1, -1):                 # bottom-up → top-down
        line = bgra[row * pitch: row * pitch + pitch]
        rgb = bytearray(w * 3)
        rgb[0::3] = line[2::4]                       # R
        rgb[1::3] = line[1::4]                       # G
        rgb[2::3] = line[0::4]                       # B
        rows.append(b"\x00" + bytes(rgb))            # filter type 0
    raw = zlib.compress(b"".join(rows), 6)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", raw))
        f.write(chunk(b"IEND", b""))


def _to_gray_bytes(bgra):
    """BGRA → 8비트 회색 바이트열."""
    b = bgra[0::4]
    g = bgra[1::4]
    r = bgra[2::4]
    return bytes((rr * 77 + gg * 150 + bb * 29) >> 8
                 for rr, gg, bb in zip(r, g, b))


def variant_contrast(bgra, invert=False):
    """
    회색조 + 대비 강화(하위/상위 5% 잘라내기). 필요하면 흑백 반전.
    초록 구형판(흰 글자)이나 흐린 화면 캡처에서 1차 인식이 실패했을 때 쓴다.
    """
    gray = _to_gray_bytes(bgra)

    hist = [0] * 256
    for v in gray:
        hist[v] += 1
    total = len(gray)
    cut = max(1, total // 20)

    acc, lo = 0, 0
    for i in range(256):
        acc += hist[i]
        if acc >= cut:
            lo = i
            break
    acc, hi = 0, 255
    for i in range(255, -1, -1):
        acc += hist[i]
        if acc >= cut:
            hi = i
            break
    if hi <= lo:
        lo, hi = 0, 255

    span = hi - lo
    lut = bytearray(256)
    for i in range(256):
        v = int((i - lo) * 255 / span)
        v = 0 if v < 0 else (255 if v > 255 else v)
        lut[i] = 255 - v if invert else v
    gray = gray.translate(bytes(lut))

    out = bytearray(len(bgra))
    out[0::4] = gray
    out[1::4] = gray
    out[2::4] = gray
    out[3::4] = b"\xff" * len(gray)
    return bytes(out)


# ════════════════════════════════════════════════
# OCR 엔진
# ════════════════════════════════════════════════
class OcrBackend:
    """사용 가능한 엔진을 한 번만 탐지해서 재사용."""

    def __init__(self, lang="ko", tesseract_path="", log=print):
        self.lang = lang
        self.tesseract_path = tesseract_path
        self.log = log
        self.kind = None
        self._winsdk = None
        self._detect()

    def _detect(self):
        # 1순위 : winsdk 파이썬 바인딩 (있으면 가장 빠름)
        try:
            self._winsdk = _WinSdkOcr(self.lang)
            self.kind = "winsdk"
            self.log("[OCR] winsdk 바인딩 사용")
            return
        except Exception as e:
            self.log(f"[OCR] winsdk 사용 불가 ({type(e).__name__}) → PowerShell 경유로 전환")

        # 2순위 : PowerShell 경유 윈도우 내장 OCR (설치 불필요, 상주 프로세스)
        try:
            self._ps = _PowerShellOcr(self.lang, log=self.log)
            self.kind = "powershell"
            return
        except Exception as e:
            self.log(f"[OCR] PowerShell OCR 사용 불가: {e}")

        # 3순위 : Tesseract 실행파일
        exe = self.tesseract_path or _find_tesseract()
        if exe:
            self.tesseract_path = exe
            self.kind = "tesseract"
            self.log(f"[OCR] Tesseract 사용: {exe}")
            return

        self.kind = None
        self.log("[OCR] 인식 엔진 없음 → 확대 이미지만 표시합니다")

    def read(self, bmp_path):
        if self.kind == "winsdk":
            return self._winsdk.read(bmp_path)
        if self.kind == "powershell":
            return self._ps.read(bmp_path)
        if self.kind == "tesseract":
            return _tesseract_read(self.tesseract_path, bmp_path)
        return ""

    def close(self):
        if getattr(self, "_ps", None):
            self._ps.close()


# ── PowerShell 5.1 을 상주시켜 WinRT OCR 을 호출 ──────────────
#   winsdk 는 Python 3.14 용 휠이 없어 컴파일이 필요하지만,
#   PowerShell 5.1 은 .NET Framework 기반이라 WinRT 에 그냥 접근된다.
#   프로세스를 매번 띄우면 0.5초씩 까먹으므로 한 번 띄워 두고 재사용한다.
_PS_WORKER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($op, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}

$engine = $null
try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('__LANG__')) } catch {}
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { Write-Output '###FAIL###OCR 언어팩 없음'; exit 1 }

Write-Output ('###READY###' + $engine.RecognizerLanguage.LanguageTag)

while ($true) {
    $path = [Console]::In.ReadLine()
    if ($null -eq $path -or $path -eq '###QUIT###') { break }
    try {
        $file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bmp     = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $res     = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
        Write-Output ($res.Text -replace "`r`n", ' ' -replace "`n", ' ')
        $stream.Dispose()
        $bmp.Dispose()
    } catch {
        Write-Output ('###ERR###' + $_.Exception.Message)
    }
    Write-Output '###EOF###'
}
"""


class _PowerShellOcr:
    def __init__(self, lang="ko", log=print):
        self.log = log
        self.lang = lang
        self.proc = None
        self.lock = threading.Lock()
        self._start()

    @staticmethod
    def _ps_path():
        p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         r"System32\WindowsPowerShell\v1.0\powershell.exe")
        if not os.path.exists(p):
            raise RuntimeError("powershell.exe 없음")
        return p

    def _start(self):
        import base64
        import subprocess

        script = _PS_WORKER.replace("__LANG__", self.lang)
        enc = base64.b64encode(script.encode("utf-16-le")).decode()

        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self.proc = subprocess.Popen(
            [self._ps_path(), "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            startupinfo=si, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )

        # READY 신호 대기 (엔진 로딩까지 보통 1~3초)
        line = self._readline(timeout=25)
        if not line or not line.startswith("###READY###"):
            self.close()
            raise RuntimeError(f"엔진 준비 실패: {line!r}")
        self.log(f"[OCR] PowerShell 내장 OCR 사용 (언어: {line[11:].strip()})")

    def _readline(self, timeout=15):
        """PowerShell 이 멈춰도 프로그램 전체가 굳지 않도록 시간 제한을 둔다."""
        box = {}

        def rd():
            try:
                box["v"] = self.proc.stdout.readline()
            except Exception as e:
                box["e"] = e

        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None
        return (box.get("v") or "").rstrip("\r\n")

    def read(self, bmp_path):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.log("[OCR] PowerShell 프로세스가 종료됨 → 재시작")
                self._start()
            try:
                self.proc.stdin.write(bmp_path + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                raise RuntimeError(f"요청 전송 실패: {e}")

            out = []
            while True:
                line = self._readline(timeout=15)
                if line is None:
                    self.log("[OCR] 응답 시간 초과 → 프로세스 재시작")
                    self.close()
                    return ""
                if line == "###EOF###":
                    break
                if line.startswith("###ERR###"):
                    self.log(f"[OCR] {line[9:]}")
                    return ""
                out.append(line)
            return " ".join(out).strip()

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.write("###QUIT###\n")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


class _WinSdkOcr:
    """Windows.Media.Ocr (winsdk / winrt 패키지 필요)."""

    def __init__(self, lang="ko"):
        mods = None
        try:
            from winsdk.windows.media.ocr import OcrEngine
            from winsdk.windows.globalization import Language
            from winsdk.windows.graphics.imaging import BitmapDecoder
            from winsdk.windows.storage import StorageFile, FileAccessMode
            mods = (OcrEngine, Language, BitmapDecoder, StorageFile, FileAccessMode)
        except ImportError:
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.globalization import Language
            from winrt.windows.graphics.imaging import BitmapDecoder
            from winrt.windows.storage import StorageFile, FileAccessMode
            mods = (OcrEngine, Language, BitmapDecoder, StorageFile, FileAccessMode)

        (self.OcrEngine, self.Language, self.BitmapDecoder,
         self.StorageFile, self.FileAccessMode) = mods

        engine = self.OcrEngine.try_create_from_language(self.Language(lang))
        if engine is None:
            engine = self.OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("사용 가능한 OCR 언어팩 없음")
        self.engine = engine

    def read(self, bmp_path):
        import asyncio

        async def go():
            f = await self.StorageFile.get_file_from_path_async(bmp_path)
            stream = await f.open_async(self.FileAccessMode.READ)
            decoder = await self.BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            result = await self.engine.recognize_async(bitmap)
            return result.text or ""

        try:
            return asyncio.run(go())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(go())
            finally:
                loop.close()


def _find_tesseract():
    for p in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        r"C:\rpa\Tesseract-OCR\tesseract.exe",
    ):
        if os.path.exists(p):
            return p
    return ""


def _tesseract_read(exe, bmp_path):
    import subprocess
    out = bmp_path + "_out"
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.run(
        [exe, bmp_path, out, "-l", "kor+eng", "--psm", "7"],
        timeout=15, startupinfo=si,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    txt = out + ".txt"
    if os.path.exists(txt):
        with open(txt, encoding="utf-8", errors="ignore") as f:
            data = f.read()
        try:
            os.remove(txt)
        except Exception:
            pass
        return data
    return ""


# ════════════════════════════════════════════════
# 번호판 텍스트 정리
# ════════════════════════════════════════════════
# 숫자 자리에서만 적용하는 오인식 보정
DIGIT_FIX = {
    "O": "0", "o": "0", "D": "0", "Q": "0", "()": "0",
    "I": "1", "l": "1", "|": "1", "i": "1", "!": "1",
    "Z": "2", "z": "2",
    "E": "3",
    "A": "4", "h": "4",
    "S": "5", "s": "5",
    "G": "6", "b": "6",
    "T": "7", "?": "7",
    "B": "8",
    "g": "9", "q": "9",
}

REGIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
           "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주")

PLATE_RE = re.compile(r"(\d{2,3})\s*([가-힣])\s*(\d{4})")
# 한글 자리가 깨졌을 때 (예: '가' → '7十') 숫자 뼈대만이라도 살리는 패턴
LOOSE_RE = re.compile(r"(\d{2,4})([^\d]{1,4})(\d{4})")
# 한글이 아예 안 읽혀 공백만 남은 경우 : '271 3971'
GAP_RE = re.compile(r"(?<!\d)(\d{2,3})\s+(\d{4})(?!\d)")

# 번호판에 실제로 쓰이는 한글 (용도 기호)
PLATE_HANGUL = (
    "가나다라마"
    "거너더러머버서어저"
    "고노도로모보소오조"
    "구누두루무부수우주"
    "바사아자"
    "배"
    "하허호"
)


def _digitize(s):
    return "".join(DIGIT_FIX.get(ch, ch) for ch in s)


def parse_plate(raw):
    """
    OCR 원문에서 번호판 후보를 뽑는다.
    반환: (번호, 원문, 확신)
      확신 True  : 한글 한 글자까지 깔끔하게 잡힘
      확신 False : 한글 자리를 '?' 로 남김 → 사용자가 골라야 함
    """
    if not raw:
        return "", "", False

    text = raw.replace("\r", " ").replace("\n", " ")
    flat = re.sub(r"[\s\-·.,_]", "", text)

    m = PLATE_RE.search(flat)
    if not m:
        fixed = _digitize(flat)
        m = PLATE_RE.search(fixed)
        if m:
            flat = fixed

    # 한글이 통째로 누락된 경우 (예: '271 3971')
    # 공백 자체가 한글이 있던 자리라는 단서다. 지우기 전에 먼저 본다.
    if not m:
        gm = GAP_RE.search(text)
        if gm:
            head, tail = gm.group(1), gm.group(2)
            return _add_region(re.sub(r"\s", "", text[:gm.start()]),
                               len(re.sub(r"\s", "", text[:gm.start()])),
                               f"{head}?{tail}"), text.strip(), False

    if m:
        head, kor, tail = m.group(1), m.group(2), m.group(3)
        plate = f"{head}{kor}{tail}"
        confident = kor in PLATE_HANGUL
        return _add_region(flat, m.start(), plate), text.strip(), confident

    # 한글이 깨진 경우 : 숫자 뼈대만 살리고 한글 자리는 '?'
    lm = LOOSE_RE.search(flat)
    if lm:
        head, tail = lm.group(1), lm.group(3)
        if len(head) > 3:          # ㄱ이 7로 읽히는 등 앞자리에 숫자가 붙은 경우
            head = head[:3]
        return _add_region(flat, lm.start(), f"{head}?{tail}"), text.strip(), False

    return "", text.strip(), False


def _add_region(flat, pos, plate):
    """지역명이 앞에 붙은 구형 번호판 처리."""
    prefix = flat[:pos]
    for r in REGIONS:
        if prefix.endswith(r):
            return r + plate
    return plate


# 눈으로 한 번 더 봐야 하는 글자
AMBIGUOUS = set("03568OB")


def ambiguous_positions(plate):
    return [i for i, ch in enumerate(plate)
            if ch in AMBIGUOUS or ("가" <= ch <= "힣")]


# ════════════════════════════════════════════════
# 드래그 오버레이 + 결과 팝업
# ════════════════════════════════════════════════
class PlateOCR:
    """
    parking_macro.py 에 붙이는 OCR 기능.

        ocr = PlateOCR(root, CONFIG, log=log, status=set_status)
        keyboard.add_hotkey("`", ocr.trigger, suppress=True)
    """

    def __init__(self, root, config, log=print, status=None):
        self.root = root
        self.cfg = config or {}
        self.log = log
        self.status = status or (lambda m: None)
        self.busy = False
        self.prev_hwnd = None
        self.overlay = None
        self.popup = None
        self._img_ref = None
        self.backend = None
        self.tmpdir = tempfile.mkdtemp(prefix="plate_ocr_")
        self.base_dir = os.path.dirname(os.path.abspath(sys.argv[0])) or os.getcwd()

    # ── 엔진 지연 로딩 (첫 사용 때 한 번만) ──
    def _ensure_backend(self):
        if self.backend is None:
            self.backend = OcrBackend(
                lang=self.cfg.get("ocr_lang", "ko"),
                tesseract_path=self.cfg.get("tesseract_path", ""),
                log=self.log,
            )
        return self.backend

    def warmup(self):
        """시작할 때 백그라운드로 엔진을 미리 올려 첫 인식 지연을 없앤다."""
        threading.Thread(target=self._ensure_backend, daemon=True).start()

    # ── 단축키 진입점 (keyboard 스레드에서 호출됨) ──
    def trigger(self):
        # 이전 실행이 중간에 끊겨 busy 가 True 로 굳으면 영영 안 열린다.
        # 오버레이도 팝업도 없는데 busy 면 비정상이므로 풀어 준다.
        if self.busy and self.overlay is None and self.popup is None:
            self.log("[OCR] 이전 상태가 남아 있어 초기화합니다")
            self.busy = False
        if self.busy:
            return
        self.busy = True
        self.prev_hwnd = user32.GetForegroundWindow()
        self.log(f"[OCR] trigger (busy={self.busy})")
        self.root.after(0, self._show_overlay)

    # ── 1단계 : 드래그 오버레이 ──
    def _show_overlay(self):
        if self.overlay is not None:
            return

        vx, vy, vw, vh = virtual_screen_rect()

        ov = tk.Toplevel(self.root)
        self.overlay = ov
        ov.overrideredirect(True)
        ov.geometry(f"{vw}x{vh}+{vx}+{vy}")
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", float(self.cfg.get("ocr_overlay_alpha", 0.30)))
        ov.configure(bg="black", cursor="crosshair")

        cv = tk.Canvas(ov, bg="black", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        cv.create_text(vw // 2, 40, fill="white", font=("맑은 고딕", 14),
                       text="번호판 영역을 드래그하세요   (Esc = 취소)")

        state = {"x": 0, "y": 0, "rect": None}

        def on_press(e):
            state["x"], state["y"] = e.x_root, e.y_root
            if state["rect"]:
                cv.delete(state["rect"])
            state["rect"] = cv.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#00ff88", width=2)
            state["cx"], state["cy"] = e.x, e.y

        def on_drag(e):
            if state["rect"]:
                cv.coords(state["rect"], state["cx"], state["cy"], e.x, e.y)

        def on_release(e):
            x1, y1 = state["x"], state["y"]
            x2, y2 = e.x_root, e.y_root
            close()
            x, y = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)
            if w < 8 or h < 6:
                self.status("영역이 너무 작습니다")
                self.busy = False
                self._restore_focus()
                return

            # 오버레이 창이 화면에서 실제로 지워질 때까지 기다린다.
            # 너무 빨리 찍으면 어두운 오버레이가 그대로 캡처된다.
            try:
                self.root.update_idletasks()
                self.root.update()
            except Exception:
                pass
            delay = int(float(self.cfg.get("ocr_capture_delay", 0.18)) * 1000)
            self.root.after(delay, lambda: self._capture_and_read(x, y, w, h))

        def close(_=None):
            if self.overlay is not None:
                try:
                    self.overlay.destroy()
                except Exception:
                    pass
                self.overlay = None

        def cancel(_=None):
            close()
            self.busy = False
            self._restore_focus()

        ov.bind("<ButtonPress-1>", on_press)
        ov.bind("<B1-Motion>", on_drag)
        ov.bind("<ButtonRelease-1>", on_release)
        ov.bind("<Escape>", cancel)
        ov.bind("<Button-3>", cancel)
        ov.focus_force()

    # ── 2단계 : 캡처 + 인식 (배율/전처리를 바꿔가며 시도) ──
    def _capture_and_read(self, x, y, w, h):
        t0 = time.time()

        # OCR 엔진은 글자가 무조건 클수록 좋지 않다. 잘 맞는 구간이 있다.
        # 드래그한 높이를 기준으로 목표 높이 몇 개를 잡아 배율을 만든다.
        targets = self.cfg.get("ocr_target_heights", [64, 100, 150, 40])
        scales = []
        for th in targets:
            sc = round(max(0.5, min(8.0, float(th) / max(1, h))), 2)
            if sc not in scales:
                scales.append(sc)
        self.log(f"[OCR] 영역 {w}x{h} → 배율 {scales}")

        # 미리보기용 캡처 (가장 첫 배율)
        try:
            sw0, sh0, bgra0 = grab_region(x, y, w, h, scales[0])
        except Exception as e:
            self.log(f"[OCR] 캡처 실패: {e}")
            self.status(f"캡처 실패: {e}")
            self.busy = False
            self._restore_focus()
            return

        png = os.path.join(self.tmpdir, "cap.png")
        try:
            save_png(png, sw0, sh0, bgra0)
        except Exception as e:
            self.log(f"[OCR] 미리보기 저장 실패: {e}")

        if self.cfg.get("ocr_keep_capture", True):
            try:
                keep = os.path.join(self.base_dir, "마지막캡처.png")
                save_png(keep, sw0, sh0, bgra0)
            except Exception as e:
                self.log(f"[OCR] 캡처 사본 저장 실패: {e}")

        def work():
            engine = self._ensure_backend()
            best = ("", "", False)
            tried = 0

            def attempt(tag, sw, sh, data):
                nonlocal best, tried
                tried += 1
                bmp = os.path.join(self.tmpdir, f"p{tried}.bmp")
                save_bmp(bmp, sw, sh, data)
                raw = engine.read(bmp)
                plate, text, conf = parse_plate(raw)
                self.log(f"[OCR] {tag}: '{raw}' → '{plate}' (확신={conf})")
                if conf:
                    best = (plate, text, True)
                    return True
                if plate and not best[0]:
                    best = (plate, text, False)
                elif text and not best[1]:
                    best = (best[0], text, False)
                return False

            try:
                for i, sc in enumerate(scales):
                    if i == 0:
                        sw, sh, data = sw0, sh0, bgra0
                    else:
                        sw, sh, data = grab_region(x, y, w, h, sc)

                    if attempt(f"x{sc} 원본", sw, sh, data):
                        break
                    if attempt(f"x{sc} 대비", sw, sh, variant_contrast(data)):
                        break
                    # 반전은 초록 구형판·어두운 배경용. 첫 배율에서만 시도.
                    if i == 0 and attempt(f"x{sc} 반전", sw, sh,
                                          variant_contrast(data, invert=True)):
                        break
                # 배율·전처리로 안 되면 기울기를 의심한다.
                # 비스듬히 찍힌 번호판은 몇 도만 돌려도 결과가 달라진다.
                if not best[2]:
                    angles = self.cfg.get("ocr_angles", [-7, 7, -14, 14])
                    sc = scales[0]
                    for ang in angles:
                        try:
                            rw, rh, rdata = grab_region_rotated(x, y, w, h, sc, ang)
                        except Exception as e:
                            self.log(f"[OCR] 회전 {ang}도 실패: {e}")
                            break
                        if attempt(f"{ang:+d}도", rw, rh, rdata):
                            break
                        if attempt(f"{ang:+d}도 대비", rw, rh,
                                   variant_contrast(rdata)):
                            break
            except Exception as e:
                self.log(f"[OCR] 인식 중 오류: {e}")

            ms = int((time.time() - t0) * 1000)
            self.log(f"[OCR] {tried}회 시도 · {ms}ms")
            self.root.after(0, lambda: self._show_result(best[0], best[1], png, ms, best[2]))

        threading.Thread(target=work, daemon=True).start()
        self.status("인식 중…")

    # ── 3단계 : 결과 팝업 (자동 입력 안 함) ──
    def _show_result(self, plate, raw, png_path, ms, confident=False):
        value = plate or ""
        if value and "?" not in value and pyperclip:
            try:
                pyperclip.copy(value)
            except Exception as e:
                self.log(f"[OCR] 클립보드 복사 실패: {e}")

        self.log(f"[OCR] {ms}ms · 결과='{value}' · 확신={confident} · 원문='{raw}'")

        if self.popup is not None:
            try:
                self.popup.destroy()
            except Exception:
                pass

        pw = tk.Toplevel(self.root)
        self.popup = pw
        pw.title("차량번호 확인")
        pw.attributes("-topmost", True)
        pw.resizable(False, False)
        try:
            px, py = pw.winfo_pointerxy()
            pw.geometry(f"+{max(0, px - 220)}+{max(0, py + 24)}")
        except Exception:
            pass

        body = tk.Frame(pw, padx=10, pady=8)
        body.pack()

        # 확대된 원본 이미지 — 사람이 대조할 근거
        try:
            img = tk.PhotoImage(file=png_path)
            if img.width() > 620:
                img = img.subsample(max(2, img.width() // 560))
            tk.Label(body, image=img, bd=1, relief="solid").pack(pady=(0, 6))
            self._img_ref = img
        except Exception as e:
            self.log(f"[OCR] 미리보기 실패: {e}")

        var = tk.StringVar(value=value)
        ent = tk.Entry(body, textvariable=var, font=("Consolas", 24),
                       justify="center", width=14)
        ent.pack()

        msg = tk.StringVar()
        tk.Label(body, textvariable=msg, font=("맑은 고딕", 9), fg="#0a6").pack(pady=(3, 0))

        def refresh_msg():
            v = var.get()
            if not v:
                msg.set("번호를 못 찾았습니다 — 이미지를 보고 직접 입력하세요")
            elif "?" in v:
                msg.set("한글이 깨졌습니다 — 아래에서 고르세요 (숫자 자릿수도 확인)")
            else:
                amb = [v[i] for i in ambiguous_positions(v)]
                tip = ("  확인: " + " ".join(amb)) if amb else ""
                msg.set("클립보드 복사됨 — Ctrl+V" + tip)

        refresh_msg()

        if raw and raw != value:
            tk.Label(body, text=f"원문: {raw[:60]}", fg="#888",
                     font=("맑은 고딕", 8)).pack()

        # ── 한글 선택판 (IME 전환 없이 클릭 한 번으로) ──
        def set_hangul(ch):
            v = var.get()
            i = v.find("?")
            if i < 0:
                for j, c in enumerate(v):
                    if "가" <= c <= "힣":
                        i = j
                        break
            if i < 0:
                return
            v = v[:i] + ch + v[i + 1:]
            var.set(v)
            if pyperclip:
                try:
                    pyperclip.copy(v)
                except Exception:
                    pass
            refresh_msg()

        picker = tk.LabelFrame(body, text="한글 선택", padx=4, pady=3)
        picker.pack(pady=(6, 0))
        for idx, ch in enumerate(PLATE_HANGUL):
            tk.Button(picker, text=ch, width=2, font=("맑은 고딕", 9),
                      command=lambda c=ch: set_hangul(c))\
                .grid(row=idx // 10, column=idx % 10, padx=1, pady=1)

        def recopy(_=None):
            v = var.get().strip()
            if "?" in v:
                msg.set("한글을 먼저 고르세요")
                return
            if v and pyperclip:
                pyperclip.copy(v)
            close()

        def close(_=None):
            if self.popup is not None:
                try:
                    self.popup.destroy()
                except Exception:
                    pass
                self.popup = None
            self.busy = False
            self._restore_focus()

        btns = tk.Frame(body)
        btns.pack(pady=(6, 0))
        tk.Button(btns, text="복사 후 닫기 (Enter)", width=20, command=recopy)\
            .grid(row=0, column=0, padx=3)
        tk.Button(btns, text="다시", width=8,
                  command=lambda: (close(), self.root.after(80, self._show_overlay)))\
            .grid(row=0, column=1, padx=3)

        pw.bind("<Return>", recopy)
        pw.bind("<Escape>", close)
        pw.protocol("WM_DELETE_WINDOW", close)
        var.trace_add("write", lambda *a: refresh_msg())

        ent.focus_set()
        ent.select_range(0, "end")

        sec = float(self.cfg.get("ocr_popup_seconds", 0))
        if sec > 0:
            pw.after(int(sec * 1000), close)

        self.status(f"OCR {ms}ms · {value or '인식 실패'}")

    def _restore_focus(self):
        """캡처 전에 쓰던 창으로 포커스를 돌려준다 → 바로 Ctrl+V 가능."""
        if self.prev_hwnd:
            try:
                user32.SetForegroundWindow(self.prev_hwnd)
            except Exception:
                pass