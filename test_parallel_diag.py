"""
PARALLEL BROWSER DIAGNOSTIC
============================
Tests the ROOT CAUSE of multi-browser failures:

Problem hypothesis: When multiple Chrome browsers share a ChromeService
instance OR use the same chromedriver binary simultaneously, the DevTools
protocol commands (CDP) can get cross-wired — browser A receives B's response.

This test launches 3 browsers SIMULTANEOUSLY (not staggered) and has each
one perform navigate → type → verify → click in parallel.  It checks:
  1. Each browser gets its OWN ChromeService (separate chromedriver process)
  2. Each browser gets its OWN user-data-dir and debugging port
  3. JavaScript-based type+click works in all browsers concurrently

We test with AND without separate ChromeService instances to prove the fix.
"""
import threading
import time
import tempfile
import shutil
import os
import sys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

print("Installing ChromeDriver...")
DRIVER_PATH = ChromeDriverManager().install()
print(f"ChromeDriver: {DRIVER_PATH}")

NUM_BROWSERS = 3
# Use a simple test page that we control — no proxy needed
# We'll use data: URI to create a self-contained test form
TEST_HTML = """data:text/html,
<html>
<head><title>Parallel Test</title></head>
<body>
  <h1 id="heading">Test Form</h1>
  <form id="testform">
    <input id="email" name="email" type="text" placeholder="Email">
    <input id="name" name="name" type="text" placeholder="Name">
    <button id="submit" type="button" onclick="document.getElementById('result').innerText='SUBMITTED:'+document.getElementById('email').value">Submit</button>
  </form>
  <div id="result"></div>
</body>
</html>"""

results = {}
results_lock = threading.Lock()
barrier = threading.Barrier(NUM_BROWSERS)  # ensures truly simultaneous start

def test_browser(bid, use_shared_service, shared_service=None):
    """Test a single browser instance."""
    profile_dir = tempfile.mkdtemp(prefix=f"pdiag_b{bid}_")
    driver = None
    debug_port = 9700 + bid
    email = f"parallel_test_{bid}@example.com"
    report = {"bid": bid, "mode": "SHARED_SERVICE" if use_shared_service else "OWN_SERVICE", "checks": {}}

    def log(msg):
        print(f"  [B{bid}|{'SHARED' if use_shared_service else 'OWN'}] {msg}", flush=True)

    try:
        opts = Options()
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--log-level=3")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument(f"--remote-debugging-port={debug_port}")
        opts.add_argument(f"--user-data-dir={profile_dir}")

        if use_shared_service:
            service = shared_service
        else:
            service = ChromeService(DRIVER_PATH)

        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_page_load_timeout(10)
        log("Browser created ✓")

        # Wait for all browsers to be ready before starting actions
        log("Waiting at barrier for simultaneous start...")
        barrier.wait(timeout=30)
        log("Barrier passed — starting actions simultaneously!")

        # === NAVIGATE ===
        driver.get(TEST_HTML)
        time.sleep(0.5)
        title = driver.title
        report["checks"]["navigate"] = title == "Parallel Test"
        log(f"Navigate: title='{title}' {'✓' if report['checks']['navigate'] else '✗'}")

        # === TYPE (JavaScript) ===
        el = driver.find_element(By.ID, "email")
        driver.execute_script("""
            var el = arguments[0];
            var val = arguments[1];
            el.focus();
            el.click();
            el.value = '';
            var nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeSetter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, el, email)
        time.sleep(0.2)
        actual = driver.execute_script("return arguments[0].value", el) or ''
        report["checks"]["type"] = email == actual
        log(f"Type: expected='{email}' actual='{actual}' {'✓' if report['checks']['type'] else '✗'}")

        # === TYPE SECOND FIELD (to test isolation) ===
        el2 = driver.find_element(By.ID, "name")
        name_val = f"User_{bid}"
        driver.execute_script("""
            var el = arguments[0]; var val = arguments[1];
            el.focus(); el.value = '';
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
        """, el2, name_val)
        time.sleep(0.1)
        actual2 = driver.execute_script("return arguments[0].value", el2) or ''
        report["checks"]["type_field2"] = name_val == actual2
        log(f"Type field2: expected='{name_val}' actual='{actual2}' {'✓' if report['checks']['type_field2'] else '✗'}")

        # === CLICK (JavaScript) ===
        btn = driver.find_element(By.ID, "submit")
        driver.execute_script("""
            var el = arguments[0];
            el.scrollIntoView({block: 'center'});
            ['mousedown', 'mouseup', 'click'].forEach(function(eventType) {
                var event = new MouseEvent(eventType, {
                    bubbles: true, cancelable: true, view: window, button: 0, buttons: 1
                });
                el.dispatchEvent(event);
            });
        """, btn)
        time.sleep(0.3)

        result_text = driver.find_element(By.ID, "result").text
        report["checks"]["click"] = f"SUBMITTED:{email}" == result_text
        log(f"Click: result='{result_text}' {'✓' if report['checks']['click'] else '✗'}")

        # === VERIFY ISOLATION: re-read email field to make sure it wasn't changed by another browser ===
        time.sleep(0.5)
        final_email = driver.execute_script("return document.getElementById('email').value") or ''
        report["checks"]["isolation"] = email == final_email
        log(f"Isolation: email still='{final_email}' {'✓' if report['checks']['isolation'] else '✗'}")

        # === Check chromedriver PID ===
        try:
            svc = service
            pid = svc.process.pid if svc.process else "?"
            report["chromedriver_pid"] = pid
            log(f"ChromeDriver PID: {pid}")
        except:
            report["chromedriver_pid"] = "N/A"

    except Exception as e:
        log(f"ERROR: {e}")
        report["error"] = str(e)[:100]
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        time.sleep(0.3)
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except:
            pass
        with results_lock:
            results[f"{bid}_{'shared' if use_shared_service else 'own'}"] = report

def run_test_round(label, use_shared):
    """Run one round of tests with either shared or own ChromeService."""
    global barrier
    barrier = threading.Barrier(NUM_BROWSERS)
    results.clear()

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}\n")

    shared_service = ChromeService(DRIVER_PATH) if use_shared else None

    threads = []
    for i in range(NUM_BROWSERS):
        t = threading.Thread(target=test_browser, args=(i+1, use_shared, shared_service))
        t.start()
        threads.append(t)
        time.sleep(0.5)  # Small stagger for browser creation only

    for t in threads:
        t.join(timeout=60)

    # Print results
    all_pass = True
    pids = set()
    for key in sorted(results.keys()):
        r = results[key]
        checks = r.get("checks", {})
        passed = all(checks.values()) and len(checks) > 0
        if not passed:
            all_pass = False
        pid = r.get("chromedriver_pid", "?")
        pids.add(str(pid))
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  B{r['bid']} [{r['mode']}]: {status} | PID={pid} | {checks}")
        if "error" in r:
            print(f"    ERROR: {r['error']}")

    print(f"\n  ChromeDriver PIDs: {pids}")
    if len(pids) > 1 or (len(pids) == 1 and "?" not in pids):
        print(f"  → {'SEPARATE' if len(pids) > 1 else 'SHARED'} chromedriver processes")
    print(f"  → Overall: {'✅ ALL PASS' if all_pass else '❌ SOME FAILURES'}")
    return all_pass

# ── Run both tests ──────────────────────────────────────────────────
print(f"\nThis test verifies whether multiple browsers can perform actions simultaneously.")
print(f"It tests with SHARED ChromeService vs SEPARATE ChromeService instances.\n")

result_shared = run_test_round("TEST 1: SHARED ChromeService (current code pattern)", use_shared=True)
result_own = run_test_round("TEST 2: SEPARATE ChromeService per browser (proposed fix)", use_shared=False)

print(f"\n{'='*60}")
print(f"  FINAL SUMMARY")
print(f"{'='*60}")
print(f"  Shared ChromeService:   {'✅ PASS' if result_shared else '❌ FAIL'}")
print(f"  Separate ChromeService: {'✅ PASS' if result_own else '❌ FAIL'}")
if not result_shared and result_own:
    print(f"\n  ⚠️  CONFIRMED: Shared ChromeService causes parallel failures!")
    print(f"  → Fix: Each worker MUST create its own ChromeService instance.")
elif result_shared and result_own:
    print(f"\n  ℹ️  Both modes work with simple pages. The issue may only manifest")
    print(f"     with real proxy-routed pages under heavier load.")
print(f"{'='*60}")
