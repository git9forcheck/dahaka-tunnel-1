"""
Test with 10 emails including 2 KNOWN results:
  jack@gmail.com → should be VALID on Netflix
  testmacros4412@gmail.com → should be INVALID on Netflix
"""
import socketio, requests, time, sys

BASE = "http://127.0.0.1:5000"

emails = """jack@gmail.com
testmacros4412@gmail.com
fakeemail001@gmail.com
fakeemail002@gmail.com
fakeemail003@gmail.com
fakeemail004@gmail.com
fakeemail005@gmail.com
fakeemail006@gmail.com
fakeemail007@gmail.com
fakeemail008@gmail.com""".strip()

sio = socketio.Client()
update_count = 0
last_data = {}
all_logs = []

@sio.on('update')
def on_update(data):
    global update_count, last_data
    update_count += 1
    last_data = data

@sio.on('log_entry')
def on_log(data):
    msg = data.get('message', '')
    lvl = data.get('level', '')
    all_logs.append(msg)
    # Show key actions and results
    if any(kw in msg for kw in ['Navigate OK', 'Navigate FAILED', 'Typed', 'Clicked', 'Valid', 'Invalid', 'valid', 'invalid', 'After-click']):
        # Clean up unicode for display
        clean = msg.encode('ascii', 'replace').decode('ascii')
        print(f"  LOG[{lvl}]: {clean[:120]}", flush=True)

@sio.on('check_finished')
def on_finished(data):
    print(f"  JOB FINISHED: {data}", flush=True)

print("Connecting...", flush=True)
sio.connect(BASE)
time.sleep(1)

# Start job
print(f"\nStarting job: 10 emails, Netflix, 5 browsers", flush=True)
resp = requests.post(f"{BASE}/api/job/start", json={
    'emails': emails,
    'domain_id': 4,
    'concurrent_browsers': 5
})
print(f"API: {resp.status_code} {resp.json()}", flush=True)

if resp.status_code != 200:
    print("FAILED!", flush=True)
    sio.disconnect()
    sys.exit(1)

# Monitor
start = time.time()
timeout = 300
last_report = 0

print(f"\nMonitoring...\n", flush=True)

while time.time() - start < timeout:
    time.sleep(2)
    d = last_data
    elapsed = int(time.time() - start)
    processed = d.get('processed', 0)
    total = d.get('total_emails', 10)
    success = d.get('success', 0)
    failure = d.get('failure', 0)
    status = d.get('status', '?')

    if elapsed - last_report >= 15:
        print(f"\n  [{elapsed}s] {status} | {processed}/{total} | valid={success} invalid={failure}", flush=True)
        sp = d.get('step_progress', {})
        for bid in sorted(sp.keys()):
            print(f"    {bid}: steps={sp[bid]}", flush=True)
        # Show current_emails status  
        ce = d.get('current_emails', [])
        for e in ce:
            if e.get('email') in ('jack@gmail.com', 'testmacros4412@gmail.com'):
                print(f"    >>> {e['email']} → {e.get('status','?')}", flush=True)
        last_report = elapsed

    if status in ('idle', 'finished') and processed > 0:
        time.sleep(3)
        break
    if processed >= total:
        time.sleep(3)
        break

# FINAL
d = last_data
print(f"\n{'='*60}", flush=True)
print(f"  FINAL RESULTS", flush=True)
print(f"{'='*60}", flush=True)
print(f"  Processed: {d.get('processed',0)}/{d.get('total_emails',10)}", flush=True)
print(f"  Valid (success): {d.get('success',0)}", flush=True)
print(f"  Invalid (failure): {d.get('failure',0)}", flush=True)

# Check current_emails for per-email results
ce = d.get('current_emails', [])
print(f"\n  Per-email results (from current_emails):", flush=True)
jack_status = None
test_status = None
for e in ce:
    em = e.get('email', '')
    st = e.get('status', '?')
    if em == 'jack@gmail.com':
        jack_status = st
    elif em == 'testmacros4412@gmail.com':
        test_status = st
    print(f"    {em} → {st}", flush=True)

# Also search logs for the specific emails
print(f"\n  Log entries mentioning key emails:", flush=True)
for log in all_logs:
    if 'jack@gmail.com' in log or 'testmacros4412' in log:
        clean = log.encode('ascii', 'replace').decode('ascii')
        print(f"    {clean[:120]}", flush=True)

print(f"\n  === KEY TEST ===", flush=True)
print(f"  jack@gmail.com         → {jack_status or 'NOT FOUND'} (expected: Present/Valid)", flush=True)
print(f"  testmacros4412@gmail.com → {test_status or 'NOT FOUND'} (expected: Not Present/Invalid)", flush=True)

if jack_status == 'Present' and test_status == 'Not Present':
    print(f"\n  ✅✅✅ MACROS WORK! Both known emails returned correct results!", flush=True)
elif jack_status == 'Present' or test_status == 'Not Present':
    print(f"\n  ⚠️ PARTIAL — one correct", flush=True)
else:
    print(f"\n  ❌ Results not matching", flush=True)

print(f"{'='*60}", flush=True)
sio.disconnect()
