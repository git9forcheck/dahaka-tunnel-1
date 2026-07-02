"""
MULTI-BROWSER DIAGNOSTIC TEST
Opens 5 Chrome browsers simultaneously and verifies that Selenium actions
(navigate, type, click) actually work in each one by checking the DOM state.

Uses Netflix login page as the test target (same as the real job).
"""
import threading
import time
import tempfile
import shutil
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# ── Config ──────────────────────────────────────────────────────────
NUM_BROWSERS = 5
TARGET_URL = "https://www.netflix.com/loginhelp"
TEST_EMAILS = [f"testuser{i}@gmail.com" for i in range(1, NUM_BROWSERS + 1)]

# Pre-install chromedriver once
print("Installing ChromeDriver...")
DRIVER_PATH = ChromeDriverManager().install()
print(f"ChromeDriver: {DRIVER_PATH}")

results = {}
results_lock = threading.Lock()

def test_browser(browser_id, email, debug_port):
    """Open a browser, navigate, type, click, and verify each action."""
    profile_dir = tempfile.mkdtemp(prefix=f"test_b{browser_id}_")
    driver = None
    report = {
        "browser": f"browser-{browser_id}",
        "email": email,
        "steps": [],
        "final_verdict": "UNKNOWN"
    }

    def log(msg):
        print(f"  [B{browser_id}] {msg}")

    try:
        # ── Step 1: Create browser ──
        log("Creating browser...")
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
        driver.set_page_load_timeout(15)
        report["steps"].append({"action": "create_browser", "status": "OK"})
        log("Browser created ✓")

        # ── Step 2: Navigate ──
        log(f"Navigating to {TARGET_URL}...")
        driver.get(TARGET_URL)
        time.sleep(3)  # Wait for SPA to load
        
        # Verify page loaded
        ready_state = driver.execute_script("return document.readyState")
        current_url = driver.current_url
        page_title = driver.title
        body_len = driver.execute_script("return document.body.innerHTML.length")
        log(f"Navigate done: readyState={ready_state}, url={current_url[:60]}, title={page_title[:40]}, bodyLen={body_len}")
        report["steps"].append({
            "action": "navigate",
            "status": "OK" if body_len > 100 else "FAIL",
            "readyState": ready_state,
            "url": current_url,
            "bodyLen": body_len
        })

        # ── Step 3: Wait for interactive elements ──
        log("Waiting for interactive elements...")
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input, button, a"))
            )
            interactive_count = len(driver.find_elements(By.CSS_SELECTOR, "input, button, a"))
            log(f"Found {interactive_count} interactive elements ✓")
            report["steps"].append({"action": "wait_interactive", "status": "OK", "count": interactive_count})
        except Exception as e:
            log(f"No interactive elements found: {e}")
            report["steps"].append({"action": "wait_interactive", "status": "FAIL", "error": str(e)[:60]})

        # ── Step 4: Find email input field ──
        log("Looking for email input...")
        email_input = None
        selectors_tried = []
        
        # Try multiple selectors for the Netflix email field
        for sel_name, by, sel in [
            ("input[name='email']", By.CSS_SELECTOR, "input[name='email']"),
            ("input[type='email']", By.CSS_SELECTOR, "input[type='email']"),
            ("input#email", By.CSS_SELECTOR, "input#email"),
            ("first input", By.CSS_SELECTOR, "input"),
            ("xpath //input", By.XPATH, "//input"),
        ]:
            try:
                el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel)))
                email_input = el
                log(f"Found input with: {sel_name} ✓")
                selectors_tried.append(f"{sel_name}: FOUND")
                break
            except:
                selectors_tried.append(f"{sel_name}: NOT FOUND")
        
        if not email_input:
            # Dump all inputs on the page for debugging
            all_inputs = driver.find_elements(By.TAG_NAME, "input")
            log(f"No email input found! Page has {len(all_inputs)} inputs:")
            for inp in all_inputs:
                attrs = driver.execute_script("""
                    var el = arguments[0];
                    return {type: el.type, name: el.name, id: el.id, placeholder: el.placeholder, visible: el.offsetParent !== null};
                """, inp)
                log(f"  <input type={attrs['type']} name={attrs['name']} id={attrs['id']} placeholder={attrs['placeholder']} visible={attrs['visible']}>")
            report["steps"].append({"action": "find_input", "status": "FAIL", "selectors": selectors_tried})
            report["final_verdict"] = "FAIL - no email input found"
            return

        report["steps"].append({"action": "find_input", "status": "OK", "selectors": selectors_tried})

        # ── Step 5: Scroll to element ──
        driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", email_input)
        time.sleep(0.2)

        # ── Step 6: Click to focus ──
        log("Clicking input to focus...")
        try:
            email_input.click()
            time.sleep(0.2)
        except:
            driver.execute_script("arguments[0].click();", email_input)
            time.sleep(0.2)

        # ── Step 7: Type email using send_keys ──
        log(f"Typing email: {email}")
        value_before = email_input.get_attribute("value") or ""
        email_input.clear()
        email_input.send_keys(email)
        time.sleep(0.3)

        # ── Step 8: VERIFY the value was actually typed ──
        value_after = email_input.get_attribute("value") or ""
        typed_ok = email in value_after
        log(f"VERIFY TYPE: before='{value_before}' → after='{value_after}' | Match: {typed_ok}")
        
        if not typed_ok:
            # Try JS fallback
            log("send_keys FAILED — trying JavaScript setValue...")
            driver.execute_script("""
                var el = arguments[0]; var val = arguments[1];
                var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            """, email_input, email)
            time.sleep(0.3)
            value_after_js = email_input.get_attribute("value") or ""
            typed_ok_js = email in value_after_js
            log(f"VERIFY JS TYPE: after='{value_after_js}' | Match: {typed_ok_js}")
            report["steps"].append({
                "action": "type_email",
                "status": "JS_FALLBACK_" + ("OK" if typed_ok_js else "FAIL"),
                "send_keys_value": value_after,
                "js_value": value_after_js
            })
        else:
            report["steps"].append({
                "action": "type_email",
                "status": "OK",
                "value": value_after
            })

        # ── Step 9: Find and click submit button ──
        log("Looking for submit button...")
        submit_btn = None
        for sel_name, by, sel in [
            ("button[type='submit']", By.CSS_SELECTOR, "button[type='submit']"),
            ("button", By.CSS_SELECTOR, "button"),
            ("input[type='submit']", By.CSS_SELECTOR, "input[type='submit']"),
        ]:
            try:
                el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel)))
                submit_btn = el
                btn_text = el.text or el.get_attribute("value") or "?"
                log(f"Found button: {sel_name} text='{btn_text[:30]}' ✓")
                break
            except:
                pass

        if submit_btn:
            url_before = driver.current_url
            try:
                submit_btn.click()
                log("Button clicked ✓")
            except:
                driver.execute_script("arguments[0].click();", submit_btn)
                log("Button JS-clicked ✓")
            time.sleep(2)
            url_after = driver.current_url
            url_changed = url_before != url_after
            log(f"VERIFY CLICK: URL changed: {url_changed} ({url_before[:40]} → {url_after[:40]})")
            report["steps"].append({
                "action": "click_submit",
                "status": "OK",
                "url_changed": url_changed,
                "url_before": url_before,
                "url_after": url_after
            })
        else:
            log("No submit button found")
            report["steps"].append({"action": "click_submit", "status": "SKIP"})

        # ── Final verdict ──
        type_step = next((s for s in report["steps"] if s["action"] == "type_email"), {})
        if type_step.get("status") == "OK":
            report["final_verdict"] = "✅ PASS - send_keys works"
        elif "JS_FALLBACK_OK" in type_step.get("status", ""):
            report["final_verdict"] = "⚠️ PARTIAL - send_keys failed, JS fallback works"
        else:
            report["final_verdict"] = "❌ FAIL - neither send_keys nor JS works"

    except Exception as e:
        log(f"EXCEPTION: {e}")
        report["steps"].append({"action": "exception", "error": str(e)[:100]})
        report["final_verdict"] = f"❌ EXCEPTION: {str(e)[:80]}"
    finally:
        # Take screenshot for evidence
        if driver:
            try:
                screenshot_path = f"test_browser_{browser_id}.png"
                driver.save_screenshot(screenshot_path)
                log(f"Screenshot saved: {screenshot_path}")
            except:
                pass
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
            results[browser_id] = report


# ── Main ────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  MULTI-BROWSER DIAGNOSTIC TEST ({NUM_BROWSERS} browsers)")
print(f"{'='*60}\n")

# Launch browsers with stagger
threads = []
for i in range(NUM_BROWSERS):
    t = threading.Thread(target=test_browser, args=(i + 1, TEST_EMAILS[i], 9300 + i))
    threads.append(t)
    print(f"Starting browser-{i+1}...")
    t.start()
    time.sleep(3)  # Stagger launches

print(f"\nAll {NUM_BROWSERS} browsers launched. Waiting for completion...\n")

for t in threads:
    t.join(timeout=120)

# ── Summary ─────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  RESULTS SUMMARY")
print(f"{'='*60}")
for bid in sorted(results.keys()):
    r = results[bid]
    print(f"\n  Browser-{bid} ({r['email']}):")
    print(f"    Verdict: {r['final_verdict']}")
    for step in r["steps"]:
        action = step["action"]
        status = step["status"]
        extra = ""
        if action == "type_email":
            extra = f" value='{step.get('value', step.get('send_keys_value', ''))[:30]}'"
        elif action == "click_submit":
            extra = f" url_changed={step.get('url_changed', '?')}"
        elif action == "navigate":
            extra = f" bodyLen={step.get('bodyLen', '?')}"
        print(f"      {action}: {status}{extra}")

pass_count = sum(1 for r in results.values() if "PASS" in r["final_verdict"])
fail_count = sum(1 for r in results.values() if "FAIL" in r["final_verdict"])
partial_count = sum(1 for r in results.values() if "PARTIAL" in r["final_verdict"])
print(f"\n  TOTALS: {pass_count} PASS | {partial_count} PARTIAL | {fail_count} FAIL")
print(f"{'='*60}")
