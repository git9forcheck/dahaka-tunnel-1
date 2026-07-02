"""Stop any running job and wait for it to clear."""
import socketio
import time
sio = socketio.Client()
stopped = False

@sio.on('job_stopped')
def on_stop():
    global stopped
    stopped = True
    print("Job stopped!")

@sio.on('update')
def on_update(data):
    print(f"Status: {data.get('status')} processed={data.get('processed')}/{data.get('total_emails')}")

sio.connect("http://127.0.0.1:5000")
print("Sending stop_job...")
sio.emit('stop_job')
time.sleep(5)
print("Done")
sio.disconnect()
