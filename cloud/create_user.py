"""
create_user.py — add a dashboard user.
Usage:  python3 create_user.py <username> <password> <role>
        role is 'admin' or 'user'
"""
import sys
import db
from werkzeug.security import generate_password_hash

if len(sys.argv) != 4:
    print("Usage: python3 create_user.py <username> <password> <role>")
    sys.exit(1)

username, password, role = sys.argv[1], sys.argv[2], sys.argv[3]
conn = db.get_connection()
conn.execute(
    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
    (username, generate_password_hash(password), role))
conn.commit()
conn.close()
print(f"Created {role} user: {username}")
