# -*- coding: utf-8 -*-
"""
Created on Fri Mar  7 12:38:18 2025

@author: Ali
"""

import requests
import os
from tqdm import tqdm

BASE_URL = "https://dserver.bundestag.de/btp"
SAVE_FOLDER = "bundestag_pdfs"
os.makedirs(SAVE_FOLDER, exist_ok=True)

# Define start and end points
start_wp = 21
end_wp = 21
end_sitzungen = {20: 70}  # Optional stopping point for WP 20

for wp in range(start_wp, end_wp + 1):
    wp_str = f"{wp:02d}"
    sitzung = 1
    print(f"\n▶️ Starting WP {wp_str}")

    while True:
        sitzung_str = f"{sitzung:03d}"
        filename = f"{wp_str}{sitzung_str}.pdf"
        url = f"{BASE_URL}/{wp_str}/{filename}"
        save_path = os.path.join(SAVE_FOLDER, filename)

        try:
            response = requests.get(url)
            if response.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(response.content)
                print(f"  ✅ Downloaded {filename}")
                sitzung += 1
                # Stop if we've hit the known end point
                if wp in end_sitzungen and sitzung > end_sitzungen[wp]:
                    print(f"  🛑 Reached last known Sitzung for WP {wp_str}.")
                    break
            else:
                print(f"  ❌ No more Sitzungen in WP {wp_str} after {sitzung - 1}. Stopping WP.")
                break

        except Exception as e:
            print(f"  ❌ Error downloading {filename}: {e}")
            break

print("\n✅ All available PDFs downloaded.")
