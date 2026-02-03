# DogWatch: Development Plan

## Raspberry Pi Zero 2 W + AI Camera (IMX500) Dog-on-Couch Detector

---

## 1. Project Summary

A privacy-first camera system that detects when your dog is on the couch, sends push notifications to your phone, plays an alert sound from the device, and serves a live "last seen" photo feed — all while **never storing frames that contain a human**.

### Core Behaviors

| Condition | Action |
|---|---|
| Dog detected, no human | Save frame, send push notification, play alert sound, update live feed |
| Dog + human detected | Discard frame immediately, no notification, no storage |
| Human only detected | Discard frame immediately |
| Nothing detected | Discard frame, continue monitoring |

---

## 2. Hardware Bill of Materials

| Item | Purpose | Est. Cost |
|---|---|---|
| Raspberry Pi Zero 2 W | Main compute (quad-core ARM Cortex-A53, 512MB RAM) | ~$15 |
| Raspberry Pi AI Camera (IMX500) | 12MP sensor + on-chip neural network accelerator | ~$70 |
| Mini camera ribbon cable | Zero 2 W uses a narrower CSI connector than Pi 4/5 — may need the included 22-pin-to-15-pin cable or a Zero-specific ribbon | included / ~$4 |
| MAX98357A I2S amplifier breakout | Mono I2S DAC + class-D amp, drives a speaker directly from GPIO; no USB port consumed | ~$6 |
| Small speaker (4Ω or 8Ω, 3W) | Plays the alert sound | ~$3–5 |
| MicroSD card (32GB+, A2 rated) | OS + model firmware + frame storage | ~$8 |
| USB-C power supply (5V 3A) | Stable power for Pi + camera + speaker | ~$10 |
| (Optional) Pi Zero case with camera cutout | Physical protection and mounting | ~$5–8 |
| (Optional) Standoffs / mounting bracket | Aim the camera at the couch | ~$5 |

**Total estimated cost: ~$115–125**

### Audio Hardware Detail: MAX98357A I2S Amp

The Pi Zero 2 W has **no analog audio output**. Your options are USB audio dongle (consumes the only USB data port), Bluetooth (adds latency and pairing complexity), or **I2S** (uses GPIO pins, zero CPU overhead, no USB port needed). The MAX98357A is the standard choice:

**Wiring (5 connections):**

| MAX98357A Pin | Pi Zero 2 W GPIO Pin |
|---|---|
| VIN | Pin 2 (5V) |
| GND | Pin 6 (GND) |
| BCLK | GPIO 18 (Pin 12) |
| LRC | GPIO 19 (Pin 35) |
| DIN | GPIO 21 (Pin 40) |

Leave the GAIN and SD pins unconnected for defaults (9dB gain, always on). Solder the speaker leads to the +/- pads on the breakout.

**Alternative:** The Waveshare WM8960 Audio HAT is a drop-on solution (no soldering) with onboard speaker terminal and I2S DAC, compatible with the Pi Zero 2 W. More expensive (~$15) but simpler.

**Note on GPIO conflict:** The AI Camera uses the CSI bus (GPIO 0–3 for I2C + dedicated camera data lines) and does NOT conflict with I2S audio pins (GPIO 18, 19, 21). Both can operate simultaneously.

---

## 3. Software Architecture

```
┌──────────────────────────────────────────────────┐
│              Raspberry Pi Zero 2 W               │
│                                                  │
│  ┌────────────┐   ┌───────────────────────────┐  │
│  │ IMX500 AI  │──▶│  detection_loop.py        │  │
│  │ Camera     │   │  (Main process)           │  │
│  │            │   │                           │  │
│  │ MobileNet  │   │  1. Get inference tensors │  │
│  │ SSD on-chip│   │  2. Parse: dog? human?    │  │
│  └────────────┘   │  3. Privacy gate          │  │
│                   │  4. Save frame / notify   │  │
│                   │  5. Play sound            │  │
│                   └──────┬────────────────────┘  │
│                          │                       │
│           ┌──────────────┼──────────────┐        │
│           ▼              ▼              ▼        │
│  ┌──────────────┐ ┌───────────┐ ┌────────────┐  │
│  │ ntfy.sh POST │ │ Audio out │ │ Flask web  │  │
│  │ (push notif) │ │ (aplay/   │ │ server     │  │
│  │              │ │  pygame)  │ │ :8080      │  │
│  └──────────────┘ └───────────┘ └────────────┘  │
│                                      │           │
└──────────────────────────────────────│───────────┘
                                       │
                              ┌────────▼────────┐
                              │  Your phone /   │
                              │  browser on LAN │
                              │  - ntfy app     │
                              │  - Live feed    │
                              └─────────────────┘
```

### Key Architectural Decisions

**Inference runs ON the camera, not the Pi.** This is the critical advantage of the IMX500 AI Camera. The Sony IMX500 sensor has an on-chip neural network accelerator that runs MobileNet SSD inference directly on the camera module. The Pi Zero 2 W only receives output tensors (bounding boxes + class IDs + confidence scores) — it never touches raw pixel data for inference. This means the Pi's CPU is nearly idle and free for saving frames, serving the web UI, and sending notifications.

**MobileNet SSD v2 (pre-packaged).** The AI Camera ships with this model pre-installed at `/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk`. It detects all 80 COCO classes in a single pass, including both "person" (class 0) and "dog" (class 16). No model training, conversion, or custom firmware required.

**ntfy.sh for push notifications.** A single HTTP POST sends a push notification to your phone. No account signup required for the free public server. Works on both iOS and Android via the ntfy app. Can optionally self-host later.

---

## 4. Development Phases

### Phase 1: Base System Setup (Day 1)

**Goal:** Pi Zero 2 W boots headless, connects to WiFi, AI Camera is recognized and running inference.

**Steps:**

1. Flash Raspberry Pi OS Lite (64-bit) to the SD card using Raspberry Pi Imager. Configure WiFi and enable SSH in the imager before writing.

2. Boot the Pi Zero 2 W with the AI Camera ribbon cable connected to the CSI port.

3. SSH in and run the full system update plus AI Camera firmware install:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-libcamera python3-kms++ libcap-dev
sudo apt install -y imx500-all
sudo reboot
```

Note: `imx500-all` installs firmware files, pre-packaged models, and the Picamera2 IMX500 integration. On the Zero 2 W this can take 10–15 minutes.

4. After reboot, verify the camera is detected:

```bash
rpicam-hello --list-cameras
```

You should see the IMX500 listed. If not, add `dtoverlay=imx500` to `/boot/firmware/config.txt` and reboot.

5. Test object detection with the pre-packaged model:

```bash
rpicam-hello -t 0s --post-process-file \
  /usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json
```

This won't display on headless, but confirms no errors. For visual confirmation, continue to Phase 2 with the web server.

**Troubleshooting:** Forum reports indicate some Zero 2 W units have issues with the camera probe. If you see `deferred probe pending` in `dmesg`, ensure you are using the 64-bit OS, the firmware is fully up to date, and try a different ribbon cable.


### Phase 2: Detection Logic with Privacy Gate (Day 1–2)

**Goal:** Python script that runs inference via the IMX500, parses results, and enforces the human-suppression privacy rule.

**Install Python dependencies:**

```bash
sudo apt install -y python3-opencv python3-munkres python3-flask
pip3 install --break-system-packages Pillow
```

**Core detection script: `dog_watch.py`**

```python
import time
import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from picamera2 import Picamera2
from picamera2.devices.imx500 import IMX500, NetworkIntrinsics
from PIL import Image
import numpy as np

# ── Config ──────────────────────────────────────────
MODEL_PATH = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"
LABELS_PATH = "coco_labels.txt"  # download from COCO or extract from picamera2 examples

PERSON_CLASS = "person"
DOG_CLASS = "dog"

DOG_CONFIDENCE_THRESHOLD = 0.50
PERSON_CONFIDENCE_THRESHOLD = 0.30  # Lower = more cautious for privacy

CAPTURE_INTERVAL = 1.0           # seconds between inference cycles
NOTIFY_COOLDOWN = 60             # minimum seconds between push notifications
MAX_KEPT_FRAMES = 20             # rolling buffer of dog frames

FRAME_DIR = Path("/home/pi/dog_watch/frames")
STATUS_FILE = Path("/home/pi/dog_watch/status.json")
ALERT_SOUND = Path("/home/pi/dog_watch/alert.wav")
NTFY_TOPIC = "my-dog-watch"      # change to a unique, hard-to-guess string
NTFY_SERVER = "https://ntfy.sh"  # or your self-hosted URL

# ── Setup ───────────────────────────────────────────
FRAME_DIR.mkdir(parents=True, exist_ok=True)
STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)

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
    buffer_count=4
)
imx500.show_network_fw_progress_bar()
picam2.start(config)

print("Camera started. Waiting for IMX500 firmware load...")
time.sleep(5)  # allow firmware to fully load on first run


def parse_detections(metadata):
    """Parse IMX500 output tensors into a list of (label, confidence, bbox)."""
    np_outputs = imx500.get_outputs(metadata, add_batch=True)
    if np_outputs is None:
        return []

    # MobileNet SSD post-processed output format:
    # [0] bboxes, [1] class_ids, [2] scores, [3] num_detections
    boxes = np_outputs[0][0]
    classes = np_outputs[1][0]
    scores = np_outputs[2][0]
    num = int(np_outputs[3][0])

    results = []
    for i in range(num):
        label = intrinsics.labels[int(classes[i])]
        score = float(scores[i])
        results.append((label, score, boxes[i]))
    return results


def analyze_frame(metadata):
    """Determine if dog and/or human are present."""
    detections = parse_detections(metadata)
    dog_detected = False
    dog_confidence = 0.0
    human_detected = False

    for label, score, bbox in detections:
        if label == DOG_CLASS and score >= DOG_CONFIDENCE_THRESHOLD:
            dog_detected = True
            dog_confidence = max(dog_confidence, score)
        if label == PERSON_CLASS and score >= PERSON_CONFIDENCE_THRESHOLD:
            human_detected = True

    return dog_detected, dog_confidence, human_detected


def send_notification(timestamp_str, confidence):
    """Send push notification via ntfy.sh."""
    try:
        message = f"Dog on couch detected at {timestamp_str} ({confidence:.0%} confidence)"
        subprocess.Popen([
            "curl", "-s",
            "-H", "Title: 🐕 Dog Alert!",
            "-H", "Priority: default",
            "-H", "Tags: dog",
            "-d", message,
            f"{NTFY_SERVER}/{NTFY_TOPIC}"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Notification error: {e}")


def play_alert():
    """Play alert sound via I2S speaker (non-blocking)."""
    try:
        subprocess.Popen(
            ["aplay", "-D", "plughw:0,0", str(ALERT_SOUND)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"Audio error: {e}")


def update_status(dog_present, human_present, last_dog_time=None):
    """Write JSON status for the web dashboard."""
    status = {
        "dog_detected": dog_present,
        "human_detected": human_present,
        "recording_active": dog_present and not human_present,
        "privacy_mode": human_present,
        "last_dog_seen": last_dog_time,
        "timestamp": datetime.now().isoformat(),
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2))


def prune_old_frames():
    """Keep only the most recent MAX_KEPT_FRAMES images."""
    frames = sorted(FRAME_DIR.glob("dog_*.jpg"))
    while len(frames) > MAX_KEPT_FRAMES:
        frames.pop(0).unlink()


# ── Main Loop ───────────────────────────────────────
last_notified = 0
last_dog_time = None

print("DogWatch running. Monitoring for dog on couch...")

while True:
    try:
        metadata = picam2.capture_metadata()
        dog_found, confidence, human_found = analyze_frame(metadata)

        if human_found:
            # PRIVACY MODE: do not capture or store anything
            update_status(dog_present=dog_found, human_present=True,
                          last_dog_time=last_dog_time)
            time.sleep(CAPTURE_INTERVAL)
            continue

        if dog_found:
            ts = datetime.now()
            timestamp_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            filename = f"dog_{ts.strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = FRAME_DIR / filename

            # Capture and save the actual image frame
            frame = picam2.capture_array()
            img = Image.fromarray(frame)
            img.save(filepath, quality=85)

            last_dog_time = timestamp_str
            prune_old_frames()

            # Send notification (with cooldown)
            now = time.time()
            if now - last_notified > NOTIFY_COOLDOWN:
                send_notification(timestamp_str, confidence)
                play_alert()
                last_notified = now

        update_status(dog_present=dog_found, human_present=False,
                      last_dog_time=last_dog_time)

    except Exception as e:
        print(f"Loop error: {e}")

    time.sleep(CAPTURE_INTERVAL)
```

**Important notes on this script:**

- The `imx500.get_outputs()` call retrieves the already-computed inference tensors from the camera hardware. The Pi is NOT running the neural network.
- The output tensor format may vary slightly depending on the model's post-processing pipeline (the `_pp` suffix means post-processed). You may need to adjust the index mapping in `parse_detections()` based on the actual tensor layout. Test with the picamera2 example scripts first to confirm the format.
- COCO labels file: Download from the picamera2 examples repo, or copy from `/usr/share/imx500-models/` if packaged. The "person" class is typically index 0 and "dog" is typically index 16 in COCO.


### Phase 3: Audio Alert (Day 2)

**Goal:** When a dog-only detection fires, play a sound from the attached speaker.

**I2S driver setup:**

```bash
# Add to /boot/firmware/config.txt:
dtoverlay=max98357a

# Reboot, then verify the sound card appears:
aplay -l
# Should show a "MAX98357A" or "hifiberry" device
```

**Create or obtain an alert sound:**

```bash
# Generate a simple 2-tone beep (no internet required):
sudo apt install -y sox
sox -n /home/pi/dog_watch/alert.wav \
    synth 0.3 sine 880 : synth 0.3 sine 1100 \
    vol 0.5
```

Or copy any short `.wav` file (dog bark sound effect, chime, etc.) to `/home/pi/dog_watch/alert.wav`.

**Test playback:**

```bash
aplay -D plughw:0,0 /home/pi/dog_watch/alert.wav
```

The `play_alert()` function in the detection script already handles this via `aplay` in a subprocess. It's non-blocking so it won't slow the detection loop.


### Phase 4: Push Notifications via ntfy (Day 2)

**Goal:** Receive push notifications on your phone when dog is detected.

**Phone setup (one-time):**

1. Install the **ntfy** app from the App Store (iOS) or Google Play (Android).
2. Open the app and subscribe to your topic name (e.g., `my-dog-watch`). Use a unique, hard-to-guess string since the free public server's topics are open.
3. That's it. Any POST to `https://ntfy.sh/my-dog-watch` will now push to your phone.

**Test from the Pi:**

```bash
curl -d "Test notification from DogWatch" https://ntfy.sh/my-dog-watch
```

You should see this arrive on your phone within seconds.

**Notification features supported by ntfy:**

- Priority levels (low, default, high, urgent)
- Emoji tags (rendered as icons in the notification)
- Image attachments (you could attach the dog frame thumbnail)
- Click actions (URL to open your live feed)

**For image attachments in notifications** (optional enhancement):

```python
subprocess.Popen([
    "curl", "-s",
    "-H", "Title: Dog Alert!",
    "-H", "Tags: dog",
    "-H", f"Click: http://<pi-ip>:8080",
    "-T", str(filepath),  # attach the image
    f"{NTFY_SERVER}/{NTFY_TOPIC}"
])
```

**iOS caveat:** On the free ntfy.sh server, iOS push notifications work via Apple's push service and arrive promptly. If you later self-host ntfy, iOS requires forwarding poll requests to ntfy.sh's upstream server for instant delivery — the ntfy docs cover this workaround.

**Privacy note:** When using the public ntfy.sh server, your topic name is effectively a password. Anyone who guesses it can subscribe. For true privacy, self-host ntfy (it runs fine in Docker on a home server or even on a separate Pi). Or use a long random topic name like `dogwatch-a7f3b2c9e1d4`.


### Phase 5: Live Feed Web Dashboard (Day 2–3)

**Goal:** A simple web page you can open on your phone/laptop that auto-refreshes showing the most recent dog-on-couch photos and system status.

**Flask web server: `web_server.py`**

```python
from flask import Flask, send_from_directory, jsonify, render_template_string
from pathlib import Path
import json

app = Flask(__name__)

FRAME_DIR = Path("/home/pi/dog_watch/frames")
STATUS_FILE = Path("/home/pi/dog_watch/status.json")

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
        .grid img { width: 100%; border-radius: 8px; }
        .timestamp { text-align: center; color: #888; font-size: 0.85em;
                     margin-top: 4px; }
        @media (min-width: 600px) {
            .grid { grid-template-columns: 1fr 1fr; }
        }
    </style>
</head>
<body>
    <h1>🐕 DogWatch</h1>
    <div id="status" class="status idle">Loading...</div>
    <div id="last-seen" class="last-seen"></div>
    <div id="grid" class="grid"></div>
    <script>
        async function refresh() {
            try {
                const status = await (await fetch('/api/status')).json();
                const el = document.getElementById('status');
                if (status.privacy_mode) {
                    el.textContent = '🔴 Privacy mode — person detected';
                    el.className = 'status privacy';
                } else if (status.dog_detected) {
                    el.textContent = '🟢 Dog on couch!';
                    el.className = 'status active';
                } else {
                    el.textContent = '⚪ Monitoring — no dog detected';
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
                    div.innerHTML = '<img src="/frames/' + f.name +
                        '?' + Date.now() + '"/>' +
                        '<div class="timestamp">' + f.time + '</div>';
                    grid.appendChild(div);
                });
            } catch (e) { console.error(e); }
        }
        refresh();
        setInterval(refresh, 3000);  // refresh every 3 seconds
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/status')
def api_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except:
        return {"dog_detected": False, "human_detected": False,
                "privacy_mode": False, "last_dog_seen": None}

@app.route('/api/frames')
def api_frames():
    frames = sorted(FRAME_DIR.glob("dog_*.jpg"), reverse=True)[:20]
    result = []
    for f in frames:
        # Extract timestamp from filename: dog_YYYYMMDD_HHMMSS.jpg
        parts = f.stem.replace("dog_", "").split("_")
        if len(parts) == 2:
            time_str = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]} {parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
        else:
            time_str = ""
        result.append({"name": f.name, "time": time_str})
    return jsonify(result)

@app.route('/frames/<path:filename>')
def serve_frame(filename):
    return send_from_directory(FRAME_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True)
```

Run as a background thread from the main script, or as a separate systemd service.


### Phase 6: System Service & Boot Setup (Day 3)

**Goal:** Everything starts automatically on boot and restarts on crash.

**Create systemd service for the detector: `/etc/systemd/system/dogwatch.service`**

```ini
[Unit]
Description=DogWatch Dog Detection Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/dog_watch
ExecStart=/usr/bin/python3 /home/pi/dog_watch/dog_watch.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

**Create systemd service for the web server: `/etc/systemd/system/dogwatch-web.service`**

```ini
[Unit]
Description=DogWatch Web Dashboard
After=network-online.target dogwatch.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/dog_watch
ExecStart=/usr/bin/python3 /home/pi/dog_watch/web_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Enable and start:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable dogwatch dogwatch-web
sudo systemctl start dogwatch dogwatch-web
```

**Add a swap file** (insurance against OOM kills on 512MB RAM):

```bash
sudo fallocate -l 256M /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 5. Privacy Design

The system is designed to be privacy-first at every layer:

- **No human frames ever reach disk.** The privacy gate runs in RAM before any file I/O. If a human is detected, the frame array is discarded and execution continues to the next cycle.

- **Lower detection threshold for humans (0.30) than dogs (0.50).** A partial limb or blurry figure at the edge of frame may score low confidence. Erring on the side of caution means more false suppression, but never a false recording.

- **No video, only stills.** The system captures individual JPEG frames at ~1 FPS, not video streams. There is no continuous recording buffer.

- **Rolling frame limit.** Only the last 20 dog-only frames are kept. Older frames are automatically deleted.

- **Status file, not image, for privacy events.** When a human is detected, only a boolean JSON flag is written — no image data, no thumbnails, no metadata about the human.

- **Protects all people.** Guests, family members, delivery workers visible through a window — anyone triggers suppression.

---

## 6. Performance Budget on Pi Zero 2 W

| Task | Resource | Notes |
|---|---|---|
| Neural network inference | IMX500 chip (not Pi CPU) | ~30 FPS capable; we use ~1 FPS |
| Tensor parsing in Python | Pi CPU: ~5ms | Trivial — just reading arrays |
| Frame capture + JPEG encode | Pi CPU: ~100–200ms | PIL save at 640×480 |
| ntfy curl POST | Pi CPU: ~50ms | Non-blocking subprocess |
| aplay sound | Pi CPU: negligible | DMA-driven I2S, subprocess |
| Flask web server | Pi CPU: ~10–20ms/request | Only serves static files + JSON |
| **Total per cycle** | **~300–400ms** | **Fits easily in 1-second interval** |
| RAM usage (est.) | ~200–250MB | OS (~100) + Python (~80) + Picamera2 (~50) |

The 512MB RAM is tight but workable. Avoid importing heavy libraries unnecessarily. Do NOT install or import OpenCV for the main detection script if you can avoid it — use PIL instead for image saving. The web server is lightweight Flask. If RAM becomes an issue, combine both scripts into one process to avoid duplicate Python interpreter overhead.

---

## 7. File Structure

```
/home/pi/dog_watch/
├── dog_watch.py          # Main detection loop
├── web_server.py         # Flask dashboard server
├── coco_labels.txt       # COCO class label names
├── alert.wav             # Alert sound file
├── status.json           # Current system state (auto-generated)
└── frames/               # Rolling buffer of dog-only frames
    ├── dog_20260131_143022.jpg
    ├── dog_20260131_143025.jpg
    └── ...
```

---

## 8. Configuration Tuning Guide

After initial deployment, you'll likely want to adjust these based on real-world performance:

| Parameter | Default | Adjust if... |
|---|---|---|
| `DOG_CONFIDENCE_THRESHOLD` | 0.50 | Too many false positives → raise; missing real detections → lower |
| `PERSON_CONFIDENCE_THRESHOLD` | 0.30 | Being recorded despite being in frame → lower to 0.20; too many false privacy triggers → raise |
| `NOTIFY_COOLDOWN` | 60s | Too many notifications → raise to 300; want faster alerts → lower to 30 |
| `CAPTURE_INTERVAL` | 1.0s | Want smoother feed → lower to 0.5; want less SD wear → raise to 2.0 |
| `MAX_KEPT_FRAMES` | 20 | Want more history → raise to 50; tight on SD space → lower to 10 |

**Camera positioning tip:** Mount the camera with the couch filling most of the frame. The closer the dog is to the camera's center and the larger it appears in frame, the higher the detection confidence will be. MobileNet SSD works best when objects occupy a reasonable portion of the 320×320 input resolution.

---

## 9. Future Enhancements (Optional)

- **ntfy image attachments:** Attach the dog frame JPEG directly to the push notification so you see the photo without opening the web dashboard.
- **Home Assistant integration:** POST to an HA webhook to trigger automations (turn on lights, send TTS announcement through smart speakers, etc.).
- **Custom fine-tuned model:** Use Sony's AITRIOS / Edge MDT toolchain to fine-tune a model specifically for "your dog on your couch" — higher accuracy, fewer false positives.
- **SD card longevity:** Write frames to a tmpfs ramdisk and sync to SD periodically, or use an external USB drive.
- **Tailscale / WireGuard:** Access the live feed dashboard from outside your home network without port forwarding.

---

## 10. Revision Notes (Post-Review)

The following changes were made to the implementation based on code review:

### Critical Fix: Privacy Race Condition
The original plan used `capture_metadata()` then `capture_array()` as separate calls, which could return data from different frames. Replaced with `capture_request()` which returns both metadata and image buffer from the same atomic capture, closing a privacy hole where a human could appear in a saved frame despite the privacy gate passing.

### Architectural Changes
1. **Merged into single process** — `web_server.py` eliminated; Flask runs in a daemon thread inside `dog_watch.py`. Saves ~80MB RAM (critical on 512MB Pi Zero 2 W).
2. **Atomic status file writes** — Uses write-to-temp + `os.rename()` to prevent Flask from reading half-written JSON.
3. **Multiple-dog detection** — `analyze_frame()` returns dog count, not just boolean. Notifications report "2 dogs on couch" etc.
4. **Region-of-interest filtering** — Configurable `COUCH_ROI` bounding box so dogs on the floor near the couch don't trigger alerts.
5. **Health heartbeat** — Periodic ntfy POST to a separate topic so you know if the system goes down.
6. **Structured logging** — `logging` module replaces `print()` for better debugging via `journalctl`.
7. **urllib replaces curl** — Notifications use `urllib.request` (stdlib) instead of spawning `curl` subprocesses.
8. **Audio device verification** — Startup check for the I2S device with configurable `AUDIO_DEVICE` constant.
9. **XSS prevention** — Dashboard uses `encodeURIComponent()` for frame filenames and DOM methods instead of `innerHTML` for user-facing content.
10. **Reduced SD writes** — JPEG quality lowered to 75, `MAX_KEPT_FRAMES` lowered to 10.

### Updated File Structure
```
/home/pi/dog_watch/
├── dog_watch.py          # Main detection loop + Flask dashboard (single process)
├── coco_labels.txt       # COCO class label names (80 classes)
├── alert.wav             # Alert sound file
├── status.json           # Current system state (auto-generated)
└── frames/               # Rolling buffer of dog-only frames
    ├── dog_20260131_143022.jpg
    └── ...
```

### Updated Systemd Setup
Only one service file needed: `dogwatch.service` (no separate web service).

```bash
sudo cp dogwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dogwatch
sudo systemctl start dogwatch
```