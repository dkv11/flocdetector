"""
migrate_errors.py — add sample_id column to device_errors so errors that
occur during a test can be linked to that sample and placed on its curve.
Safe to run once. Run on the EC2:  python3 migrate_errors.py
"""
import sqlite3
conn = sqlite3.connect("/home/ubuntu/flocdashboard/flocdash.db")

# Check if the column already exists.
cols = [r[1] for r in conn.execute("PRAGMA table_info(device_errors)").fetchall()]
if "sample_id" not in cols:
    conn.execute("ALTER TABLE device_errors ADD COLUMN sample_id TEXT")
    conn.commit()
    print("Added sample_id column to device_errors.")
else:
    print("sample_id column already exists.")
conn.close()
