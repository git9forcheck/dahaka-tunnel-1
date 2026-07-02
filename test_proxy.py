from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

proxy = '83.149.70.159:13012'
opts = Options()
opts.add_argument(f'--proxy-server={proxy}')
opts.add_argument('--disable-gpu')
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')
opts.add_argument('--log-level=3')
opts.add_argument('--headless')
driver = None
try:
    print('Starting Chrome with correct driver...')
    driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=opts)
    driver.set_page_load_timeout(15)
    print('Opening google.com via proxy...')
    driver.get('https://www.google.com')
    url = driver.current_url
    title = driver.title
    print(f'Current URL: {url}')
    print(f'Title: {title}')
    body = driver.find_element(By.TAG_NAME, 'body')
    print(f'Body found: {body is not None}')
    has_google_url = 'google' in url.lower()
    has_google_title = 'google' in title.lower()
    print(f'URL contains google: {has_google_url}')
    print(f'Title contains google: {has_google_title}')
    if has_google_url or has_google_title:
        print('RESULT: VALID')
    else:
        print('RESULT: INVALID - google not found in URL or title')
except Exception as e:
    print(f'ERROR: {type(e).__name__}: {e}')
finally:
    if driver:
        driver.quit()
