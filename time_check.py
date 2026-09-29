# -*- coding: utf-8 -*-
"""
time_check.py — 위반자료 상세관리 화면 캡처들을 읽어 '촬영간격 부족' 건을 엑셀로 뽑는다.

사용
  pip install openpyxl pillow
  python time_check.py                     ← 저장 폴더에서 가장 최근 캡처 폴더를 자동 선택
  python time_check.py "D:\\...\\시간확인\\20260929_101500"
  python time_check.py 폴더 --engine tesseract     (윈도우 OCR 이 안 될 때)

판정 규칙
  · 비고가 '기타5분'  → 1번째~2번째 사진 촬영간격이 300초(5분) 이상이어야 정상
  · 비고가 그 외      → 60초(1분) 이상이어야 정상
  · 비고가 비어 있음  → '비고없음' (판정 안 함. 제외건 등)
  · 3장 이상이어도 1번째와 2번째 사진 간격만 본다

간격은 두 군데서 따로 구해 서로 맞춰 본다.
  ① 민원내용의 (1/N) (2/N) 촬영시각 → 초 단위로 직접 계산
  ② 화면의 '촬영간격 : 1분' 표시   → 분 단위로 잘려서 나옴 (5분42초 → '5분')
     기준(60초, 300초)이 모두 분 단위라서 ②만으로도 정상/부족은 정확히 가려진다.
둘이 서로 다른 결론이면 '확인필요' 로 남긴다. 못 읽은 것을 정상으로 넘기지 않는다.
"""

import argparse
import difflib
import glob
import io
import os
import re
import subprocess
import tempfile
import sys
import time
from datetime import datetime

try:
    from PIL import Image, ImageOps
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    print("[설치 필요] pip install openpyxl pillow")
    sys.exit(1)

# ────────────────────────────────────────────────
# 설정
# ────────────────────────────────────────────────
DEFAULT_BASE = r"D:\양산\바탕 화면\엄성문\유용한기능\시간간격 검사\시간확인"

NORMAL_SEC = 60          # 일반 비고 기준 (이상이면 정상)
ETC5_SEC = 300           # 기타5분 기준
KNOWN_REMARKS = ["인도", "소방시설", "어린이보호구역", "교차로", "횡단보도", "버스정류장", "기타5분"]

# 기준 화면(1442x1006) 에서의 읽을 영역 (x1, y1, x2, y2). 창 크기가 다르면 비율로 보정.
BASE_W, BASE_H = 1442, 1006
REGIONS = {
    "complaint_no": (105, 95, 285, 125),      # 민원번호
    "plate":        (108, 281, 240, 307),     # 차량번호 (참고용)
    "remark":       (105, 445, 605, 472),     # 비고
    "interval":     (915, 490, 1045, 516),    # 촬영간격 : N분
    "body":         (630, 100, 1420, 425),    # 민원내용 (촬영시각 줄이 여기 있음)
    "index":        (90, 965, 125, 997),      # 화면 왼쪽 아래 현재 번호
}
SCALE_UP = {"body": 2, "index": 4}            # 그 외는 3배


# ────────────────────────────────────────────────
# OCR 엔진
# ────────────────────────────────────────────────
class TesseractEngine:
    name = "tesseract"

    def __init__(self):
        exe = None
        for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                  r"C:\rpa\Tesseract-OCR\tesseract.exe"):
            if os.path.exists(p):
                exe = p
                break
        self.exe = exe or "tesseract"
        subprocess.run([self.exe, "--version"], capture_output=True, check=True)

    def read(self, img, kind):
        buf = io.BytesIO()
        img.save(buf, "PNG")
        psm = "6" if kind == "body" else "7"
        cmd = [self.exe, "stdin", "stdout", "-l", "kor+eng", "--psm", psm]
        if kind == "index":
            cmd += ["-c", "tessedit_char_whitelist=0123456789"]
        r = subprocess.run(cmd, input=buf.getvalue(), capture_output=True)
        return r.stdout.decode("utf-8", "replace")

    def close(self):
        pass


# 윈도우 내장 OCR 을 PowerShell 5.1 로 상주시켜 쓴다. (plate_ocr.py 와 같은 방식)
# 단, 정부 PC 보안 프로그램이 임시 이미지 파일 쓰기를 막아서
# 이미지를 파일로 저장하지 않고 base64 로 표준입력에 넘겨 메모리에서 바로 읽힌다.
_PS_MEM_WORKER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
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
if ($null -eq $engine) { Write-Output '###FAIL###OCR 언어팩 없음'; exit 1 }
Write-Output ('###READY###' + $engine.RecognizerLanguage.LanguageTag)

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line -or $line -eq '###QUIT###') { break }
    try {
        $bytes   = [Convert]::FromBase64String($line)
        $ms      = New-Object System.IO.MemoryStream(,$bytes)
        $ras     = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bmp     = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $res     = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
        foreach ($ln in $res.Lines) { Write-Output $ln.Text }
        $bmp.Dispose()
        $ms.Dispose()
    } catch {
        Write-Output ('###ERR###' + $_.Exception.Message)
    }
    Write-Output '###EOF###'
}
"""


class WindowsEngine:
    """윈도우 내장 OCR (임시 파일 없이 메모리로 전달)."""
    name = "windows"

    def __init__(self, folder=None):
        import base64
        import threading
        self._b64 = base64.b64encode
        self._threading = threading
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          r"System32\WindowsPowerShell\v1.0\powershell.exe")
        enc = base64.b64encode(_PS_MEM_WORKER.encode("utf-16-le")).decode()
        self.proc = subprocess.Popen(
            [ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            startupinfo=self._si(),
        )
        line = self._readline(30)
        if not line or not line.startswith("###READY###"):
            self.close()
            raise RuntimeError(f"엔진 준비 실패: {line!r}")
        self.first_err = True

    @staticmethod
    def _si():
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return si

    def _readline(self, timeout):
        box = {}

        def rd():
            box["v"] = self.proc.stdout.readline()

        t = self._threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None
        return (box.get("v") or "").rstrip("\r\n")

    def read(self, img, kind):
        buf = io.BytesIO()
        img.save(buf, "PNG")
        self.proc.stdin.write(self._b64(buf.getvalue()).decode() + "\n")
        self.proc.stdin.flush()
        out = []
        while True:
            line = self._readline(20)
            if line is None:
                raise RuntimeError("OCR 응답 시간 초과")
            if line == "###EOF###":
                break
            if line.startswith("###ERR###"):
                raise RuntimeError("윈도우 OCR 오류: " + line[9:])
            out.append(line)
        return "\n".join(out)

    def close(self):
        if getattr(self, "proc", None) is None:
            return
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


class PlateOcrEngine:
    """기존 차량번호 매크로(plate_ocr.py)와 똑같은 방식: 임시 BMP 를 매번 새 이름으로 저장해 읽힌다.
    같은 이름으로 덮어쓰면 OCR 이 앞 파일을 아직 잡고 있어 쓰기가 거부된다."""
    name = "windows(plate_ocr)"

    def __init__(self, folder=None):
        import plate_ocr
        self.po = plate_ocr
        self.be = plate_ocr.OcrBackend(lang="ko", log=lambda *a: None)
        if self.be.kind is None:
            raise RuntimeError("윈도우 OCR 엔진 없음")
        self.name = f"windows({self.be.kind})"
        self.tmp = tempfile.mkdtemp(prefix="timecheck_")
        self.n = 0
        self.read(Image.new("RGB", (60, 30), "white"), "test")   # 시험 한 번

    def read(self, img, kind):
        self.n += 1
        path = os.path.join(self.tmp, f"c{self.n}.bmp")
        rgba = img.convert("RGBA")
        b, g, r, a = rgba.split()
        bgra = Image.merge("RGBA", (b, g, r, a)).transpose(Image.FLIP_TOP_BOTTOM).tobytes()
        self.po.save_bmp(path, img.width, img.height, bgra)
        text = self.be.read(path)
        if self.n > 3:                                   # 오래된 파일은 조용히 정리
            try:
                os.remove(os.path.join(self.tmp, f"c{self.n - 3}.bmp"))
            except OSError:
                pass
        return text

    def close(self):
        try:
            self.be.close()
        except Exception:
            pass
        for f in glob.glob(os.path.join(self.tmp, "*.bmp")):
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass


class WinRtEngine:
    """파이썬 winrt/winsdk 모듈로 윈도우 내장 OCR 을 직접 호출 (파일도 PowerShell 도 쓰지 않음).
    이 PC 는 PowerShell 실행과 임시 이미지 저장이 막혀 있어 이 방식을 1순위로 쓴다."""
    name = "windows(winrt)"

    def __init__(self, folder=None):
        m = None
        for pkg in ("winsdk", "winrt"):
            try:
                ocr = __import__(pkg + ".windows.media.ocr", fromlist=["x"])
                glob_ = __import__(pkg + ".windows.globalization", fromlist=["x"])
                img = __import__(pkg + ".windows.graphics.imaging", fromlist=["x"])
                m = (pkg, ocr, glob_, img)
                break
            except ImportError:
                continue
        if m is None:
            raise RuntimeError("winrt/winsdk 모듈 없음")
        self.pkg, ocr, glob_, self.img = m
        eng = ocr.OcrEngine.try_create_from_language(glob_.Language("ko"))
        if eng is None:
            eng = ocr.OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError("OCR 언어팩 없음")
        self.engine = eng
        self._make_buffer = self._buffer_maker()
        # 시험 한 번 (여기서 실패하면 다음 방식으로 넘어간다)
        self.read(Image.new("RGB", (60, 30), "white"), "test")

    def _buffer_maker(self):
        """바이트열 → IBuffer. 설치된 모듈에 따라 되는 방법이 달라서 차례로 시도."""
        makers = []
        try:
            st = __import__(self.pkg + ".windows.storage.streams", fromlist=["x"])

            def by_writer(data):
                w = st.DataWriter()
                try:
                    w.write_bytes(data)
                except TypeError:
                    w.write_bytes(list(data))
                return w.detach_buffer()
            makers.append(by_writer)
        except ImportError:
            pass
        try:
            cr = __import__(self.pkg + ".windows.security.cryptography", fromlist=["x"])

            def by_crypto(data):
                try:
                    return cr.CryptographicBuffer.create_from_byte_array(data)
                except TypeError:
                    return cr.CryptographicBuffer.create_from_byte_array(list(data))
            makers.append(by_crypto)
        except ImportError:
            pass
        if not makers:
            raise RuntimeError("winrt Streams/Cryptography 모듈 없음")

        def make(data):
            last = None
            for f in makers:
                try:
                    return f(data)
                except Exception as e:
                    last = e
            raise last
        return make

    def read(self, img, kind):
        import asyncio
        rgba = img.convert("RGBA")
        b, g, r, a = rgba.split()
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()      # BGRA 순서
        buf = self._make_buffer(data)
        bmp = self.img.SoftwareBitmap.create_copy_from_buffer(
            buf, self.img.BitmapPixelFormat.BGRA8, img.width, img.height,
            self.img.BitmapAlphaMode.PREMULTIPLIED)

        async def go():
            res = await self.engine.recognize_async(bmp)
            return "\n".join(ln.text for ln in res.lines)

        try:
            return asyncio.run(go())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(go())
            finally:
                loop.close()

    def close(self):
        pass


def make_engine(choice, folder=None):
    if choice in ("auto", "windows") and sys.platform == "win32":
        for cls in (PlateOcrEngine, WinRtEngine, WindowsEngine):
            try:
                return cls(folder)
            except Exception as e:
                print(f"[안내] {cls.__name__} 사용 불가 ({type(e).__name__}: {e})")
    try:
        return TesseractEngine()
    except Exception as e:
        print(f"[오류] 사용할 수 있는 OCR 엔진이 없습니다: {e}")
        sys.exit(1)


# ────────────────────────────────────────────────
# 이미지 → 영역별 글자
# ────────────────────────────────────────────────
def crop_region(img, key):
    sx, sy = img.width / BASE_W, img.height / BASE_H
    x1, y1, x2, y2 = REGIONS[key]
    c = img.crop((int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)))
    k = SCALE_UP.get(key, 3)
    c = c.resize((c.width * k, c.height * k), Image.LANCZOS)
    bg = c.getpixel((2, 2))
    return ImageOps.expand(c, border=20, fill=bg)      # 글자에 붙지 않게 여백


def is_blank(img):
    """글자(어두운 픽셀)가 거의 없으면 빈 칸."""
    g = img.convert("L")
    return sum(g.histogram()[:128]) < 20


def read_screen(engine, path):
    img = Image.open(path).convert("RGB")
    raw = {}
    for k in REGIONS:
        c = crop_region(img, k)
        raw[k] = "" if k == "remark" and is_blank(c) else engine.read(c, k).strip()
    return raw, (img.width, img.height)


# ────────────────────────────────────────────────
# 글자 → 값
# ────────────────────────────────────────────────
TS_RE = re.compile(r"(\d{4})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[T ]?\s*"
                   r"(\d{1,2})\s*[:;]\s*(\d{2})\s*[:;]\s*(\d{2})")


def _to_dt(m):
    try:
        return datetime(*[int(g) for g in m.groups()])
    except ValueError:
        return None


def parse_stamps(text):
    """(1/N), (2/N) 뒤의 촬영시각 두 개. 표식이 깨졌으면 앞에서 두 개."""
    found = {}
    for k in (1, 2):
        mk = re.search(r"\(\s*%d\s*/\s*\d+\s*\)" % k, text)
        if mk:
            m = TS_RE.search(text, mk.end(), mk.end() + 60)
            if m and _to_dt(m):
                found[k] = _to_dt(m)
    if 1 in found and 2 in found:
        return found[1], found[2]
    dts = [d for d in (_to_dt(m) for m in TS_RE.finditer(text)) if d]
    if len(dts) >= 2:
        return dts[0], dts[1]
    return (dts[0] if dts else None), None


def parse_display(text):
    """'촬영간격 : 1분' / '30초' / '1분 30초' → (하한초, 상한초). 못 읽으면 None."""
    t = text.replace(" ", "")
    mn = re.search(r"(\d+)분", t)
    sc = re.search(r"(\d+)초", t)
    if not mn and not sc:
        return None
    lo = (int(mn.group(1)) * 60 if mn else 0) + (int(sc.group(1)) if sc else 0)
    hi = lo + 59 if (mn and not sc) else lo       # '5분' 은 5분00~59초
    return lo, hi


def match_remark(text):
    """(정규화된 비고, 상태)  상태: ok / empty / unknown"""
    n = re.sub(r"[^가-힣0-9]", "", text)
    if not n:
        return "", "empty"
    if "기타" in n or (n.endswith("5분") and len(n) <= 3):
        return "기타5분", "ok"
    for r in KNOWN_REMARKS:
        if r in n:
            return r, "ok"
    for r in KNOWN_REMARKS:
        if len(n) >= 2 and n in r:
            return r, "ok"
    c = difflib.get_close_matches(n, KNOWN_REMARKS, n=1, cutoff=0.6)
    if c:
        return c[0], "ok"
    return n, "unknown"


def parse_complaint_no(text):
    t = re.sub(r"\s+", "", text)
    m = re.search(r"(\d{4})\D{0,2}(\d{7})", t)
    if not m:
        return t
    return f"{m.group(1)}-{m.group(2)}"          # 접두어(2AA 등)는 OCR 이 흔들려서 뺀다


def fmt_sec(s):
    if s is None:
        return ""
    m, r = divmod(int(s), 60)
    return f"{m}분{r:02d}초" if m else f"{r}초"


# ────────────────────────────────────────────────
# 판정
# ────────────────────────────────────────────────
def judge(raw):
    rem, rem_state = match_remark(raw["remark"])
    t1, t2 = parse_stamps(raw["body"])
    computed = None
    if t1 and t2:
        computed = int((t2 - t1).total_seconds())
    disp = parse_display(raw["interval"])
    idx = re.sub(r"\D", "", raw["index"])

    row = {
        "화면번호": idx, "민원번호": parse_complaint_no(raw["complaint_no"]),
        "차량번호(참고)": re.sub(r"\s+", "", raw["plate"]), "비고": rem if rem_state == "ok" else ("" if rem_state == "empty" else raw["remark"]),
        "1번째 촬영": t1.strftime("%H:%M:%S") if t1 else "", "2번째 촬영": t2.strftime("%H:%M:%S") if t2 else "",
        "간격(계산)": fmt_sec(computed) if computed is not None else "",
        "간격(화면표시)": fmt_sec(disp[0]) + ("~" if disp and disp[1] > disp[0] else "") if disp else "",
    }
    notes = []

    if rem_state == "empty":
        row.update({"기준": "", "판정": "비고없음", "사유": "비고가 비어 있어 판정하지 않음"})
        return row
    if rem_state == "unknown":
        row.update({"기준": "", "판정": "확인필요", "사유": f"비고를 못 읽음: '{raw['remark']}'"})
        return row

    need = ETC5_SEC if rem == "기타5분" else NORMAL_SEC
    row["기준"] = fmt_sec(need) + " 이상"

    verdicts = {}
    if computed is not None:
        if computed < 0:
            notes.append("2번째 시각이 1번째보다 빠름")
        else:
            verdicts["시각"] = computed >= need
    if disp:
        if disp[0] >= need:
            verdicts["표시"] = True
        elif disp[1] < need:
            verdicts["표시"] = False
        else:
            notes.append("화면 표시만으로는 판단 불가")

    if computed is not None and disp and not (disp[0] <= computed <= disp[1]):
        notes.append(f"계산({fmt_sec(computed)})과 화면표시({row['간격(화면표시)']}) 불일치 — 시각을 잘못 읽었을 수 있음")

    if not verdicts:
        row.update({"판정": "확인필요", "사유": "; ".join(notes) or "촬영시각과 촬영간격을 못 읽음"})
        return row
    if len(set(verdicts.values())) > 1:
        row.update({"판정": "확인필요", "사유": "시각 계산과 화면표시의 결론이 다름; " + "; ".join(notes)})
        return row

    ok = next(iter(verdicts.values()))
    if computed is None:
        notes.append("촬영시각 못 읽음 → 화면표시 기준")
    shown = fmt_sec(computed) if computed is not None and computed >= 0 else row["간격(화면표시)"]
    if ok:
        row["판정"] = "정상"
        row["사유"] = "; ".join(notes)
    else:
        row["판정"] = "부족"
        row["사유"] = f"{rem} {shown} (기준 {fmt_sec(need)} 이상)" + ("; " + "; ".join(notes) if notes else "")
    if ok and any("불일치" in n or "못 읽음" in n for n in notes):
        row["판정"] = "정상(주의)"
    return row


# ────────────────────────────────────────────────
# 엑셀
# ────────────────────────────────────────────────
COLS = ["No", "화면번호", "민원번호", "차량번호(참고)", "비고", "1번째 촬영", "2번째 촬영",
        "간격(계산)", "간격(화면표시)", "기준", "판정", "사유", "캡처파일"]
FILLS = {
    "부족": PatternFill("solid", fgColor="FFC7CE"),
    "확인필요": PatternFill("solid", fgColor="FFEB9C"),
    "정상(주의)": PatternFill("solid", fgColor="FFF2CC"),
    "비고없음": PatternFill("solid", fgColor="E7E6E6"),
}
WIDTHS = [6, 9, 20, 14, 14, 12, 12, 12, 14, 12, 11, 60, 14]


def write_sheet(ws, rows):
    ws.append(COLS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
        c.alignment = Alignment(horizontal="center")
    for r in rows:
        ws.append([r.get(c, "") for c in COLS])
        fill = FILLS.get(r["판정"])
        rn = ws.max_row
        if fill:
            for cell in ws[rn]:
                cell.fill = fill
        link = ws.cell(rn, len(COLS))
        link.hyperlink = r["캡처파일"]
        link.font = Font(color="0563C1", underline="single")
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def save_excel(rows, out_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "전체"
    write_sheet(ws, rows)
    bad = [r for r in rows if r["판정"] in ("부족", "확인필요", "정상(주의)")]
    ws2 = wb.create_sheet("부족·확인필요")
    write_sheet(ws2, bad)
    wb.save(out_path)


# ────────────────────────────────────────────────
# 실행
# ────────────────────────────────────────────────
def natural_key(p):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", os.path.basename(p))]


def pick_folder(base):
    if glob.glob(os.path.join(base, "*.png")):
        return base
    subs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)
            and glob.glob(os.path.join(d, "*.png"))]
    if not subs:
        return None
    return max(subs, key=os.path.getmtime)


def run(folder, engine_choice="auto"):
    files = sorted(glob.glob(os.path.join(folder, "*.png")), key=natural_key)
    if not files:
        print(f"[오류] PNG 파일이 없습니다: {folder}")
        return None
    engine = make_engine(engine_choice, folder)
    print(f"폴더: {folder}\n캡처 {len(files)}장 / OCR 엔진: {engine.name}")
    rows, t0, errors = [], time.time(), 0
    for i, f in enumerate(files, 1):
        try:
            raw, size = read_screen(engine, f)
            row = judge(raw)
            if row["판정"] == "확인필요":
                row["사유"] += " | 원문 비고=%r 간격=%r 본문=%r" % (
                    raw["remark"][:20], raw["interval"][:20], raw["body"][:60].replace("\n", " "))
            if abs(size[0] / BASE_W - 1) > 0.02 or abs(size[1] / BASE_H - 1) > 0.02:
                row["사유"] = (row.get("사유", "") + f"; 창 크기 {size[0]}x{size[1]} (기준 {BASE_W}x{BASE_H})").strip("; ")
        except Exception as e:
            if not errors:
                import traceback
                traceback.print_exc()          # 첫 실패만 자세히 보여준다
            errors += 1
            row = {"판정": "확인필요", "사유": f"읽기 실패: {type(e).__name__}: {e}"}
        row["No"] = i
        row["캡처파일"] = os.path.basename(f)
        rows.append(row)
        print(f"  [{i}/{len(files)}] {row.get('민원번호', '?')}  {row.get('비고', '')}  "
              f"{row.get('간격(계산)', '')}  → {row['판정']}")
    engine.close()

    out = os.path.join(folder, f"시간간격_결과_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    save_excel(rows, out)

    cnt = {}
    for r in rows:
        cnt[r["판정"]] = cnt.get(r["판정"], 0) + 1
    print(f"\n완료 ({time.time() - t0:.0f}초)  " + "  ".join(f"{k} {v}" for k, v in cnt.items()))
    for r in rows:
        if r["판정"] in ("부족", "확인필요", "정상(주의)"):
            print(f"  ▶ {r['판정']}: {r.get('민원번호', '')}  {r.get('사유', '')}  ({r['캡처파일']})")
    print(f"엑셀: {out}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="촬영간격 부족 건 엑셀 추출")
    ap.add_argument("folder", nargs="?", help="캡처 PNG 가 있는 폴더 (생략하면 최근 폴더)")
    ap.add_argument("--engine", choices=["auto", "windows", "tesseract"], default="auto")
    a = ap.parse_args(argv)
    folder = a.folder or pick_folder(DEFAULT_BASE)
    if not folder or not os.path.isdir(folder):
        print(f"[오류] 캡처 폴더를 찾지 못했습니다: {a.folder or DEFAULT_BASE}")
        return 1
    return 0 if run(folder, a.engine) else 1


if __name__ == "__main__":
    sys.exit(main())
