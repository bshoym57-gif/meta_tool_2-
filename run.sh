#!/bin/bash
echo "🚀 Meta Ads Tool..."
pip3 install -r requirements.txt
playwright install chromium
mkdir -p templates
python3 app.py
