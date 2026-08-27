# -*- coding: utf-8 -*-
"""
ocr_test.py — 윈도우 내장 OCR 이 PowerShell 경유로 동작하는지 확인

실행:
    & "C:\\Users\\user\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe" C:\\rpa\\ocr_test.py

하는 일
  1) PowerShell 5.1 을 찾는다
  2) WinRT OCR 엔진을 만들 수 있는지 확인
  3) 사용 가능한 인식 언어 목록 출력
  4) 글자를 그린 테스트 이미지를 만들어 실제로 읽어 본다
"""

import base64
import os
import subprocess
import sys
import tempfile

PS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                  r"System32\WindowsPowerShell\v1.0\powershell.exe")

CHECK = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] | Out-Null

Write-Output ("PSVersion=" + $PSVersionTable.PSVersion.ToString())

$langs = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
Write-Output ("LangCount=" + $langs.Count)
foreach ($l in $langs) { Write-Output ("  LANG " + $l.LanguageTag + "  " + $l.DisplayName) }

$ko = $null
try { $ko = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('ko')) } catch {}
if ($ko) { Write-Output "Korean=OK" } else { Write-Output "Korean=NO" }

$def = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($def) { Write-Output ("Default=" + $def.RecognizerLanguage.LanguageTag) } else { Write-Output "Default=NONE" }
"""

READ = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

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
try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('ko')) } catch {}
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { Write-Output '###ERR###엔진 없음'; exit 1 }

$path = '__PATH__'
$file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp     = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$res     = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
Write-Output ("TEXT=" + ($res.Text -replace "`r`n", ' '))
"""


def run_ps(script, timeout=60):
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    p = subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        capture_output=True, timeout=timeout, startupinfo=si,
    )
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


def make_test_image(path):
    """GDI 로 '12가3456' 을 그려서 BMP 로 저장 (외부 라이브러리 없이)."""
    import ctypes
    import ctypes.wintypes as wt
    import struct

    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    # 64비트에서 핸들은 c_int 범위를 넘을 수 있으므로 모두 c_void_p 로 지정한다
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.GetStockObject.restype = ctypes.c_void_p
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.CreateFontW.restype = ctypes.c_void_p
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [ctypes.c_ulong] * 8 + [ctypes.c_wchar_p]
    gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    gdi32.TextOutW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_wchar_p, ctypes.c_int]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint]

    w, h = 600, 140
    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    hbmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, hbmp)

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    rc = RECT(0, 0, w, h)
    gdi32.SetBkMode(mdc, 1)                          # TRANSPARENT
    user32.FillRect(mdc, ctypes.byref(rc), gdi32.GetStockObject(0))  # WHITE_BRUSH

    font = gdi32.CreateFontW(90, 0, 0, 0, 700, 0, 0, 0, 129, 0, 0, 4, 0, "맑은 고딕")
    gdi32.SelectObject(mdc, font)
    gdi32.SetTextColor(mdc, 0x000000)
    text = "12가3456"
    gdi32.TextOutW(mdc, 30, 20, text, len(text))

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class BI(ctypes.Structure):
        _fields_ = [("h", BIH), ("c", wt.DWORD * 3)]

    bmi = BI()
    bmi.h.biSize = ctypes.sizeof(BIH)
    bmi.h.biWidth = w
    bmi.h.biHeight = h
    bmi.h.biPlanes = 1
    bmi.h.biBitCount = 32
    bmi.h.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)

    data = buf.raw
    with open(path, "wb") as f:
        f.write(struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0,
                            len(data), 2835, 2835, 0, 0))
        f.write(data)

    gdi32.DeleteObject(font)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, hdc)


def main():
    print("=" * 55)
    print("1) PowerShell 확인:", PS)
    if not os.path.exists(PS):
        print("   [실패] powershell.exe 를 찾을 수 없습니다")
        return
    print("   [OK] 존재함")

    print("\n2) OCR 엔진 / 언어 확인")
    rc, out, err = run_ps(CHECK)
    print(out.strip() or "(출력 없음)")
    if err.strip():
        print("   [stderr]", err.strip()[:800])
    if rc != 0:
        print(f"   [실패] 종료코드 {rc}")
        return

    print("\n3) 실제 인식 테스트  (기대값: 12가3456)")
    tmp = os.path.join(tempfile.gettempdir(), "ocr_selftest.bmp")
    try:
        make_test_image(tmp)
        print("   테스트 이미지:", tmp)
    except Exception as e:
        print("   [실패] 이미지 생성:", e)
        return

    rc, out, err = run_ps(READ.replace("__PATH__", tmp.replace("'", "''")))
    print("   결과:", out.strip() or "(없음)")
    if err.strip():
        print("   [stderr]", err.strip()[:800])

    print("\n" + "=" * 55)
    print("위 내용을 그대로 복사해서 알려주세요.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
    input("\n엔터를 누르면 종료합니다...")