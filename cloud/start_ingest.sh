#!/bin/bash
cd /home/ubuntu/flocdashboard
source venv/bin/activate
exec python ingest.py
