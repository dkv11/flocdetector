# Edge (Raspberry Pi)

- `main.py` — captures frames from the RTSP camera, runs the YOLO model to
  measure the sludge interface each minute, tracks test state (idle → start →
  ongoing → 30/60/90 marks → end, plus floating-sludge and error states), and
  publishes readings over MQTT. Snapshot images at key moments are published for
  the cloud image service.

- **Node-RED** runs alongside on the Pi. It bridges MQTT and forwards snapshot
  images to the cloud image service via a parallel HTTP POST (multipart/form-data),
  including the shared upload token. See `docs/implementation-guide.md` (Part E)
  for the exact function + HTTP nodes and wiring.

Secrets (camera RTSP URL, MQTT broker, device certs) load from a local `.env`
and a `certs/` directory that are git-ignored.
