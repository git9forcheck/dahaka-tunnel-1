"""
Test script: Start a job with 10 emails via the API and monitor progress via socket.
"""
import requests
import socketio
import time
import sys
import json

BASE = "http://127.0.0.1:5000"

# 10 test emails
emails = """test1@gmail.com
test2@gmail.com
test3@gmail.com
test4@gmail.com
test5@gmail.com
test6@gmail.com
test7@gmail.com
test8@gmail.com
test9@gmail.com
test10@gmail.com"""

# Connect socket to receive live updates
sio = socketio.Client()
update_count = 0
last_status = {}

@sio.on('update')
def on_update(data):
    global update_count, last_status
    update_count += 1
    last_status = data
    status = data.get('status', 'unknown')
    processed = data.get('processed', 0)
    total = data.get('total_emails', 0)
    success = data.get('success', 0)
    failure = data.get('failure', 0)
    browsers = data.get('browser_threads', {})
    step_prog = data.get('step_progress', {})
    browser_stats_data = data.get('browser_stats', {})
    
    if update_count % 3 == 0 or status != 'running':  # print every 3rd update to reduce spam
        print(f"\n--- Update #{update_count} | Status: {status.upper()} ---")
        print(f"  Progress: {processed}/{total} | Valid: {success} | Invalid: {failure}")
        print(f"  Browsers:")
        for tid, st in sorted(browsers.items()):
            stats = browser_stats_data.get(tid, {})
            proxy = stats.get('proxy', '?')[:20]
            last_email = stats.get('last_email', '')[:25]
            print(f"    {tid}: {st} | proxy={proxy} | email={last_email}")
        if step_prog:
            print(f"  Step progress:")
            for tid, steps in sorted(step_prog.items()):
                print(f"    {tid}: {steps}")

@sio.on('log_entry')
def on_log(data):
    msg = data.get('message', '')
    lvl = data.get('level', 'info')
    if lvl in ('bad', 'warn', 'good') or 'browser' in msg.lower() or 'step' in msg.lower():
        print(f"  [LOG/{lvl}] {msg}")

@sio.on('check_finished')
def on_finished(data):
    print(f"\n✅ JOB FINISHED: {json.dumps(data)}")

@sio.on('job_stopped')
def on_stopped():
    print(f"\n⏹ JOB STOPPED")

print("Connecting to server...")
sio.connect(BASE)
time.sleep(1)

# Use domain_id=4 (Netflix) since user mentioned Netflix testing
# Change to domain_id=1 for Amazon
domain_id = 4
print(f"\nStarting job with 10 emails on domain_id={domain_id}...")
resp = requests.post(f"{BASE}/api/job/start", json={
    "domain_id": domain_id,
    "emails": emails
})
print(f"API response: {resp.status_code} {resp.json()}")

if resp.status_code != 200:
    print("Failed to start job!")
    sio.disconnect()
    sys.exit(1)

# Monitor for up to 3 minutes
print("\nMonitoring job (will wait up to 3 minutes)...")
start = time.time()
while time.time() - start < 180:
    time.sleep(2)
    if last_status.get('status') in ('finished', 'idle') and update_count > 5:
        print("\nJob completed!")
        break

print(f"\n=== FINAL STATE ===")
print(f"Status: {last_status.get('status', 'unknown')}")
print(f"Processed: {last_status.get('processed', 0)}/{last_status.get('total_emails', 0)}")
print(f"Valid: {last_status.get('success', 0)}")
print(f"Invalid: {last_status.get('failure', 0)}")
print(f"Total updates received: {update_count}")

sio.disconnect()
