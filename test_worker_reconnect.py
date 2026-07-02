"""
TEST WORKER RECONNECT SCENARIO
================================
Simulates the exact bug: create browser → do actions → quit → create NEW browser
Tests BOTH the broken pattern (reuse ChromeService) and the fixed pattern (fresh ChromeService).

This is the #1 root cause of multi-browser failures:
When a ChromeService is reused after driver.quit(), the chromedriver process may be stale,
causing Chrome to open but all WebDriver commands to be silently dropped.
"""
import threading, time, tempfile, shutil, sys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DRIVER_PATH = ChromeDriverManager().install()

FORM_HTML = """data:text/html;charset=utf-8,<!DOCTYPE html>
<html><head><title>ReconnectTest</title></head>
<body>
<input id="inp" name="inp" type="text" autocomplete="off">
<button id="btn" type="button" onclick="document.getElementById('out').textContent='OK:'+document.getElementById('inp').value">Go</button>
<div id="out"></div>
</body></html>"""

def do_actions(driver, email, label):
    """Run type+click+verify on an open browser. Returns True if it worked."""
    try:
        driver.get(FORM_HTML)
        time.sleep(0.5)
        WebDriverWait(driver, 5).until(lambda d: d.execute_script("return document.readyState") == "complete")

        inp = driver.find_element(By.ID, "inp")
        driver.execute_script("""
            var el = arguments[0]; var val = arguments[1];
            el.focus(); el.value = '';
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
        """, inp, email)
        time.sleep(0.2)
        actual = driver.execute_script("return arguments[0].value", inp) or ''
        if actual != email:
            print(f"    [{label}] TYPE FAILED: expected '{email}' got '{actual}'", flush=True)
            return False

        btn = driver.find_element(By.ID, "btn")
        driver.execute_script("""
            var el = arguments[0];
            ['mousedown','mouseup','click'].forEach(function(t){
                el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
            });
        """, btn)
        time.sleep(0.3)
        out = driver.find_element(By.ID, "out").text
        expected = f"OK:{email}"
        if out != expected:
            print(f"    [{label}] CLICK FAILED: expected '{expected}' got '{out}'", flush=True)
            return False

        print(f"    [{label}] PASS - typed '{email}', clicked, got '{out}'", flush=True)
        return True
    except Exception as e:
        print(f"    [{label}] ERROR: {e}", flush=True)
        return False


def test_reuse_service():
    """BAD PATTERN: Create ChromeService once, quit browser, reuse same service for new browser."""
    print("\n--- TEST: REUSE ChromeService after driver.quit() ---", flush=True)
    service = ChromeService(DRIVER_PATH)
    results = []

    for round_num in range(1, 4):
        profile = tempfile.mkdtemp(prefix=f"reuse_r{round_num}_")
        port = 9400 + round_num
        try:
            opts = Options()
            opts.add_argument("--disable-gpu")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--log-level=3")
            opts.add_argument(f"--remote-debugging-port={port}")
            opts.add_argument(f"--user-data-dir={profile}")

            driver = webdriver.Chrome(service=service, options=opts)
            driver.set_page_load_timeout(10)
            email = f"reuse_round{round_num}@test.com"
            ok = do_actions(driver, email, f"REUSE-R{round_num}")
            results.append(ok)
            driver.quit()
            time.sleep(1)
        except Exception as e:
            print(f"    [REUSE-R{round_num}] EXCEPTION creating browser: {e}", flush=True)
            results.append(False)
        finally:
            try: shutil.rmtree(profile, ignore_errors=True)
            except: pass

    return results


def test_fresh_service():
    """GOOD PATTERN: Create a fresh ChromeService for each browser."""
    print("\n--- TEST: FRESH ChromeService per browser ---", flush=True)
    results = []

    for round_num in range(1, 4):
        profile = tempfile.mkdtemp(prefix=f"fresh_r{round_num}_")
        port = 9410 + round_num
        service = None
        try:
            opts = Options()
            opts.add_argument("--disable-gpu")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--log-level=3")
            opts.add_argument(f"--remote-debugging-port={port}")
            opts.add_argument(f"--user-data-dir={profile}")

            service = ChromeService(DRIVER_PATH)
            driver = webdriver.Chrome(service=service, options=opts)
            driver.set_page_load_timeout(10)
            email = f"fresh_round{round_num}@test.com"
            ok = do_actions(driver, email, f"FRESH-R{round_num}")
            results.append(ok)
            driver.quit()
            try: service.stop()
            except: pass
            time.sleep(1)
        except Exception as e:
            print(f"    [FRESH-R{round_num}] EXCEPTION creating browser: {e}", flush=True)
            results.append(False)
            if service:
                try: service.stop()
                except: pass
        finally:
            try: shutil.rmtree(profile, ignore_errors=True)
            except: pass

    return results


def test_parallel_fresh():
    """GOOD PATTERN in parallel: Multiple threads each with fresh ChromeService."""
    print("\n--- TEST: PARALLEL fresh ChromeService (3 threads, each quit+reopen) ---", flush=True)
    all_results = {}
    lock = threading.Lock()

    def worker(tid):
        thread_results = []
        for round_num in range(1, 3):
            profile = tempfile.mkdtemp(prefix=f"par_t{tid}_r{round_num}_")
            port = 9420 + tid * 10 + round_num
            service = None
            try:
                opts = Options()
                opts.add_argument("--disable-gpu")
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                opts.add_argument("--log-level=3")
                opts.add_argument(f"--remote-debugging-port={port}")
                opts.add_argument(f"--user-data-dir={profile}")

                service = ChromeService(DRIVER_PATH)
                driver = webdriver.Chrome(service=service, options=opts)
                driver.set_page_load_timeout(10)
                email = f"par_t{tid}_r{round_num}@test.com"
                ok = do_actions(driver, email, f"PAR-T{tid}-R{round_num}")
                thread_results.append(ok)
                driver.quit()
                try: service.stop()
                except: pass
                time.sleep(0.5)
            except Exception as e:
                print(f"    [PAR-T{tid}-R{round_num}] EXCEPTION: {e}", flush=True)
                thread_results.append(False)
                if service:
                    try: service.stop()
                    except: pass
            finally:
                try: shutil.rmtree(profile, ignore_errors=True)
                except: pass

        with lock:
            all_results[tid] = thread_results

    threads = []
    for i in range(3):
        t = threading.Thread(target=worker, args=(i+1,))
        t.start()
        threads.append(t)
        time.sleep(1)

    for t in threads:
        t.join(timeout=60)

    return all_results


# ── Run tests ──────────────────────────────────────────────────────
print("="*60, flush=True)
print("  WORKER RECONNECT TEST", flush=True)
print("="*60, flush=True)

reuse_results = test_reuse_service()
fresh_results = test_fresh_service()
parallel_results = test_parallel_fresh()

print("\n" + "="*60, flush=True)
print("  SUMMARY", flush=True)
print("="*60, flush=True)

reuse_pass = sum(reuse_results)
fresh_pass = sum(fresh_results)
par_pass = sum(v for vals in parallel_results.values() for v in vals)
par_total = sum(len(vals) for vals in parallel_results.values())

print(f"  REUSE ChromeService:    {reuse_pass}/{len(reuse_results)} rounds passed", flush=True)
print(f"  FRESH ChromeService:    {fresh_pass}/{len(fresh_results)} rounds passed", flush=True)
print(f"  PARALLEL fresh:         {par_pass}/{par_total} rounds passed", flush=True)

if reuse_pass < len(reuse_results) and fresh_pass == len(fresh_results):
    print("\n  CONFIRMED: Reusing ChromeService after quit causes failures!", flush=True)
    print("  FIX: Always create a fresh ChromeService for each new browser.", flush=True)
elif reuse_pass == len(reuse_results) and fresh_pass == len(fresh_results):
    print("\n  Both patterns work in sequential mode.", flush=True)
    print("  The failure may only manifest under higher load or with real websites.", flush=True)
    print("  The fresh pattern is still safer as it guarantees a clean chromedriver process.", flush=True)

print("="*60, flush=True)
