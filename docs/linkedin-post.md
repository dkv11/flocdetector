# LinkedIn Post — Options

Three versions at different lengths/tones. Pick the one that fits you, or mix.
Replace [GitHub link] with your repo URL. Adjust any details to your voice.

---

## Version 1 — The "constraints forced good engineering" story (recommended)

I built a complete edge-to-cloud IoT platform on a 2 GB server. The constraint
taught me more than any tutorial could.

The project: FlocDetector — a system that monitors wastewater sludge-settling
tests across a fleet of sensors. Cameras + an ML model on Raspberry Pis measure
how sludge settles each minute; the cloud ingests that telemetry, computes health
metrics, and streams live settling curves to a dashboard with snapshot images.

The catch: it all had to run on a single EC2 instance with ~1.2 GB usable RAM,
already shared with other production services. No room for a bigger box.

That constraint drove every decision:

→ SQLite instead of PostgreSQL — a "lesser" database, but it's just a file with
  near-zero idle memory. The "better" option would have consumed RAM I didn't have.

→ Three lightweight Flask processes instead of one FastAPI service — FastAPI's
  startup memory spike literally triggered the kernel's out-of-memory killer.
  Three ~30 MB processes fit where one heavier one couldn't.

→ Lazy vs. eager library loading, placed deliberately — I moved a library's
  memory spike to startup in one service and to first-use in another, depending
  on where a spike would do the least damage.

→ S3 for images, only the keys in the database — keep the DB small and fast; let
  object storage do what it's built for.

The biggest lesson: a "better" tool that doesn't fit is worse than a simpler one
that runs reliably. Good architecture isn't about using the most powerful tools —
it's about the best fit for your actual constraints.

Full write-up, code, and an implementation guide (the how AND the why) on GitHub:
[GitHub link]

#IoT #SystemDesign #Python #AWS #SoftwareEngineering #EdgeComputing

---

## Version 2 — Shorter, punchier

"Just use a bigger server" wasn't an option. So I learned to build within limits.

I built FlocDetector: an edge-to-cloud IoT platform monitoring wastewater
sludge-settling across a fleet of sensors — live settling curves, real-time
updates, image capture to S3, multi-user dashboard. All on ONE 2 GB EC2 box
already shared with production services.

Every architecture choice came from that constraint:
• SQLite over Postgres (zero idle RAM — it's just a file)
• 3 light Flask processes over 1 FastAPI (its startup spike OOM-killed the box)
• S3 for images, keys in the DB (keep the database small)
• Store UTC, convert to each device's local timezone at display (sensors span
  countries)

The takeaway I'll carry forward: architecture is the art of fitting the solution
to the constraints — not reaching for the most powerful tool.

Code + full implementation guide: [GitHub link]

#IoT #SystemDesign #Python #AWS #EdgeComputing

---

## Version 3 — Story-first / reflective

A single "Killed" message in my terminal taught me more about system design than
months of reading about it.

I was building FlocDetector — an IoT platform to monitor wastewater sludge-settling
across a fleet of field sensors. Live curves streaming to a dashboard, snapshot
images, multi-user access. The whole cloud side had to run on one 2 GB EC2 instance
already shared with production workloads.

The first time I tried a "proper" modern stack — FastAPI, a real database — the
kernel started killing my processes. Out of memory. There was no bigger box coming.

So I rebuilt around the constraint instead of fighting it:
- SQLite (a file, not a server — near-zero idle memory)
- Three small Flask processes, each ~30 MB, decoupled so one crashing never takes
  down the others
- MQTT ingestion with idempotent writes (safe against network redelivery)
- A resilient test-lifecycle with four independent close triggers + a sweep for
  units that drop mid-test
- Real-time updates via Server-Sent Events, with a single shared DB-watcher thread
  so 10 open dashboards don't mean 10× the queries

It runs in production now, monitoring a live fleet — comfortably, with RAM to spare.

The lesson stuck: constraints aren't obstacles to good engineering. They ARE the
engineering. The best solution is the one that fits.

Write-up + code + a full implementation guide: [GitHub link]

#SoftwareEngineering #SystemDesign #IoT #Python #AWS #EdgeComputing

---

## Tips for posting

- **Add a visual.** A screenshot of your dashboard (a settling curve with the
  event tags) or the architecture diagram gets far more engagement than text alone.
  Blur/rename anything client-specific first.
- **Post timing:** Tue–Thu mornings tend to perform best on LinkedIn.
- **First comment:** drop the GitHub link in the first comment too (some say
  external links in the post body get down-ranked; a link in a comment hedges that).
- **Engage early:** reply to the first few comments quickly — it boosts reach.
- **Keep it honest:** the constraints story is compelling *because* it's real.
  If someone asks a follow-up (e.g. "why not a bigger instance?"), you have a
  genuine answer — that's what makes it credible.
