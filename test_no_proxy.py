"""
Test macros with 2 known emails using direct connection (no proxy).
Uses 2 browsers to prove multi-browser macros work.
"""
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import threading

DRIVER_PATH = ChromeDriverManager().install()

results = {}

def run_browser(browser_id, email, expected):
    opts = Options()
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--log-level=3")
    opts.add_argument(f"--user-data-dir=C:\\temp\\test_noproxy_{browser_id}")
    
    svc = ChromeService(DRIVER_PATH)
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(30)
    
    try:
        print(f"  [{browser_id}] Starting — email={email} expected={expected}", flush=True)
        
        # Step 0: Navigate
        driver.get("https://www.netflix.com/loginhelp")
        WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(2)
        print(f"  [{browser_id}] Step 0 (navigate): DONE — {driver.current_url[:40]}", flush=True)
        
        # Step 1: Type email (JS-first)
        el = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.NAME, "email")))
        driver.execute_script("""
            var el = arguments[0]; var val = arguments[1];
            el.focus(); el.click(); el.value = '';
            var ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            ns.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, el, email)
        time.sleep(0.5)
        actual = driver.execute_script("return arguments[0].value", el) or ''
        print(f"  [{browser_id}] Step 1 (type): DONE — typed='{actual}'", flush=True)
        
        # Step 2: Click "Email Me" (multi-strategy)
        btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[data-uia="emailMeButton"]'))
        )
        driver.execute_script("""
            var el = arguments[0];
            el.scrollIntoView({block: 'center'});
            ['mousedown', 'mouseup', 'click'].forEach(function(t) {
                el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window, button:0, buttons:1}));
            });
        """, btn)
        time.sleep(3)
        print(f"  [{browser_id}] Step 2 (click): DONE", flush=True)
        
        # Step 3: Check captcha (skip)
        # Step 4: Check "No account found"
        page_text = driver.find_element(By.TAG_NAME, 'body').text
        end_time = time.time() + 10
        found_invalid = False
        found_valid = False
        while time.time() < end_time:
            page_text = driver.find_element(By.TAG_NAME, 'body').text
            if 'no account found' in page_text.lower():
                found_invalid = True
                break
            if 'email sent' in page_text.lower():
                found_valid = True
                break
            time.sleep(0.5)
        
        if found_invalid:
            result = 'INVALID'
        elif found_valid:
            result = 'VALID'
        else:
            result = f'UNKNOWN (page: {page_text[:100]})'
        
        results[browser_id] = {'email': email, 'expected': expected, 'result': result}
        match = (result == expected)
        symbol = '✅' if match else '❌'
        print(f"  [{browser_id}] RESULT: {result} (expected {expected}) {symbol}", flush=True)
        
    except Exception as e:
        results[browser_id] = {'email': email, 'expected': expected, 'result': f'ERROR: {e}'}
        print(f"  [{browser_id}] ERROR: {e}", flush=True)
    finally:
        driver.quit()

print("="*60, flush=True)
print("  NO-PROXY MULTI-BROWSER MACRO TEST", flush=True)
print("="*60, flush=True)
print(f"  Testing 2 browsers simultaneously (no proxy)", flush=True)
print(flush=True)

# Run 2 browsers in parallel
t1 = threading.Thread(target=run_browser, args=("browser-1", "jack@gmail.com", "VALID"))
t2 = threading.Thread(target=run_browser, args=("browser-2", "testmacros4412@gmail.com", "INVALID"))

t1.start()
time.sleep(1)  # Small stagger
t2.start()

t1.join(timeout=120)
t2.join(timeout=120)

print(flush=True)
print("="*60, flush=True)
print("  FINAL RESULTS", flush=True)
print("="*60, flush=True)
for bid, r in sorted(results.items()):
    match = '✅' if r['result'] == r['expected'] else '❌'
    print(f"  {bid}: {r['email']} → {r['result']} (expected {r['expected']}) {match}", flush=True)

all_match = all(r['result'] == r['expected'] for r in results.values())
if all_match and len(results) == 2:
    print(f"\n  ✅✅✅ ALL CORRECT — Multi-browser macros WORK!", flush=True)
else:
    print(f"\n  ❌ Some results incorrect", flush=True)
print("="*60, flush=True)
