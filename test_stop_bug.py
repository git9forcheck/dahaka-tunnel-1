"""Reproduce the stop-job bug: start a job with 20 fake emails, wait 2 min, stop, observe behavior."""
import socketio
import requests
import time
import json

sio = socketio.Client()
job_status = {'state': 'unknown', 'stopped': False, 'updates': []}

@sio.on('update')
def on_update(data):
    status = data.get('status', '?')
    processed = data.get('processed', 0)
    total = data.get('total_emails', 0)
    success = data.get('success', 0)
    failure = data.get('failure', 0)
    bt = data.get('browser_threads', {})
    uptime = data.get('uptime', 0)
    job_status['state'] = status
    print(f"  [UPDATE] status={status} processed={processed}/{total} success={success} fail={failure} threads={len(bt)} uptime={uptime:.0f}s")

@sio.on('check_started')
def on_started(data):
    print(f"  [JOB STARTED] job_id={data.get('job_id')}")

@sio.on('job_stopped')
def on_stopped(data=None):
    job_status['stopped'] = True
    print("  [JOB_STOPPED EVENT RECEIVED]")

@sio.on('check_finished')
def on_finished(data=None):
    print(f"  [JOB FINISHED] {data}")

@sio.on('error')
def on_error(data):
    print(f"  [ERROR] {data}")

# Connect
print("Connecting to server...")
sio.connect("http://127.0.0.1:5000")
print("Connected.")

# Get domains
r = requests.get('http://127.0.0.1:5000/api/domains')
domains = r.json()
print(f"Available domains: {json.dumps(domains, indent=2)}")

if not domains:
    print("ERROR: No domains configured!")
    sio.disconnect()
    exit(1)

domain_id = domains[0]['id']
print(f"Using domain_id={domain_id} ({domains[0].get('name', '?')})")

# 20 fake emails
emails = "\n".join([f"faketest{i}@gmail.com" for i in range(1, 21)])
print(f"Emails:\n{emails}\n")

# Start job
print("=" * 60)
print("STARTING JOB...")
print("=" * 60)
r = requests.post('http://127.0.0.1:5000/api/job/start', json={'domain_id': domain_id, 'emails': emails})
print(f"Start response: {r.status_code} {r.text[:200]}")

# Wait 2 minutes
print("\nWaiting 2 minutes before stopping...")
for i in range(120):
    time.sleep(1)
    if i % 10 == 0:
        print(f"  ... {i}s / 120s elapsed ...")

# Stop the job
print("\n" + "=" * 60)
print("STOPPING JOB...")
print("=" * 60)
job_status['stopped'] = False
sio.emit('stop_job')
print("stop_job event emitted")

# Wait and observe
print("\nObserving for 30 seconds after stop...")
for i in range(30):
    time.sleep(1)
    if i % 5 == 0:
        print(f"  ... {i}s after stop, stopped_event_received={job_status['stopped']}, state={job_status['state']}")

print("\n" + "=" * 60)
print(f"FINAL STATE: stopped={job_status['stopped']}, state={job_status['state']}")
print("=" * 60)

sio.disconnect()
print("Done.")
