"""
Inspect Netflix login help page to find correct selectors
for valid and invalid email detection.
"""
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

DRIVER_PATH = ChromeDriverManager().install()

def test_email(email, label):
    opts = Options()
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--log-level=3")
    
    driver = webdriver.Chrome(service=ChromeService(DRIVER_PATH), options=opts)
    driver.set_page_load_timeout(30)
    
    try:
        print(f"\n{'='*60}")
        print(f"  Testing: {email} (expected: {label})")
        print(f"{'='*60}")
        
        # Navigate
        driver.get("https://www.netflix.com/loginhelp")
        time.sleep(3)
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")
        print(f"  Page loaded: {driver.current_url}")
        
        # Type email via JS
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
        actual = driver.execute_script("return arguments[0].value", el)
        print(f"  Typed: {actual}")
        
        # Click button
        btns = driver.find_elements(By.TAG_NAME, "button")
        clicked = False
        for btn in btns:
            txt = btn.text.lower()
            if 'email' in txt or 'me' in txt:
                driver.execute_script("arguments[0].click()", btn)
                clicked = True
                print(f"  Clicked button: '{btn.text}'")
                break
        if not clicked and btns:
            driver.execute_script("arguments[0].click()", btns[0])
            print(f"  Clicked first button: '{btns[0].text}'")
        
        # Wait for result
        time.sleep(8)
        
        # Get URL
        print(f"  URL after click: {driver.current_url}")
        
        # Get page text
        body_text = driver.find_element(By.TAG_NAME, "body").text
        print(f"\n  PAGE TEXT (first 500 chars):")
        for line in body_text[:500].split('\n'):
            if line.strip():
                print(f"    | {line.strip()}")
        
        # Find all h1, h2, h3, p, span with text
        print(f"\n  KEY ELEMENTS:")
        for tag in ['h1', 'h2', 'h3']:
            elems = driver.find_elements(By.TAG_NAME, tag)
            for e in elems:
                if e.text.strip():
                    cls = e.get_attribute('class') or ''
                    print(f"    <{tag} class='{cls}'> {e.text.strip()[:80]}")
        
        # Find error/success messages - look for specific classes
        for selector in ['[data-uia]', '.error-message', '.ui-message-contents', '[class*="error"]', '[class*="success"]', '[class*="alert"]', '[class*="message"]']:
            elems = driver.find_elements(By.CSS_SELECTOR, selector)
            for e in elems:
                if e.text.strip() and len(e.text.strip()) < 200:
                    tag = e.tag_name
                    cls = e.get_attribute('class') or ''
                    uia = e.get_attribute('data-uia') or ''
                    print(f"    <{tag} class='{cls[:50]}' data-uia='{uia}'> {e.text.strip()[:100]}")
        
        # Save screenshot
        fname = f"netflix_{label}.png"
        driver.save_screenshot(fname)
        print(f"\n  Screenshot: {fname}")
        
    except Exception as e:
        print(f"  ERROR: {e}")
    finally:
        driver.quit()

test_email("testmacros4412@gmail.com", "INVALID")
test_email("jack@gmail.com", "VALID")
