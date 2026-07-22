"""
Builds a tiny mock 'warehouse' (SQLite) with fake shipment data.
This stands in for the real fact_shipments / dim_carrier tables in the design doc.
Run once: python data/build_db.py
"""
from pathlib import Path
import sqlite3
from datetime import date, timedelta
import random

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR 
DATA_DIR.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect("portdesk.db")
cur = conn.cursor()

cur.execute("DROP TABLE IF EXISTS shipments")
cur.execute("""
CREATE TABLE shipments (
    ship_date TEXT,
    carrier TEXT,
    region TEXT,
    port TEXT,
    on_time INTEGER,       -- 1 = on time, 0 = late
    dwell_hours REAL
)
""")

carriers = ["Maersk", "MSC", "COSCO", "Evergreen"]
regions = ["East Coast", "West Coast"]
ports = {"East Coast": "Newark", "West Coast": "Long Beach"}

today = date(2026, 7, 21)
rows = []
for i in range(60):  # last 60 days
    d = today - timedelta(days=i)
    for _ in range(8):  # 8 shipments/day
        region = random.choice(regions)
        carrier = random.choice(carriers)
        port = ports[region]
        # bake in a deliberate dip: West Coast on-time rate drops hard in the last 7 days
        if region == "West Coast" and i < 7:
            on_time = 1 if random.random() < 0.55 else 0
            dwell = round(random.uniform(30, 55), 1)  # dwell spikes too
        else:
            on_time = 1 if random.random() < 0.90 else 0
            dwell = round(random.uniform(10, 25), 1)
        rows.append((d.isoformat(), carrier, region, port, on_time, dwell))

cur.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?)", rows)
conn.commit()
conn.close()
print(f"Built data/portdesk.db with {len(rows)} rows.")