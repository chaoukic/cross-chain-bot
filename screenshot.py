"""Full-page screenshot of the local dashboard (1400px wide).
   python screenshot.py                       -> dashboard.png (default tab)
   python screenshot.py '#new' out.png        -> a given tab (#current / #new) to a given file"""
import os, sys
from playwright.sync_api import sync_playwright
BASE = os.path.dirname(os.path.abspath(__file__))
PORT = open(os.path.join(BASE, "logs", "port")).read().strip() if os.path.exists(os.path.join(BASE, "logs", "port")) else "8797"
HASH = sys.argv[1] if len(sys.argv) > 1 else ""
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE, "dashboard.png")
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    pg.goto(f"http://localhost:{PORT}/{HASH}", wait_until="networkidle")
    pg.wait_for_timeout(3000)
    pg.screenshot(path=OUT, full_page=True)
    b.close()
print("saved", OUT)
