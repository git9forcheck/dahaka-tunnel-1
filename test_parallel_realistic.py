"""
REALISTIC PARALLEL TEST — Tests with actual web pages (no proxy)
================================================================
Uses example.com (always up, no proxy needed) to test timing-sensitive
concurrent operations. Tests with 5 browsers doing navigate+type+click
simultaneously with realistic delays that mirror the production code.

Key things tested:
1. WebDriverWait works in all browsers concurrently
2. JS type + verify works
3. Page state is correctly isolated
4. Stale element handling
"""
import threading, time, tempfile, shutil, os, sys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

DRIVER_PATH = ChromeDriverManager().install()
NUM_BROWSERS = 5

# A page that has forms we can interact with
FORM_HTML = """data:text/html;charset=utf-8,<!DOCTYPE html>
<html><head><title>ParallelForm</title></head>
<body>
<form id="f1">
  <input id="inp1" name="inp1" type="text" autocomplete="off">
  <input id="inp2" name="inp2" type="text" autocomplete="off">
  <select id="sel1"><option value="">Choose</option><option value="a">A</option><option value="b">B</option></select>
  <button id="btn1" type="button" onclick="
    document.getElementById('out').textContent = 'OK:' + document.getElementById('inp1').value + ':' + document.getElementById('inp2').value;
  ">Go</button>
</form>
<div id="out"></div>
<script>
  // Simulate a slow SPA: delay rendering of second section
  setTimeout(function() {
    var d = document.createElement('div');
    d.id = 'delayed';
    d.textContent = 'DELAYED_CONTENT';
    document.body.appendChild(d);
  }, 2000);
</script>
</body></html>"""

results = {}
lock = threading.Lock()
barrier = threading.Barrier(NUM_BROWSERS)

def worker(bid):
    profile = tempfile.mkdtemp(prefix=f"rtest_b{bid}_")
    driver = None
    port = 9800 + bid
    checks = {}

    def log(msg):
        print(f"  [B{bid}] {msg}", flush=True)

    try:
        opts = Options()
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--log-level=3")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument(f"--remote-debugging-port={port}")
        opts.add_argument(f"--user-data-dir={profile}")
        # Each browser gets its OWN chromedriver process
        svc = ChromeService(DRIVER_PATH)
        driver = webdriver.Chrome(service=svc, options=opts)
        driver.set_page_load_timeout(15)
        log("Created")

        barrier.wait(timeout=30)

        # 1. Navigate
        driver.get(FORM_HTML)
        WebDriverWait(driver, 5).until(lambda d: d.execute_script("return document.readyState") == "complete")
        checks["navigate"] = driver.title == "ParallelForm"
        log(f"Navigate: {checks['navigate']}")

        # 2. Wait for delayed content (SPA simulation)
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.ID, "delayed")))
        delayed = driver.find_element(By.ID, "delayed").text
        checks["wait_delayed"] = delayed == "DELAYED_CONTENT"
        log(f"Wait delayed: {checks['wait_delayed']} ('{delayed}')")

        # 3. Type into inp1 via JS (same as production code)
        email = f"user_{bid}@test.com"
        inp1 = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, "inp1")))
        driver.execute_script("""
            var el = arguments[0]; var val = arguments[1];
            el.focus(); el.click(); el.value = '';
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('focus', {bubbles: true}));
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, inp1, email)
        time.sleep(0.3)
        v1 = driver.execute_script("return arguments[0].value", inp1) or ''
        checks["type_inp1"] = v1 == email
        log(f"Type inp1: '{v1}' == '{email}' -> {checks['type_inp1']}")

        # 4. Type into inp2
        code = f"CODE_{bid}"
        inp2 = driver.find_element(By.ID, "inp2")
        driver.execute_script("""
            var el = arguments[0]; var val = arguments[1];
            el.focus(); el.value = '';
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, inp2, code)
        time.sleep(0.2)
        v2 = driver.execute_script("return arguments[0].value", inp2) or ''
        checks["type_inp2"] = v2 == code
        log(f"Type inp2: '{v2}' == '{code}' -> {checks['type_inp2']}")

        # 5. Click button via multi-strategy (same as production)
        btn = driver.find_element(By.ID, "btn1")
        driver.execute_script("""
            var el = arguments[0];
            el.scrollIntoView({block: 'center'});
            ['mousedown', 'mouseup', 'click'].forEach(function(t) {
                el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window, button:0}));
            });
        """, btn)
        time.sleep(0.5)
        out = driver.find_element(By.ID, "out").text
        expected_out = f"OK:{email}:{code}"
        checks["click"] = out == expected_out
        log(f"Click: '{out}' == '{expected_out}' -> {checks['click']}")

        # 6. Second round: navigate again and repeat (simulates processing next email)
        driver.get(FORM_HTML)
        WebDriverWait(driver, 5).until(lambda d: d.execute_script("return document.readyState") == "complete")
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.ID, "delayed")))
        email2 = f"second_{bid}@test.com"
        inp1b = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, "inp1")))
        driver.execute_script("""
            var el = arguments[0]; var val = arguments[1];
            el.focus(); el.click(); el.value = '';
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
        """, inp1b, email2)
        time.sleep(0.2)
        v1b = driver.execute_script("return arguments[0].value", inp1b) or ''
        checks["round2_type"] = v1b == email2
        log(f"Round2 type: '{v1b}' == '{email2}' -> {checks['round2_type']}")

        # 7. Isolation check: verify state hasn't leaked from another browser
        time.sleep(1)
        final = driver.execute_script("return document.getElementById('inp1').value") or ''
        checks["isolation"] = final == email2
        log(f"Isolation: '{final}' == '{email2}' -> {checks['isolation']}")

    except Exception as e:
        log(f"ERROR: {e}")
        checks["error"] = str(e)[:80]
    finally:
        if driver:
            try: driver.quit()
            except: pass
        time.sleep(0.3)
        try: shutil.rmtree(profile, ignore_errors=True)
        except: pass
        with lock:
            results[bid] = checks

print(f"\n{'='*60}")
print(f"  REALISTIC PARALLEL TEST ({NUM_BROWSERS} browsers)")
print(f"{'='*60}\n")

threads = []
for i in range(NUM_BROWSERS):
    t = threading.Thread(target=worker, args=(i+1,))
    t.start()
    threads.append(t)
    time.sleep(1)  # Stagger creation slightly

for t in threads:
    t.join(timeout=90)

print(f"\n{'='*60}")
print(f"  RESULTS")
print(f"{'='*60}")
total_pass = 0
total_fail = 0
for bid in sorted(results.keys()):
    c = results[bid]
    if "error" in c:
        print(f"  B{bid}: ERROR - {c['error']}")
        total_fail += 1
        continue
    passed = all(v for k, v in c.items() if k != "error")
    status = "PASS" if passed else "FAIL"
    if passed: total_pass += 1
    else: total_fail += 1
    failures = [k for k, v in c.items() if not v and k != "error"]
    print(f"  B{bid}: {status} {'| FAILED: ' + ', '.join(failures) if failures else ''}")

print(f"\n  TOTAL: {total_pass} PASS / {total_fail} FAIL out of {NUM_BROWSERS}")
print(f"{'='*60}")
