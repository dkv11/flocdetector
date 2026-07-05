import cv2
import os
import time
import torch
import json
import base64
from datetime import datetime, timedelta
from ultralytics import YOLO
import paho.mqtt.client as mqtt
import uuid
import socket
from dotenv import load_dotenv

load_dotenv()

# MQTT broker details
mqtt_broker = os.getenv("MQTT_BROKER", "localhost")
port = int(os.getenv("MQTT_PORT", "1883"))

# MQTT topics
topics = {
    "data": os.getenv("MQTT_TOPIC_DATA", "Sv30Beaker/data"),
    "state": os.getenv("MQTT_TOPIC_STATE", "BeakerSv30/State"),
}

# Initialize MQTT client
client = mqtt.Client(os.getenv("MQTT_CLIENT_ID", "SV30Beaker"))

# RTSP stream URL
rtsp_url = os.getenv("RTSP_URL", "rtsp://USER:PASSWORD@CAMERA_IP:554/stream1")

BASE_DIR_IMG = os.getenv("BASE_DIR_IMG", "/home/pi/sv30FD")
BASE_DIR_CODE = os.getenv("BASE_DIR_CODE", "/home/pi/sv30")

# Directory to save captured images
images_dir = os.path.join(BASE_DIR_IMG, "capturedImgV2")
os.makedirs(images_dir, exist_ok=True)

# --------------------------- CONFIGURATIONS ---------------------------

CONFIG = {
    "JSONL_FILE": os.path.join(BASE_DIR_CODE, "sludge_data.jsonl"),
    "MODEL_PATH": os.path.join(BASE_DIR_CODE, "best.pt"),
    "MIN_CONF": {
        "Beaker": float(os.getenv("MIN_CONF_BEAKER", "0.50")),
        "Sludge": float(os.getenv("MIN_CONF_SLUDGE", "0.20")),
        "FloatingSludge": float(os.getenv("MIN_CONF_FLOATING_SLUDGE", "0.50")),
    },
    "RETENTION_DAYS": int(os.getenv("RETENTION_DAYS", "14")),
}

MAINTENANCE_TRACKER = os.path.join(BASE_DIR_CODE, "last_maintenance.txt")

def is_clock_reliable():
    """Checks if the Raspberry Pi system clock is actually correct."""
    return datetime.now().year >= 2025


def perform_maintenance():
    """Trims the log file and deletes images older than 14 days."""
    if not is_clock_reliable():
        print("Clock unreliable (System thinks it is 1970). Skipping maintenance to prevent data loss.")
        return

    print("Running 14-day maintenance...")
    now = datetime.now()
    cutoff_date = now - timedelta(days=CONFIG["RETENTION_DAYS"])

    # Trim the JSONL log file
    if os.path.exists(CONFIG["JSONL_FILE"]):
        try:
            with open(CONFIG["JSONL_FILE"], "r") as f:
                lines = f.readlines()
            kept_lines = []
            for line in lines:
                try:
                    data = json.loads(line)
                    log_time = datetime.strptime(data["timestamp"], "%Y-%m-%d %H:%M:%S")
                    if log_time > cutoff_date:
                        kept_lines.append(line)
                except:
                    continue
            with open(CONFIG["JSONL_FILE"], "w") as f:
                f.writelines(kept_lines)
            print(f"Log trimmed: Kept {len(kept_lines)} lines.")
        except Exception as e:
            print(f"Error trimming log: {e}")

    # Delete images older than 14 days
    retention_seconds = CONFIG["RETENTION_DAYS"] * 24 * 60 * 60
    current_time_unix = time.time()
    for filename in os.listdir(images_dir):
        file_path = os.path.join(images_dir, filename)
        if os.path.isfile(file_path):
            if (current_time_unix - os.path.getmtime(file_path)) > retention_seconds:
                os.remove(file_path)

    # Record this successful maintenance run
    with open(MAINTENANCE_TRACKER, "w") as f:
        f.write(now.strftime("%Y-%m-%d %H:%M:%S"))


# --------------------------- STATE VARIABLES ---------------------------

test_state = {
    "id": None,
    "in_progress": False,  # Whether a test is in progress
    "start_time": None,  # Start time of the current test (set on first frame)
    "floating_sludge_detected": False,  # Whether floating sludge is detected
    "floating_sludge_flag": False,
    "floating_sludge": False,
    "current_state": "idle",  # idle / start / ongoing / end / error
    "base64_encoded": None,
}

# --------------------------- MODEL LOADING ---------------------------
device = torch.device("cpu")
model = YOLO(CONFIG["MODEL_PATH"])
model.model.to(device)

capture_interval = int(os.getenv("CAPTURE_INTERVAL", "60"))
CPU_TEMP_THRESHOLD = float(os.getenv("CPU_TEMP_THRESHOLD", "100"))
MAX_SLUDGE_HEIGHT = int(os.getenv("MAX_SLUDGE_HEIGHT", "826"))
SNAPSHOT_QUALITY = int(os.getenv("SNAPSHOT_QUALITY", "30"))

# --------------------------- MQTT CALLBACKS ---------------------------


def on_message(client, userdata, message):
    """Callback for MQTT message reception."""
    global test_state
    payload = message.payload.decode("utf-8")

    if message.topic == topics["state"] and payload == "start":

        # 1. If ongoing (start/ongoing), force end the current test.
        # This is a cleanup step before starting the new test.
        if test_state["current_state"] not in ["idle", "error", "end"]:
            print("Ending current test to start a new one.")
            end_test()

        # 2. Start a new test if the state is suitable (idle, error, or just ended).
        print("Received 'Start' command. Starting new test.")
        start_new_test()


def on_connect(client, userdata, flags, rc):
    """Callback for MQTT connection."""
    if rc == 0:
        print("Connected to MQTT broker.")
        client.subscribe(topics["state"], qos=2)  # Subscribe to state topic with QoS 2
    else:
        print(f"Failed to connect to MQTT broker. Return code: {rc}")


# Initialize MQTT client and set callbacks
client.on_message = on_message
client.on_connect = on_connect

# Connect to the MQTT broker
client.connect(mqtt_broker, port)
client.loop_start()  # Start the MQTT loop in a separate thread


# --------------------------- HELPER FUNCTIONS ---------------------------


def write_jsonl(data):
    """Append JSON record to a JSONL file."""
    with open(CONFIG["JSONL_FILE"], "a") as f:
        json.dump(data, f)
        f.write("\n")


def get_elapsed_minutes(start_time, current_time):
    """Return elapsed minutes between two datetime objects."""
    if not start_time:
        return 0  # Ensure new test starts at 0 minutes
    elapsed = round((current_time - start_time).total_seconds() / 60)
    return max(0, elapsed)


def attach_image_to_payload(payload: dict, img, quality: int = 30):
    """
    Try to encode `img` (BGR numpy array) to JPEG and attach base64 to payload as `base64_encoded`.
    If encoding fails or img is None, set `base64_encoded` to None and add an `image_error` message.
    """
    try:
        if img is None:
            payload["base64_encoded"] = None
            payload["image_error"] = "no_image"
            return payload
        _, buffer = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        payload["base64_encoded"] = base64.b64encode(buffer).decode("utf-8")
    except Exception as exc:
        payload["base64_encoded"] = None
        payload["image_error"] = f"encoding_failed: {exc}"
    return payload


def log_state(
    timestamp,
    state,
    image_path,
    sludge_percentage=None,
    sludge_bbox=None,
    base64_encoded=None,
    elapsed_min=None,
):
    """Logs test progress with the required JSON format."""
    log_data = {
        "timestamp": timestamp,
        "sampleId": test_state["id"],
        "minute": elapsed_min,
        "image_file": os.path.basename(image_path) if image_path else None,
        "sludge_percentage": sludge_percentage if sludge_percentage else 0.0,
        "floating_sludge_detected": test_state["floating_sludge_detected"],
        "floating_sludge_flag": test_state["floating_sludge_flag"],
        "floating_sludge": test_state["floating_sludge"],
        "sludge_bbox": sludge_bbox if sludge_bbox else None,
        "state": state,
        "base64_encoded": base64_encoded if base64_encoded else None,
    }

    write_jsonl(log_data)
    publish_sensor_data(log_data)


def start_new_test():
    """Start a new test session.

    NOTE: start_time is intentionally kept None and will be set on the FIRST frame
    of this test in process_image(), to align the timer with the first image.
    """
    global test_state
    test_state["id"] = uuid.uuid4().hex
    test_state.update(
        {
            "in_progress": True,
            "start_time": None,  # will be set on first frame of test
            "current_state": "start",
            "floating_sludge_detected": False,
            "floating_sludge_flag": False,
            "floating_sludge": False,
            "base64_encoded": None,
        }
    )
    print(f"New test started. Test ID: {test_state['id']}")


def end_test():
    """End the current test session."""
    global test_state
    test_state.update(
        {
            "in_progress": False,
            "current_state": "end",
            "start_time": None,
            "id": None,
        }
    )
    print("Test ended.")


def calculate_sludge_percentage(sludge_bboxes):
    """Calculate sludge percentage based on bounding box height."""
    if not sludge_bboxes:
        return None
    first_bbox = sludge_bboxes[0]
    sludge_height = first_bbox[3] - first_bbox[1]
    return (sludge_height / MAX_SLUDGE_HEIGHT) * 100


def publish_sensor_data(data):
    """Publish sensor data to the MQTT broker, but print truncated version to console."""
    try:
        # 1. Prepare the actual full payload for MQTT
        json_payload = json.dumps(data)
        client.publish(topics["data"], json_payload)
        
        # 2. Prepare and print the truncated version for the console
        truncated_data = get_truncated_data(data)
        print(f"Published data: {json.dumps(truncated_data)}")
        
    except Exception as e:
        print(f"Error publishing data: {e}")


def report_error(error_data):
    """Centralized error reporting: write/publish an error record and set runtime state to 'error'.

    Keeps `sampleId` intact until the system transitions to 'idle'.
    """
    global test_state
    if "sampleId" not in error_data:
        error_data["sampleId"] = test_state.get("id")
    error_data["state"] = "error"
    write_jsonl(error_data)
    publish_sensor_data(error_data)
    # update runtime state to reflect the error, but do NOT change in_progress
    test_state["current_state"] = "error"
    print(
        f"Error reported and runtime state set to 'error' (in_progress unchanged): "
        f"{error_data.get('message') or error_data.get('error_code')}"
    )


def recover_from_error():
    """Attempt to recover from error state on a clean frame."""
    global test_state
    if test_state["current_state"] != "error":
        return False
    if test_state["in_progress"]:
        test_state["current_state"] = "ongoing"
        print(f"Recovered from error. Continuing test {test_state['id']}")
    else:
        test_state["current_state"] = "idle"
        print("Recovered from error. Returning to idle.")
    return True


def get_cpu_temperature():
    """Read the Raspberry Pi CPU temperature from the system file."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp = f.read()
            temp = float(temp) / 1000.0  # Convert from millidegree to degree Celsius
            return temp
    except FileNotFoundError:
        print("CPU temperature file not found.")
        return None
    
def get_truncated_data(data):
    """Returns a copy of the data with a truncated base64 string for logging/printing."""
    display_data = data.copy()
    b64 = display_data.get("base64_encoded")
    if isinstance(b64, str) and len(b64) > 20:
        display_data["base64_encoded"] = b64[:20] + "..."
    return display_data


# --------------------------- MAIN INFERENCE FUNCTION ---------------------------


def process_image(image_path, image_timestamp):
    """Process a single image using YOLO and update test state."""
    global test_state
    timestamp_str = image_timestamp.strftime("%Y-%m-%d %H:%M:%S")
    test_state["floating_sludge"] = False

    # 1) Align start_time with first frame of the test
    if test_state["in_progress"] and test_state["start_time"] is None:
        test_state["start_time"] = image_timestamp
        # First frame of the test → treat as minute 0
        elapsed_min = 0
    else:
        elapsed_min = get_elapsed_minutes(test_state["start_time"], image_timestamp)

    # 2) HARD TIMEOUT: if test is in progress and runtime > 99 min, end test immediately
    if test_state["in_progress"] and elapsed_min > 98:
        if test_state["current_state"] != "end":
            end_test()

        # Log a final record for this frame (no sludge data, just state + timeout minute)
        log_state(
            timestamp_str,
            test_state["current_state"],
            image_path,
            sludge_percentage=None,
            sludge_bbox=None,
            base64_encoded=None,
            elapsed_min=elapsed_min,
        )
        return

    # 3) Attempt to recover from any error state BEFORE doing CV
    if test_state["current_state"] == "error":
        recover_from_error()

    img = cv2.imread(image_path)
    if img is None:
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E004",
            "message": "Unable to load image",
        }
        attach_image_to_payload(error_data, None)
        report_error(error_data)
        print(f"[Error] Skipped corrupt image: {image_path}")
        return

    try:
        results = model(img)
    except Exception as e:
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E004",
            "message": f"Model inference error: {e}",
        }
        attach_image_to_payload(error_data, img)
        report_error(error_data)
        print(f"Error Misc: {image_path}")
        return

    detections = results[0].boxes
    boxes = detections.xyxy.cpu().numpy()
    confidences = detections.conf.cpu().numpy()
    class_indices = detections.cls.cpu().numpy()
    labels = results[0].names

    beaker_bboxes = []
    sludge_bbox = []
    floating_sludge_bbox = None

    for i, box in enumerate(boxes):
        x1, y1, x2, y2 = map(int, box[:4])
        label = labels[int(class_indices[i])]
        conf = confidences[i]
        if conf >= CONFIG["MIN_CONF"].get(label, 0):
            if label == "Beaker":
                beaker_bboxes.append([x1, y1, x2, y2])
            elif label == "Sludge":
                sludge_bbox.append([x1, y1, x2, y2])
            elif label == "FloatingSludge" and floating_sludge_bbox is None:
                floating_sludge_bbox = (x1, y1, x2, y2)
                test_state["floating_sludge_detected"] = True
                if not test_state["floating_sludge_flag"]:
                    test_state["floating_sludge_flag"] = True
                    test_state["floating_sludge"] = True

    # --- Error conditions (these all RETURN early, but timeout already handled above) ---

    if len(beaker_bboxes) == 0:
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E001",
            "message": "No beaker detected",
        }
        attach_image_to_payload(error_data, img)
        report_error(error_data)
        print("No beaker detected.")
        return

    if len(beaker_bboxes) > 1:
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E002",
            "message": "Multiple Beakers Detected",
        }
        attach_image_to_payload(error_data, img)
        report_error(error_data)
        print("Multiple Beakers Detected")
        return

    if len(sludge_bbox) > 1:
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E003",
            "message": "Multiple Sludge Detected",
        }
        attach_image_to_payload(error_data, img)
        report_error(error_data)
        print("Multiple Sludge Detected")
        return

    temp = get_cpu_temperature()
    if temp is not None:
        if temp > CPU_TEMP_THRESHOLD:
            error_data = {
                "timestamp": timestamp_str,
                "error_code": "E005",
                "message": "CPU Overheating",
            }
            attach_image_to_payload(error_data, img)
            report_error(error_data)
            print("CPU Overheating")
            return
    else:
        print("Unable to get CPU temperature.")

    # ----------------- State transitions (non-timeout) -----------------

    if sludge_bbox:
        sludge_percentage = calculate_sludge_percentage(sludge_bbox)
    else:
        sludge_percentage = None

    # elapsed_min already computed at top

    if test_state["current_state"] == "start" and elapsed_min > 0:
        test_state["current_state"] = "ongoing"

    elif test_state["current_state"] == "end" and elapsed_min == 0:
        test_state["current_state"] = "idle"
        test_state["id"] = None  # Final ID cleanup

    # ----------------- Base64 logic -----------------

    # Floating sludge special case
    if test_state["floating_sludge"] is True:
        try:
            _, buffer = cv2.imencode(".jpg", img)
            base64_encoded = base64.b64encode(buffer).decode("utf-8")
            test_state["base64_encoded"] = base64_encoded
        except Exception as exc:
            test_state["base64_encoded"] = None
            print(f"Failed to encode base64 for floating_sludge: {exc}")
    else:
        test_state["base64_encoded"] = None

    # 30 / 60 / 90 minute snapshots
    if elapsed_min in (30, 60, 90):
        try:
            _, buffer = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), SNAPSHOT_QUALITY])
            base64_encoded = base64.b64encode(buffer).decode("utf-8")
            test_state["base64_encoded"] = base64_encoded
            print(f"Elapsed Minute snapshot taken at: {elapsed_min}")
        except Exception as exc:
            test_state["base64_encoded"] = None
            print(f"Failed to encode base64 image for elapsed_min {elapsed_min}: {exc}")

    if test_state["current_state"] == "idle":
        test_state["base64_encoded"] = None
        test_state["id"] = None

    # Log the current state
    log_state(
        timestamp_str,
        test_state["current_state"],
        image_path,
        sludge_percentage,
        sludge_bbox,
        test_state["base64_encoded"],
        elapsed_min,
    )


def capture_and_infer():
    """Capture an image from the RTSP stream and process it."""

    # Internet connectivity check (runs once per capture interval)
    def is_internet_available(timeout: float = 3.0) -> bool:
        try:
            conn = socket.create_connection(("8.8.8.8", 53), timeout=timeout)
            conn.close()
            return True
        except Exception:
            return False

    timestamp = datetime.now()
    timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    timestamp_file = timestamp.strftime("%Y-%m-%d_%H-%M-%S")

    # If internet is down, report E007 and skip this capture; check runs every minute
    if not is_internet_available():
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E007",
            "message": "Internet unavailable",
        }
        attach_image_to_payload(error_data, None)
        report_error(error_data)
        print("Error: Internet unavailable. Skipping capture.")
        return

    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        cap.release()
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E006",
            "message": "Unable to connect to RTSP stream",
        }
        attach_image_to_payload(error_data, None)
        report_error(error_data)
        print("Error: Unable to connect to RTSP stream.")
        return

    ret, frame = cap.read()

    if ret:
        output_file = os.path.join(images_dir, f"{timestamp_file}.jpg")
        # frame = cv2.rotate(frame, cv2.ROTATE_180)

        # Optional enhancement: CLAHE in LAB space
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        cl = clahe.apply(l)
        enhanced_lab = cv2.merge((cl, a, b))
        enhanced_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)

        cv2.imwrite(output_file, enhanced_bgr)
        process_image(output_file, timestamp)
    else:
        error_data = {
            "timestamp": timestamp_str,
            "error_code": "E006",
            "message": "Unable to connect to RTSP stream",
        }
        attach_image_to_payload(error_data, None)
        report_error(error_data)
        print("Failed to capture image.")
    cap.release()


# --------------------------- MAIN LOOP ---------------------------


def main():
    """Main loop to capture and process images with compensated timing."""
    # Run maintenance immediately on startup if the clock is correct
    try:
        if is_clock_reliable():
            perform_maintenance()
    except Exception as e:
        print(f"Initial maintenance failed: {e}")

    # Initialize the time of the *next* expected capture
    next_capture_time = time.time()

    while True:
        current_time = time.time()

        # Check if we are past the expected capture time (e.g. if processing took too long)
        if current_time >= next_capture_time:
            # If so, just proceed with capture and adjust the *next* expected time
            pass
        else:
            # If we are ahead of schedule, sleep for the remaining time
            time_to_sleep = next_capture_time - current_time
            if time_to_sleep > 0:
                time.sleep(time_to_sleep)

        # Set the target time for the next iteration (60 seconds from the *last target*)
        next_capture_time += capture_interval

        capture_and_infer()

        # Check if 24 hours have passed since last maintenance
        try:
            do_maint = False
            if not os.path.exists(MAINTENANCE_TRACKER):
                do_maint = True
            else:
                with open(MAINTENANCE_TRACKER, "r") as f:
                    last_m_str = f.read().strip()
                    last_m = datetime.strptime(last_m_str, "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - last_m).total_seconds() > 86400:
                        do_maint = True
            if do_maint and is_clock_reliable():
                perform_maintenance()
        except Exception as e:
            print(f"Maintenance check error: {e}")


if __name__ == "__main__":
    main()
