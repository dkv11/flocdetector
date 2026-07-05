"""
migrate_timezone.py — add a `timezone` column to devices so each unit's
timestamps can be shown in its own local (plant) time.

Stores IANA timezone names like 'Asia/Kolkata', 'America/Los_Angeles'.
Defaults to 'UTC'. Safe to run once.  Run:  python3 migrate_timezone.py
"""
import sqlite3
conn = sqlite3.connect("/home/ubuntu/flocdashboard/flocdash.db")

cols = [r[1] for r in conn.execute("PRAGMA table_info(devices)").fetchall()]
if "timezone" not in cols:
    conn.execute("ALTER TABLE devices ADD COLUMN timezone TEXT DEFAULT 'UTC'")
    conn.commit()
    print("Added timezone column to devices (default 'UTC').")
else:
    print("timezone column already exists.")

# Show current devices so you can set each one's timezone.
print("\nCurrent devices:")
for r in conn.execute("SELECT id, plant_id, plc_id, name, timezone FROM devices").fetchall():
    print(f"  id={r[0]}  name={r[3]}  tz={r[4]}  ({r[1]}/{r[2]})")
conn.close()
print("\nSet a device's timezone with, e.g.:")
print("  sqlite3 flocdash.db \"UPDATE devices SET timezone='Asia/Kolkata' WHERE id=2;\"")
print("  sqlite3 flocdash.db \"UPDATE devices SET timezone='America/Los_Angeles' WHERE id=5;\"")
