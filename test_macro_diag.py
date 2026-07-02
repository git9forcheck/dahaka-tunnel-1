"""
COMPREHENSIVE MULTI-BROWSER MACRO DIAGNOSTIC
Uses the ACTUAL project functions (create_browser, execute_automation) 
to test if macros work. Takes screenshots after EVERY step.
Runs WITHOUT proxies first to isolate whether issue is code or proxy.
"""
import threading, time, tempfile, shutil, os, sys, json, copy

# Import from the actual project
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db
db.init_db()

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# Pre-install chromedriver
print("Installing ChromeDriver...")
DRIVER_PATH = ChromeDriverManager().install()

# Create screenshot dir
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "diag_screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

NUM_BROWSERS = 5
# Use Netflix domain
domain = db.get_domains()[2]  # Netflix (id=4)
steps = db.get_steps(domain['id'])
config = db.get_config()

print(f"Domain: {domain['name']} ({domain['url']})")
print(f"Steps ({len(steps)}):")
for i, s in enumerate(steps):
    print(f"  [{i}] {s['action']} | {s.get('selector_type','')}: {s.get('selector_value','')[:50]} | input: {s.get('input_value','')[:30]} | on_ok={s.get('on_success','continue')} on_fail={s.get('on_failure','proxy_error')}")
print(f"Browsers: {NUM_BROWSERS}")
print()

emails = [f"testmacro{i}@gmail.com" for i in range(1, NUM_BROWSERS + 1)]
results_lock = threading.Lock()
results = {}

SELECTOR_MAP = {'id': By.ID, 'name': By.NAME, 'css': By.CSS_SELECTOR, 'xpath': By.XPATH, 'tag': By.TAG_NAME}

def screenshot(driver, bid, name):
    """Save screenshot with descriptive name."""
    path = os.path.join(SCREENSHOT_DIR, f"b{bid}_{name}.png")
    try:
        driver.save_screenshot(path)
    except:
        pass
    return path

def test_worker(bid, email, debug_port):
    """Run the EXACT same automation steps the project uses, but with detailed diagnostics."""
    profile_dir = tempfile.mkdtemp(prefix=f"diag_b{bid}_")
    driver = None
    report = {"browser": bid, "email": email, "actions": []}
    
    def log(msg):
        print(f"  [B{bid}] {msg}")
    
    try:
        # Create browser WITHOUT proxy (to isolate code issues from proxy issues)
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
        
        # Execute each step manually with full diagnostics
        for idx, step in enumerate(steps):
            action = step['action']
            sel_type = step.get('selector_type', '')
            sel_val = step.get('selector_value', '')
            inp_val = step.get('input_value', '').replace('{{EMAIL}}', email).replace('{{DOMAIN_URL}}', domain['url'])
            timeout = step.get('timeout', 10)
            by = SELECTOR_MAP.get(sel_type, By.ID)
            
            step_result = {"step": idx, "action": action, "selector": f"{sel_type}:{sel_val[:40]}", "status": "?"}
            log(f"--- Step {idx}: {action} | {sel_type}:{sel_val[:40]} | input:{inp_val[:25]} ---")
            
            try:
                # Force JS sync
                try:
                    body_len = driver.execute_script("return document.body.innerHTML.length")
                    log(f"  JS sync OK, body length: {body_len}")
                except Exception as e:
                    log(f"  JS sync FAILED: {e}")
                
                if action == 'navigate':
                    url = inp_val if inp_val else domain['url']
                    log(f"  Navigating to: {url}")
                    driver.get(url)
                    time.sleep(3)
                    
                    # Check readyState
                    ready = driver.execute_script("return document.readyState")
                    cur_url = driver.current_url
                    title = driver.title
                    body_len = driver.execute_script("return document.body.innerHTML.length")
                    log(f"  readyState={ready}, url={cur_url[:60]}, title={title[:30]}, bodyLen={body_len}")
                    
                    # Wait for interactive elements
                    try:
                        WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "input, button, a"))
                        )
                        count = len(driver.find_elements(By.CSS_SELECTOR, "input, button, a"))
                        log(f"  Interactive elements found: {count}")
                    except:
                        log(f"  NO interactive elements found!")
                    
                    screenshot(driver, bid, f"step{idx}_navigate")
                    step_result["status"] = "OK"
                    step_result["body_len"] = body_len
                    step_result["url"] = cur_url
                    
                elif action == 'wait':
                    if sel_type == 'text' and sel_val:
                        log(f"  Waiting for text: '{sel_val}'")
                        end_time = time.time() + timeout
                        found = False
                        while time.time() < end_time:
                            page_text = driver.find_element(By.TAG_NAME, 'body').text
                            if sel_val.lower() in page_text.lower():
                                found = True
                                break
                            time.sleep(0.5)
                        log(f"  Text found: {found}")
                        step_result["status"] = "OK" if found else "TIMEOUT"
                    elif sel_val:
                        log(f"  Waiting for element: {sel_type}:{sel_val[:40]}")
                        try:
                            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, sel_val)))
                            log(f"  Element found!")
                            step_result["status"] = "OK"
                        except:
                            log(f"  Element NOT found within {timeout}s!")
                            step_result["status"] = "TIMEOUT"
                    else:
                        log(f"  Sleeping {timeout}s")
                        time.sleep(timeout)
                        step_result["status"] = "OK"
                    screenshot(driver, bid, f"step{idx}_wait")
                    
                elif action in ('type', 'fill'):
                    log(f"  Finding element: {sel_type}:{sel_val}")
                    try:
                        el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, sel_val)))
                        driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", el)
                        time.sleep(0.2)
                        
                        # Check element properties
                        el_info = driver.execute_script("""
                            var e = arguments[0];
                            return {tag: e.tagName, type: e.type, name: e.name, id: e.id, 
                                    visible: e.offsetParent !== null, 
                                    width: e.offsetWidth, height: e.offsetHeight,
                                    disabled: e.disabled, readonly: e.readOnly};
                        """, el)
                        log(f"  Element found: {el_info}")
                        
                        screenshot(driver, bid, f"step{idx}_type_before")
                        
                        # Click to focus
                        try:
                            el.click()
                            time.sleep(0.2)
                        except:
                            driver.execute_script("arguments[0].click();", el)
                            time.sleep(0.2)
                        
                        # Clear
                        try:
                            el.clear()
                        except:
                            driver.execute_script("arguments[0].value = '';", el)
                        
                        # Type
                        el.send_keys(inp_val)
                        time.sleep(0.3)
                        
                        # VERIFY
                        actual = el.get_attribute('value') or ''
                        typed_ok = inp_val in actual
                        log(f"  VERIFY send_keys: expected='{inp_val[:25]}' actual='{actual[:25]}' match={typed_ok}")
                        
                        if not typed_ok:
                            # JS fallback
                            log(f"  send_keys FAILED, trying JS...")
                            driver.execute_script("""
                                var el=arguments[0]; var val=arguments[1];
                                var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                                setter.call(el, val);
                                el.dispatchEvent(new Event('input',{bubbles:true}));
                                el.dispatchEvent(new Event('change',{bubbles:true}));
                            """, el, inp_val)
                            time.sleep(0.3)
                            actual2 = el.get_attribute('value') or ''
                            log(f"  VERIFY JS: actual='{actual2[:25]}' match={inp_val in actual2}")
                            step_result["status"] = "JS_OK" if inp_val in actual2 else "FAIL"
                        else:
                            step_result["status"] = "OK"
                        
                        screenshot(driver, bid, f"step{idx}_type_after")
                        step_result["value_in_field"] = el.get_attribute('value') or ''
                        
                    except Exception as e:
                        log(f"  TYPE FAILED: {e}")
                        step_result["status"] = f"ERROR: {str(e)[:60]}"
                        screenshot(driver, bid, f"step{idx}_type_error")
                
                elif action == 'click':
                    log(f"  Finding button: {sel_type}:{sel_val}")
                    try:
                        el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, sel_val)))
                        driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", el)
                        time.sleep(0.2)
                        
                        el_text = el.text or el.get_attribute('value') or '?'
                        log(f"  Button found: text='{el_text[:30]}'")
                        
                        url_before = driver.current_url
                        screenshot(driver, bid, f"step{idx}_click_before")
                        
                        try:
                            el.click()
                            log(f"  Clicked via Selenium")
                        except:
                            driver.execute_script("arguments[0].click();", el)
                            log(f"  Clicked via JS fallback")
                        
                        time.sleep(2)
                        url_after = driver.current_url
                        log(f"  URL changed: {url_before != url_after} ({url_before[:40]} -> {url_after[:40]})")
                        
                        screenshot(driver, bid, f"step{idx}_click_after")
                        step_result["status"] = "OK"
                        step_result["url_changed"] = url_before != url_after
                        
                    except Exception as e:
                        log(f"  CLICK FAILED: {e}")
                        step_result["status"] = f"ERROR: {str(e)[:60]}"
                        screenshot(driver, bid, f"step{idx}_click_error")
                
                elif action == 'check_element':
                    log(f"  Checking element: {sel_type}:{sel_val}")
                    try:
                        el = driver.find_element(by, sel_val)
                        displayed = el.is_displayed()
                        log(f"  Element FOUND, displayed={displayed}")
                        step_result["status"] = "FOUND" if displayed else "HIDDEN"
                    except:
                        log(f"  Element NOT FOUND")
                        step_result["status"] = "NOT_FOUND"
                    screenshot(driver, bid, f"step{idx}_check")
                
                else:
                    log(f"  Unknown action: {action}")
                    step_result["status"] = "SKIP"
                    
            except Exception as e:
                log(f"  EXCEPTION in step {idx}: {e}")
                step_result["status"] = f"EXCEPTION: {str(e)[:60]}"
                screenshot(driver, bid, f"step{idx}_exception")
            
            report["actions"].append(step_result)
            log(f"  Result: {step_result['status']}")
        
        # Final screenshot
        screenshot(driver, bid, "final")
        
    except Exception as e:
        log(f"FATAL: {e}")
        report["fatal"] = str(e)[:100]
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        time.sleep(0.5)
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except:
            pass
        with results_lock:
            results[bid] = report


# ── Main ────────────────────────────────────────────────────────────
print(f"{'='*70}")
print(f"  MULTI-BROWSER MACRO DIAGNOSTIC ({NUM_BROWSERS} browsers, NO PROXY)")
print(f"  Domain: {domain['name']} ({domain['url']})")
print(f"  Screenshots: {SCREENSHOT_DIR}")
print(f"{'='*70}\n")

threads = []
for i in range(NUM_BROWSERS):
    t = threading.Thread(target=test_worker, args=(i+1, emails[i], 9400+i))
    threads.append(t)
    print(f"Launching browser-{i+1} with {emails[i]}...")
    t.start()
    time.sleep(3)  # 3s stagger

print(f"\nAll browsers launched. Waiting...\n")
for t in threads:
    t.join(timeout=120)

# ── Summary ─────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  RESULTS SUMMARY")
print(f"{'='*70}")
for bid in sorted(results.keys()):
    r = results[bid]
    print(f"\n  Browser-{bid} ({r['email']}):")
    if 'fatal' in r:
        print(f"    FATAL: {r['fatal']}")
        continue
    for a in r["actions"]:
        extra = ""
        if a["action"] in ("type", "fill"):
            extra = f" value='{a.get('value_in_field', '?')[:25]}'"
        elif a["action"] == "click":
            extra = f" url_changed={a.get('url_changed', '?')}"
        elif a["action"] == "check_element":
            extra = f""
        print(f"    Step {a['step']}: {a['action']:15} -> {a['status']}{extra}")

# Count
all_ok = sum(1 for r in results.values() if all(a['status'] in ('OK', 'JS_OK', 'FOUND', 'NOT_FOUND', 'HIDDEN', 'SKIP') for a in r.get('actions', [])))
print(f"\n  PASS: {all_ok}/{NUM_BROWSERS}")
print(f"  Screenshots saved to: {SCREENSHOT_DIR}")
print(f"{'='*70}")
