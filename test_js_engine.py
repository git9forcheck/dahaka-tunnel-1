"""
Test the NEW JavaScript-first automation engine with 5 browsers.
Uses the exact same JS approach now in the project: nativeInputValueSetter + JS click.
"""
import threading, time, tempfile, shutil, os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

print("Installing ChromeDriver...")
DRIVER_PATH = ChromeDriverManager().install()

NUM_BROWSERS = 5
TARGET_URL = "https://www.netflix.com/loginhelp"
emails = [f"jstest{i}@gmail.com" for i in range(1, NUM_BROWSERS + 1)]
results = {}
results_lock = threading.Lock()

def test_js_browser(bid, email, debug_port):
    profile_dir = tempfile.mkdtemp(prefix=f"jstest_b{bid}_")
    driver = None
    report = {"bid": bid, "email": email, "steps": []}
    
    def log(msg):
        print(f"  [B{bid}] {msg}")
    
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
        
        driver = webdriver.Chrome(service=ChromeService(DRIVER_PATH), options=opts)
        driver.set_page_load_timeout(30)
        log("Browser created")
        
        # Navigate
        driver.get(TARGET_URL)
        time.sleep(3)
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input, button")))
        body_len = driver.execute_script("return document.body.innerHTML.length")
        log(f"Navigate OK: bodyLen={body_len}")
        report["steps"].append({"action": "navigate", "status": "OK", "bodyLen": body_len})
        
        # Find email input
        el = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.NAME, "email")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", el)
        time.sleep(0.15)
        log("Found email input")
        
        # === JAVASCRIPT-FIRST TYPE (exact same code as project) ===
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
            el.dispatchEvent(new Event('focus', {bubbles: true}));
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
        """, el, email)
        time.sleep(0.3)
        
        # Verify
        actual = driver.execute_script("return arguments[0].value", el) or ''
        typed_ok = email in actual
        log(f"JS TYPE: expected='{email}' actual='{actual}' MATCH={typed_ok}")
        report["steps"].append({"action": "type", "status": "OK" if typed_ok else "FAIL", "value": actual})
        
        # === JAVASCRIPT-FIRST CLICK (exact same code as project) ===
        # Find the "Email Me" button via xpath
        btn_xpath = "//*[@id='appMountPoint']//button"
        btns = driver.find_elements(By.XPATH, btn_xpath)
        btn = None
        for b in btns:
            if 'email' in (b.text or '').lower() or 'me' in (b.text or '').lower():
                btn = b
                break
        if not btn and btns:
            btn = btns[0]
        
        if btn:
            driver.execute_script("""
                var el = arguments[0];
                el.scrollIntoView({block: 'center'});
                el.click();
            """, btn)
            time.sleep(1)
            log(f"JS CLICK OK: button text='{btn.text[:20]}'")
            report["steps"].append({"action": "click", "status": "OK"})
        else:
            log("No button found!")
            report["steps"].append({"action": "click", "status": "NO_BUTTON"})
        
        # Save screenshot
        driver.save_screenshot(f"jstest_b{bid}.png")
        log("Screenshot saved")
        
    except Exception as e:
        log(f"ERROR: {e}")
        report["error"] = str(e)[:80]
    finally:
        if driver:
            try: driver.quit()
            except: pass
        time.sleep(0.5)
        try: shutil.rmtree(profile_dir, ignore_errors=True)
        except: pass
        with results_lock:
            results[bid] = report

print(f"\n{'='*60}")
print(f"  JS-FIRST ENGINE TEST ({NUM_BROWSERS} browsers)")
print(f"{'='*60}\n")

threads = []
for i in range(NUM_BROWSERS):
    t = threading.Thread(target=test_js_browser, args=(i+1, emails[i], 9500+i))
    threads.append(t)
    print(f"Starting browser-{i+1}...")
    t.start()
    time.sleep(3)

print(f"\nWaiting...\n")
for t in threads:
    t.join(timeout=90)

print(f"\n{'='*60}")
print(f"  RESULTS")
print(f"{'='*60}")
for bid in sorted(results.keys()):
    r = results[bid]
    print(f"\n  Browser-{bid} ({r['email']}):")
    if 'error' in r:
        print(f"    ERROR: {r['error']}")
    for s in r.get("steps", []):
        extra = f" value='{s.get('value','')[:25]}'" if s['action'] == 'type' else ''
        print(f"    {s['action']}: {s['status']}{extra}")

pass_count = sum(1 for r in results.values() if all(s['status'] == 'OK' for s in r.get('steps', [])))
print(f"\n  PASS: {pass_count}/{NUM_BROWSERS}")
print(f"{'='*60}")
