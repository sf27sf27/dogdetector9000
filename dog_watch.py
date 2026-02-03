"""DogWatch: Privacy-first dog-on-couch detector for Raspberry Pi Zero 2 W + IMX500 AI Camera.

Detects dogs on the couch via on-chip MobileNet SSD inference, sends push notifications,
plays an alert sound, and serves a live photo dashboard. Never stores frames containing humans.
"""

import json
import logging
import os
import time
import threading
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

from picamera2 import Picamera2
from picamera2.devices.imx500 import IMX500, NetworkIntrinsics
from PIL import Image
import numpy as np
from flask import Flask, send_from_directory, jsonify, render_template_string

# ── Logging ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dogwatch")

# ── Config ───────────────────────────────────────────
MODEL_PATH = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"
BASE_DIR = Path(__file__).parent
LABELS_PATH = BASE_DIR / "coco_labels.txt"

PERSON_CLASS = "person"
DOG_CLASS = "dog"

DOG_CONFIDENCE_THRESHOLD = 0.50
PERSON_CONFIDENCE_THRESHOLD = 0.30  # Lower = more cautious for privacy

CAPTURE_INTERVAL = 1.0       # seconds between inference cycles
NOTIFY_COOLDOWN = 60         # minimum seconds between push notifications
MAX_KEPT_FRAMES = 10         # rolling buffer of dog frames
JPEG_QUALITY = 75            # lower than 85 to reduce SD writes, negligible visual diff at 640x480

FRAME_DIR = BASE_DIR / "frames"
STATUS_FILE = BASE_DIR / "status.json"
ALERT_SOUND = BASE_DIR / "alert.wav"

NTFY_TOPIC = "dogwatch-770291bdb79df5f2"
NTFY_HEALTH_TOPIC = "dogwatch-health-e790a2780c99c782"
NTFY_SERVER = "https://ntfy.sh"

AUDIO_ENABLED = False        # set to True once I2S amp is installed
AUDIO_DEVICE = "plughw:0,0"  # adjust based on `aplay -l` output

# Region of interest: only alert when dog bbox overlaps this zone.
# Coordinates as fractions of frame dimensions (x1, y1, x2, y2).
# Set to (0.0, 0.0, 1.0, 1.0) to disable ROI filtering (entire frame).
COUCH_ROI = (0.0, 0.0, 1.0, 1.0)
ROI_OVERLAP_THRESHOLD = 0.5  # fraction of dog bbox that must overlap ROI

HEALTH_HEARTBEAT_INTERVAL = 1800  # seconds (30 minutes)

WEB_PORT = 8080

# ── Setup ────────────────────────────────────────────
FRAME_DIR.mkdir(parents=True, exist_ok=True)
STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)


def verify_audio_device():
    """Check that the configured audio device exists. Disable audio if not found."""
    global AUDIO_ENABLED
    if not AUDIO_ENABLED:
        log.info("Audio disabled via config (AUDIO_ENABLED=False)")
        return
    try:
        result = subprocess.run(
            ["aplay", "-l"], capture_output=True, text=True, timeout=5
        )
        if "card" not in result.stdout.lower():
            log.warning("No audio devices found via aplay -l. Disabling audio alerts.")
            AUDIO_ENABLED = False
        else:
            log.info("Audio devices found:\n%s", result.stdout.strip())
    except Exception as e:
        log.warning("Could not verify audio device: %s. Disabling audio alerts.", e)
        AUDIO_ENABLED = False


def bbox_overlap_fraction(dog_bbox, roi):
    """Calculate what fraction of the dog bbox overlaps with the ROI.

    Both are (x1, y1, x2, y2) tuples, normalized to [0, 1].
    Returns a float in [0, 1].
    """
    dx1, dy1, dx2, dy2 = dog_bbox
    rx1, ry1, rx2, ry2 = roi

    # Intersection
    ix1 = max(dx1, rx1)
    iy1 = max(dy1, ry1)
    ix2 = min(dx2, rx2)
    iy2 = min(dy2, ry2)

    if ix1 >= ix2 or iy1 >= iy2:
        return 0.0

    intersection = (ix2 - ix1) * (iy2 - iy1)
    dog_area = (dx2 - dx1) * (dy2 - dy1)

    if dog_area <= 0:
        return 0.0

    return intersection / dog_area


def parse_detections(imx500, intrinsics, metadata):
    """Parse IMX500 output tensors into a list of (label, confidence, bbox)."""
    np_outputs = imx500.get_outputs(metadata, add_batch=True)
    if np_outputs is None:
        log.info("No inference output yet (firmware may still be loading)")
        return []

    # MobileNet SSD post-processed output format:
    # [0] bboxes (normalized), [1] class_ids, [2] scores, [3] num_detections
    boxes = np_outputs[0][0]
    classes = np_outputs[1][0]
    scores = np_outputs[2][0]
    num = int(np_outputs[3][0])

    results = []
    for i in range(num):
        class_id = int(classes[i])
        label = intrinsics.labels[class_id] if class_id < len(intrinsics.labels) else f"unknown({class_id})"
        score = float(scores[i])
        bbox = tuple(float(v) for v in boxes[i])
        log.info("  Detection: class_id=%d label=%r score=%.2f bbox=%s", class_id, label, score, bbox)
        results.append((label, score, bbox))
    return results


def analyze_frame(imx500, intrinsics, metadata):
    """Determine if dogs and/or humans are present.

    Returns:
        dog_count: number of dogs detected in the ROI
        max_dog_confidence: highest confidence among detected dogs
        human_detected: whether any human was detected
    """
    detections = parse_detections(imx500, intrinsics, metadata)
    dog_count = 0
    max_dog_confidence = 0.0
    human_detected = False

    for label, score, bbox in detections:
        if label == PERSON_CLASS and score >= PERSON_CONFIDENCE_THRESHOLD:
            human_detected = True

        if label == DOG_CLASS and score >= DOG_CONFIDENCE_THRESHOLD:
            # Check if this dog overlaps with the couch ROI
            overlap = bbox_overlap_fraction(bbox, COUCH_ROI)
            if overlap >= ROI_OVERLAP_THRESHOLD:
                dog_count += 1
                max_dog_confidence = max(max_dog_confidence, score)

    return dog_count, max_dog_confidence, human_detected


def send_notification(timestamp_str, confidence, dog_count):
    """Send push notification via ntfy.sh using urllib (no subprocess)."""
    try:
        dogs_word = "dog" if dog_count == 1 else "dogs"
        message = (
            f"{dog_count} {dogs_word} on couch detected at {timestamp_str} "
            f"({confidence:.0%} confidence)"
        )
        data = message.encode("utf-8")
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=data,
            headers={
                "Title": "Dog Alert!",
                "Priority": "default",
                "Tags": "dog",
            },
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.error("Notification error: %s", e)


def send_heartbeat():
    """Send a low-priority health heartbeat via ntfy."""
    try:
        data = f"DogWatch running as of {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".encode("utf-8")
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_HEALTH_TOPIC}",
            data=data,
            headers={
                "Title": "DogWatch Heartbeat",
                "Priority": "low",
                "Tags": "heartbeat",
            },
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Health heartbeat sent")
    except Exception as e:
        log.error("Heartbeat error: %s", e)


def play_alert():
    """Play alert sound via I2S speaker (non-blocking). Skips silently if audio is disabled."""
    if not AUDIO_ENABLED:
        return
    if not ALERT_SOUND.exists():
        log.warning("Alert sound file not found: %s", ALERT_SOUND)
        return
    try:
        subprocess.Popen(
            ["aplay", "-D", AUDIO_DEVICE, str(ALERT_SOUND)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.error("Audio error: %s", e)


def update_status(dog_count, human_present, last_dog_time=None):
    """Write JSON status for the web dashboard (atomic write via rename)."""
    status = {
        "dog_detected": dog_count > 0,
        "dog_count": dog_count,
        "human_detected": human_present,
        "recording_active": dog_count > 0 and not human_present,
        "privacy_mode": human_present,
        "last_dog_seen": last_dog_time,
        "timestamp": datetime.now().isoformat(),
    }
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2))
    os.rename(tmp, STATUS_FILE)


def prune_old_frames():
    """Keep only the most recent MAX_KEPT_FRAMES images."""
    frames = sorted(FRAME_DIR.glob("dog_*.jpg"))
    while len(frames) > MAX_KEPT_FRAMES:
        frames.pop(0).unlink()


# ── Flask Web Dashboard ─────────────────────────────
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>DogWatch Live Feed</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, system-ui, sans-serif;
               background: #1a1a2e; color: #eee; padding: 16px; }
        h1 { text-align: center; margin-bottom: 8px; font-size: 1.4em; }
        .status { text-align: center; padding: 10px; border-radius: 8px;
                  margin-bottom: 16px; font-weight: bold; }
        .status.active { background: #2d6a4f; }
        .status.privacy { background: #d63031; }
        .status.idle { background: #636e72; }
        .last-seen { text-align: center; color: #aaa; margin-bottom: 16px; }
        .grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
        .grid img { width: 100%%; border-radius: 8px; }
        .timestamp { text-align: center; color: #888; font-size: 0.85em;
                     margin-top: 4px; }
        @media (min-width: 600px) {
            .grid { grid-template-columns: 1fr 1fr; }
        }
    </style>
</head>
<body>
    <h1>DogWatch</h1>
    <div id="status" class="status idle">Loading...</div>
    <div id="last-seen" class="last-seen"></div>
    <div id="grid" class="grid"></div>
    <script>
        async function refresh() {
            try {
                const status = await (await fetch('/api/status')).json();
                const el = document.getElementById('status');
                if (status.privacy_mode) {
                    el.textContent = 'Privacy mode - person detected';
                    el.className = 'status privacy';
                } else if (status.dog_detected) {
                    const count = status.dog_count || 1;
                    const word = count === 1 ? 'dog' : 'dogs';
                    el.textContent = count + ' ' + word + ' on couch!';
                    el.className = 'status active';
                } else {
                    el.textContent = 'Monitoring - no dog detected';
                    el.className = 'status idle';
                }
                const ls = document.getElementById('last-seen');
                ls.textContent = status.last_dog_seen
                    ? 'Last seen: ' + status.last_dog_seen
                    : 'No dog sightings yet';

                const frames = await (await fetch('/api/frames')).json();
                const grid = document.getElementById('grid');
                grid.innerHTML = '';
                frames.forEach(f => {
                    const div = document.createElement('div');
                    const img = document.createElement('img');
                    img.src = '/frames/' + encodeURIComponent(f.name) + '?' + Date.now();
                    img.alt = 'Dog detected at ' + f.time;
                    const ts = document.createElement('div');
                    ts.className = 'timestamp';
                    ts.textContent = f.time;
                    div.appendChild(img);
                    div.appendChild(ts);
                    grid.appendChild(div);
                });
            } catch (e) { console.error(e); }
        }
        refresh();
        setInterval(refresh, 3000);
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except FileNotFoundError:
        return {"dog_detected": False, "dog_count": 0, "human_detected": False,
                "privacy_mode": False, "last_dog_seen": None}
    except Exception as e:
        log.error("Error reading status file: %s", e)
        return {"error": "Failed to read status"}, 500


@app.route("/api/frames")
def api_frames():
    frames = sorted(FRAME_DIR.glob("dog_*.jpg"), reverse=True)[:MAX_KEPT_FRAMES]
    result = []
    for f in frames:
        parts = f.stem.replace("dog_", "").split("_")
        if len(parts) == 2:
            time_str = (
                f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]} "
                f"{parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
            )
        else:
            time_str = ""
        result.append({"name": f.name, "time": time_str})
    return jsonify(result)


@app.route("/frames/<path:filename>")
def serve_frame(filename):
    return send_from_directory(FRAME_DIR, filename)


def start_web_server():
    """Run Flask in a daemon thread so it doesn't block the main detection loop."""
    log.info("Starting web dashboard on port %d", WEB_PORT)
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, use_reloader=False)


# ── Main ─────────────────────────────────────────────
def main():
    log.info("Initializing DogWatch...")

    verify_audio_device()

    # Load the model onto the IMX500
    imx500 = IMX500(MODEL_PATH)
    intrinsics = imx500.network_intrinsics or NetworkIntrinsics()
    intrinsics.task = "object detection"

    # Load COCO labels
    if intrinsics.labels is None:
        with open(LABELS_PATH, "r") as f:
            intrinsics.labels = f.read().splitlines()
    intrinsics.update_with_defaults()

    # Initialize camera
    picam2 = Picamera2(imx500.camera_num)
    config = picam2.create_still_configuration(
        main={"size": (640, 480)},
        buffer_count=4,
    )
    imx500.show_network_fw_progress_bar()
    picam2.start(config)

    log.info("Camera started. Waiting for IMX500 firmware load...")
    time.sleep(5)

    # Start Flask web server in background thread
    web_thread = threading.Thread(target=start_web_server, daemon=True)
    web_thread.start()

    # Detection loop state
    last_notified = 0
    last_dog_time = None
    last_heartbeat = time.time()

    # Send initial heartbeat
    threading.Thread(target=send_heartbeat, daemon=True).start()

    log.info("DogWatch running. Monitoring for dogs on couch...")

    while True:
        try:
            # Use capture_request() for atomic metadata + image from the SAME frame.
            # This fixes the privacy race condition where capture_metadata() and
            # capture_array() could return data from different frames.
            request = picam2.capture_request()
            try:
                metadata = request.get_metadata()
                dog_count, confidence, human_found = analyze_frame(
                    imx500, intrinsics, metadata
                )

                if human_found:
                    # PRIVACY MODE: do not capture or store anything
                    update_status(
                        dog_count=dog_count, human_present=True,
                        last_dog_time=last_dog_time
                    )
                    log.debug("Human detected — frame discarded (privacy mode)")
                    time.sleep(CAPTURE_INTERVAL)
                    continue

                if dog_count > 0:
                    ts = datetime.now()
                    timestamp_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                    filename = f"dog_{ts.strftime('%Y%m%d_%H%M%S')}.jpg"
                    filepath = FRAME_DIR / filename

                    # Get the image from the SAME request (same frame as inference)
                    frame = request.make_array("main")
                    img = Image.fromarray(frame)
                    img.save(filepath, quality=JPEG_QUALITY)

                    last_dog_time = timestamp_str
                    prune_old_frames()

                    dogs_word = "dog" if dog_count == 1 else "dogs"
                    log.info(
                        "%d %s detected (%.0f%% confidence) — frame saved: %s",
                        dog_count, dogs_word, confidence * 100, filename,
                    )

                    # Send notification (with cooldown)
                    now = time.time()
                    if now - last_notified > NOTIFY_COOLDOWN:
                        threading.Thread(
                            target=send_notification,
                            args=(timestamp_str, confidence, dog_count),
                            daemon=True,
                        ).start()
                        play_alert()
                        last_notified = now

                update_status(
                    dog_count=dog_count, human_present=False,
                    last_dog_time=last_dog_time,
                )
            finally:
                request.release()

            # Periodic health heartbeat
            now = time.time()
            if now - last_heartbeat > HEALTH_HEARTBEAT_INTERVAL:
                threading.Thread(target=send_heartbeat, daemon=True).start()
                last_heartbeat = now

        except Exception as e:
            log.error("Loop error: %s", e, exc_info=True)

        time.sleep(CAPTURE_INTERVAL)


if __name__ == "__main__":
    main()
