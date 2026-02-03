# DogWatch Testing Guide

## 1. Deploy to Raspberry Pi

### On your Mac (push code to GitHub)

```bash
cd /Users/sydneywatson/dogdetector9000
git init
git remote add origin git@github.com:sf27sf27/dogdetector9000.git
git add dog_watch.py coco_labels.txt dogwatch.service TESTING.md initial_plan.md
git commit -m "Initial DogWatch implementation"
git branch -M main
git push -u origin main
```

### On the Pi (SSH in and pull)

```bash
ssh pi@<pi-ip>

# Install git if not present
sudo apt install -y git

# Clone the repo
git clone https://github.com/sf27sf27/dogdetector9000.git /home/pi/dog_watch

# Install Python dependencies
sudo apt install -y python3-libcamera python3-kms++ libcap-dev imx500-all
sudo apt install -y python3-flask python3-pil python3-numpy
```

After `imx500-all` finishes, reboot:

```bash
sudo reboot
```

SSH back in after reboot.

---

## 2. Pre-Flight Hardware Checks

Run these one at a time to verify each subsystem before starting the full script.

### 2a. Camera detection

```bash
rpicam-hello --list-cameras
```

**Expected:** Output lists the IMX500 sensor. If not found, check:

```bash
# Verify the overlay is loaded
dmesg | grep imx500

# If needed, add to /boot/firmware/config.txt:
echo "dtoverlay=imx500" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

### 2b. Camera inference test

```bash
rpicam-hello -t 10s --post-process-file \
  /usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json 2>&1
```

**Expected:** Runs for 10 seconds with no errors. On headless Pi there's no visual output, but absence of errors confirms the model firmware loaded and inference ran.

### 2c. Network / ntfy test

```bash
# Test that the Pi can reach ntfy.sh
curl -d "DogWatch test notification" https://ntfy.sh/dogwatch-770291bdb79df5f2
```

**Expected:** You receive a push notification on your phone (subscribe to `dogwatch-770291bdb79df5f2` in the ntfy app first).

### 2d. Verify COCO labels file

```bash
head -20 /home/pi/dog_watch/coco_labels.txt
```

**Expected:** First line is `person`, line 17 is `dog`. Verify with:

```bash
sed -n '1p' /home/pi/dog_watch/coco_labels.txt   # should print: person
sed -n '17p' /home/pi/dog_watch/coco_labels.txt   # should print: dog
```

### 2e. Memory baseline

```bash
free -m
```

**Expected:** At least 300MB free before starting DogWatch. If less, check for other running services consuming RAM.

---

## 3. Run DogWatch Directly (Interactive Mode)

Run the script directly (not via systemd) so you can see log output in real time.

```bash
cd /home/pi/dog_watch
python3 dog_watch.py
```

**Expected startup output:**

```
2026-XX-XX [INFO] Initializing DogWatch...
2026-XX-XX [INFO] Audio disabled via config (AUDIO_ENABLED=False)
2026-XX-XX [INFO] Camera started. Waiting for IMX500 firmware load...
2026-XX-XX [INFO] Starting web dashboard on port 8080
2026-XX-XX [INFO] Health heartbeat sent
2026-XX-XX [INFO] DogWatch running. Monitoring for dogs on couch...
```

If you see import errors, install the missing package and retry.

Leave this running in the SSH session for the following tests. Open a second SSH session for commands.

---

## 4. Test Detection Logic

### 4a. Dog detection (trigger an alert)

Hold your phone in front of the camera showing a clear photo of a dog. Wait 2-3 seconds.

**Check logs (first SSH session):**

```
2026-XX-XX [INFO] 1 dog detected (XX% confidence) — frame saved: dog_XXXXXXXX_XXXXXX.jpg
```

**Verify frame was saved:**

```bash
ls -la /home/pi/dog_watch/frames/
```

**Verify notification arrived** on your phone via the ntfy app.

### 4b. Multi-dog detection

Show a photo containing two or more dogs.

**Expected log:**

```
2026-XX-XX [INFO] 2 dogs detected (XX% confidence) — frame saved: dog_XXXXXXXX_XXXXXX.jpg
```

**Expected notification:** "2 dogs on couch detected at ..."

### 4c. Privacy gate — human suppression

Walk in front of the camera yourself (or show a photo of a person).

**Expected log:**

```
2026-XX-XX [DEBUG] Human detected — frame discarded (privacy mode)
```

Note: Debug-level messages are hidden by default. To see them, temporarily change the log level in the script to `DEBUG`, or check that no new frame files appear in the frames directory:

```bash
# Before walking in front of camera, note the frame count:
ls /home/pi/dog_watch/frames/ | wc -l

# Walk in front of camera, wait 5 seconds, check again:
ls /home/pi/dog_watch/frames/ | wc -l
```

**Expected:** Frame count does NOT increase while a person is visible.

### 4d. Privacy gate — dog + human together

Show the camera a scene with both a dog and a person visible simultaneously.

**Expected:** No frame saved, no notification sent. The privacy gate takes precedence.

### 4e. Notification cooldown

Trigger two dog detections less than 60 seconds apart.

**Expected:** First detection sends a notification. Second detection saves a frame but does NOT send a notification (cooldown). Check logs for the frame save without a corresponding notification.

---

## 5. Test Web Dashboard

### 5a. Access the dashboard

From any device on the same network, open a browser to:

```
http://<pi-ip>:8080
```

**Expected:** DogWatch dashboard loads showing current status and any saved frames.

### 5b. Status display

| Scenario | Expected dashboard status |
|---|---|
| No detection | "Monitoring - no dog detected" (gray) |
| Dog detected | "1 dog on couch!" (green) |
| Person detected | "Privacy mode - person detected" (red) |

### 5c. Auto-refresh

Leave the dashboard open. Trigger a dog detection. Within 3-6 seconds, the dashboard should update to show the new frame without manual refresh.

### 5d. API endpoints

```bash
# From the second SSH session:
curl -s http://localhost:8080/api/status | python3 -m json.tool
curl -s http://localhost:8080/api/frames | python3 -m json.tool
```

**Expected:** Valid JSON responses. Status should include `dog_count`, `privacy_mode`, `last_dog_seen` fields.

---

## 6. Test Frame Management

### 6a. Rolling frame limit

Trigger more than 10 dog detections (the `MAX_KEPT_FRAMES` limit).

```bash
ls /home/pi/dog_watch/frames/ | wc -l
```

**Expected:** Never more than 10 frames. Oldest frames are deleted automatically.

### 6b. Atomic status writes

While the script is running, rapidly read the status file:

```bash
for i in $(seq 1 100); do
  python3 -c "import json; json.load(open('/home/pi/dog_watch/status.json'))" 2>&1
done
```

**Expected:** No JSON decode errors. Every read should return valid JSON.

---

## 7. Test Health Heartbeat

The heartbeat fires every 30 minutes by default. For testing, you can temporarily change `HEALTH_HEARTBEAT_INTERVAL` to `60` (1 minute) in the script:

```bash
# Edit on the Pi temporarily
nano /home/pi/dog_watch/dog_watch.py
# Change HEALTH_HEARTBEAT_INTERVAL = 1800 to 60
```

Subscribe to `dogwatch-health-e790a2780c99c782` in the ntfy app. Wait ~1 minute.

**Expected:** Low-priority "DogWatch Heartbeat" notification arrives.

Revert the interval back to `1800` after testing.

---

## 8. Test Systemd Service

Once all interactive tests pass, set up the service for unattended operation.

```bash
# Stop the interactive session first (Ctrl+C in the first SSH session)

# Install the service
sudo cp /home/pi/dog_watch/dogwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dogwatch
sudo systemctl start dogwatch

# Verify it's running
sudo systemctl status dogwatch

# Tail the logs
journalctl -u dogwatch -f
```

**Expected:** Service shows `active (running)`. Logs match what you saw in interactive mode.

### 8a. Crash recovery

```bash
# Find the process and kill it
sudo systemctl kill dogwatch

# Wait 10 seconds (RestartSec=10), then check:
sudo systemctl status dogwatch
```

**Expected:** Service automatically restarts. Status shows `active (running)` again.

### 8b. Boot persistence

```bash
sudo reboot
```

SSH back in after reboot:

```bash
sudo systemctl status dogwatch
```

**Expected:** Service is running, started automatically at boot.

---

## 9. Memory Soak Test

Let DogWatch run for at least 1 hour with periodic detections.

```bash
# Check memory usage
free -m

# Check DogWatch process specifically
ps aux | grep dog_watch

# Check for OOM kills
dmesg | grep -i "oom\|killed"
```

**Expected:**
- Total memory usage stays below 400MB
- No OOM kills in dmesg
- DogWatch process RSS stays stable (no memory leak)

---

## 10. Updating Code on the Pi

After making changes on your Mac and pushing to GitHub:

```bash
# On your Mac
cd /Users/sydneywatson/dogdetector9000
git add -A && git commit -m "Description of changes" && git push

# On the Pi (SSH)
cd /home/pi/dog_watch
git pull
sudo systemctl restart dogwatch
journalctl -u dogwatch -f
```

---

## Quick Reference: Test Checklist

- [ ] Camera detected (`rpicam-hello --list-cameras`)
- [ ] Inference runs without errors
- [ ] ntfy test notification received on phone
- [ ] COCO labels file correct (person=line 1, dog=line 17)
- [ ] Script starts without errors
- [ ] Dog detection triggers frame save + notification
- [ ] Multi-dog count is correct in notification
- [ ] Human in frame prevents frame save (privacy gate)
- [ ] Dog + human together prevents frame save
- [ ] Notification cooldown works (no spam)
- [ ] Web dashboard loads at :8080
- [ ] Dashboard auto-refreshes with new frames
- [ ] API endpoints return valid JSON
- [ ] Frame count never exceeds MAX_KEPT_FRAMES
- [ ] Status file is always valid JSON
- [ ] Health heartbeat arrives on ntfy
- [ ] Systemd service starts and auto-restarts
- [ ] Service survives reboot
- [ ] Memory stable after 1 hour
