import cv2
import io
import os
import time
import threading
import requests
from flask import Flask, Response, render_template_string

# xXXXXXXXXXXXx
# CONFIG
# xXXXXXXXXXXXx
WEBHOOK_URL = "EXTERNAL WEBHOOK HERE!"
CAMERAS = [0, 1]  # multi-camera support
FPS = 20
SENSITIVITY = 25
MIN_CONTOUR_AREA = 1500
ALERT_COOLDOWN = 10
ALERT_FRAME_COUNT = 3
ALERT_FRAME_INTERVAL = 3
REQUEST_TIMEOUT = 15

# xXXXXXXXXXXXXXx
# PERSON DETECTOR (H.O.G.)
# xXXXXXXXXXXXXXx
hog = cv2.HOGDescriptor()
hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

app = Flask(__name__)
latest_frames = {}
event_log = []
camera_caps = {}
camera_locks = {}

# xXXXXXXXXXXXXXXx
# CAMERA UTILITIES
# xXXXXXXXXXXXXXXx

def init_camera(cam_id): 1
    cap = cv2.VideoCapture(cam_id)
    if not cap.isOpened():
        raise RuntimeError(f"Camera {cam_id} could not be opened.")
    cap.set(cv2.CAP_PROP_FPS, FPS)
    return cap


def safe_imencode(frame):
    if frame is None:
        return None, None
    return cv2.imencode(".jpg", frame)

# xXXXXXXXXXXXXXXXXXXXXXXXXXXx
# SEND ALERT PHOTOS TO DISCORD
# xXXXXXXXXXXXXXXXXXXXXXXXXXXx

def send_photos_to_discord(cam_id, capture, first_frame):
    frames = []

    if first_frame is not None:
        frames.append(first_frame.copy())

    for index in range(ALERT_FRAME_COUNT - len(frames)):
        time.sleep(ALERT_FRAME_INTERVAL)
        try:
            with camera_locks[cam_id]:
                ret, frame = capture.read()
        except Exception as exc:
            event_log.append(f"[Camera {cam_id}] failed to capture alert frame: {exc}")
            break

        if not ret or frame is None:
            event_log.append(f"[Camera {cam_id}] lost video feed while collecting alert frames.")
            break

        frames.append(frame.copy())

    if not frames:
        event_log.append(f"[Camera {cam_id}] no frames collected for alert.")
        return

    try:
        files = []
        for idx, frame in enumerate(frames):
            success, buffer = cv2.imencode('.jpg', frame)
            if not success:
                event_log.append(f"[Camera {cam_id}] failed to encode alert frame {idx}.")
                continue

            image_bytes = io.BytesIO(buffer.tobytes())
            files.append((f"file{idx}", (f"alert_cam{cam_id}_{idx}.jpg", image_bytes, "image/jpeg")))

        if not files:
            event_log.append(f"[Camera {cam_id}] no encoded alert images available.")
            return

        data = {
            "content": f"📸 **Person detected on camera {cam_id}**\nCaptured {len(files)} image(s) from the live feed."
        }

        response = requests.post(WEBHOOK_URL, data=data, files=files, timeout=REQUEST_TIMEOUT)
        if response.status_code >= 400:
            raise RuntimeError(f"Webhook returned status {response.status_code}: {response.text}")

        event_log.append(f"[Camera {cam_id}] alert sent with {len(files)} image(s).")
    except Exception as exc:
        event_log.append(f"[Camera {cam_id}] alert failed: {exc}")

# xXXXXXXXXXXXXx
# CAMERA THREAD
# xXXXXXXXXXXXXx

def camera_thread(cam_id):
    cap = None
    try:
        cap = init_camera(cam_id)
        camera_caps[cam_id] = cap
        camera_locks[cam_id] = threading.Lock()

        with camera_locks[cam_id]:
            ret, frame1 = cap.read()
            ret, frame2 = cap.read()

        if not ret or frame1 is None or frame2 is None:
            raise RuntimeError(f"Camera {cam_id} returned invalid initial frames.")

        last_event = 0

        while True:
            diff = cv2.absdiff(frame1, frame2)
            gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (5, 5), 0)
            _, thresh = cv2.threshold(blur, SENSITIVITY, 255, cv2.THRESH_BINARY)
            dilated = cv2.dilate(thresh, None, iterations=3)
            contours, _ = cv2.findContours(dilated, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

            motion = any(cv2.contourArea(c) > MIN_CONTOUR_AREA for c in contours)
            boxes, _ = hog.detectMultiScale(frame2, winStride=(8, 8))
            person_detected = len(boxes) > 0

            latest_frames[cam_id] = frame2.copy()

            if motion and person_detected and (time.time() - last_event > ALERT_COOLDOWN):
                last_event = time.time()
                saved_frame = frame2.copy()
                threading.Thread(
                    target=send_photos_to_discord,
                    args=(cam_id, cap, saved_frame),
                    daemon=True,
                ).start()

            frame1 = frame2
            with camera_locks[cam_id]:
                ret, frame2 = cap.read()

            if not ret or frame2 is None:
                event_log.append(f"[Camera {cam_id}] camera feed stopped.")
                break
    except Exception as exc:
        event_log.append(f"[Camera {cam_id}] thread error: {exc}")
    finally:
        if cap is not None:
            cap.release()

# xXXXXXXXXXXXXXx
# FLASK DASHBOARD
# xXXXXXXXXXXXXXx
TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<title>Dashboard</title>
<style>
body { background:#111; color:white; font-family:Arial; }
img { width:45%; margin:10px; border:2px solid #444; }
</style>
</head>
<body>
<h1>Security Dashboard</h1>

<h2>Live Cameras</h2>
{% for cam in cameras %}
    <h3>Camera {{cam}}</h3>
    <img src="/stream/{{cam}}">
{% endfor %}

<h2>Event Log</h2>
<ul>
{% for e in events %}
    <li>{{e}}</li>
{% endfor %}
</ul>

</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(TEMPLATE, cameras=CAMERAS, events=event_log)

@app.route("/stream/<int:cam_id>")
def stream(cam_id):
    def gen():
        while True:
            frame = latest_frames.get(cam_id)
            if frame is not None:
                success, jpeg = safe_imencode(frame)
                if success:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n")
            time.sleep(0.05)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


# LAUNCH APPLICATION

if __name__ == "__main__":
    for cam in CAMERAS:
        threading.Thread(target=camera_thread, args=(cam,), daemon=True).start()

    app.run(host="0.0.0.0", port=8080, debug=False)
