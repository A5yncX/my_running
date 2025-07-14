#!/usr/bin/env python3

"""
Modern Garmin Connect Exporter (CSV-only, flat file version)
Author: AsyncX
Description:
    1. Perform Garmin Connect login with token persistence to avoid repeated logins.
    2. After successful login, obtain secret_string and instantiate the asynchronous AsyncGarmin client.
    3. Fetch the recent activity list and iterate through each activity:
       - Retrieve averageHR or averageHeartRate from summaryDTO (Heartrate)
       - Retrieve totalElevationGain or elevationGain from summaryDTO (Elevation Gain)
    4. Write results to src/components/activities.csv, inserting new records at the top.
"""

import os
import csv
import getpass
import argparse
import asyncio

from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
    GarminConnectAuthenticationError
)
from httpx import Timeout
import cloudscraper
import garth
import httpx

class AsyncGarmin:
    """Asynchronous Garmin client (simplified)."""
    def __init__(self, secret_string, auth_domain="COM", only_running=False):
        self.req = httpx.AsyncClient(timeout=Timeout(240.0, connect=360.0))
        self.cf_req = cloudscraper.CloudScraper()
        urls = {
            "COM": {
                "MODERN_URL": "https://connectapi.garmin.com",
                "ACTIVITY_URL": "https://connectapi.garmin.com/activity-service/activity/{id}"
            },
            "CN": {
                "MODERN_URL": "https://connectapi.garmin.cn",
                "ACTIVITY_URL": "https://connectapi.garmin.cn/activity-service/activity/{id}"
            }
        }[auth_domain]
        self.base = urls["MODERN_URL"]
        self.activity_url = urls["ACTIVITY_URL"]

        # Load and refresh tokens if needed
        garth.client.loads(secret_string)
        if garth.client.oauth2_token.expired:
            garth.client.refresh_oauth2()

        self.headers = {
            "Authorization": str(garth.client.oauth2_token),
            "User-Agent": "Mozilla/5.0"
        }
        self.only_running = only_running

    async def get_activities(self, start, limit):
        """Fetch available activities."""
        url = f"{self.base}/activitylist-service/activities/search/activities?start={start}&limit={limit}"
        if self.only_running:
            url += "&activityType=running"
        r = await self.req.get(url, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def get_activity_summary(self, activity_id):
        """Fetch activity summary for a given activity ID."""
        url = self.activity_url.format(id=activity_id)
        r = await self.req.get(url, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def close(self):
        """Close the HTTP client."""
        await self.req.aclose()

def main():
    parser = argparse.ArgumentParser(description="Export Garmin activities to CSV summary.")
    parser.add_argument('--username', required=True, help='Garmin Connect username (email)')
    parser.add_argument('--password', help='Garmin Connect password (or will be prompted)')
    parser.add_argument('--count', type=int, default=200, help='Number of recent activities to fetch')
    args = parser.parse_args()

    # Prompt for password if not provided
    password = args.password or getpass.getpass("Enter your Garmin Connect password: ")

    # Prepare token directory
    token_dir = os.path.expanduser(os.getenv("GARMINTOKENS", "~/.garminconnect"))
    os.makedirs(token_dir, exist_ok=True)

    # Synchronous login with token persistence
    sync_client = Garmin()
    try:
        sync_client.login(token_dir)
        print("✅ Login successful (using cached token)")
    except (FileNotFoundError, GarminConnectAuthenticationError):
        try:
            sync_client = Garmin(email=args.username, password=password)
            sync_client.login()
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError) as e:
            print("❌ Login failed:", e)
            return
        sync_client.garth.dump(token_dir)
        print("✅ Login successful (username/password), new token saved")

    # Prepare CSV file path
    base = os.path.dirname(os.path.abspath(__file__))
    csv_dir = os.path.join(os.path.dirname(base), "src", "components")
    os.makedirs(csv_dir, exist_ok=True)
    csv_file = os.path.join(csv_dir, "activities.csv")

    # Load existing activity IDs
    seen = set()
    if os.path.exists(csv_file):
        with open(csv_file, encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                seen.add(row["Activity ID"])

    # Create asynchronous client
    secret = sync_client.garth.dumps()
    auth_domain = "COM"  # or "CN" if required
    ag = AsyncGarmin(secret, auth_domain)

    async def fetch_and_write():
        # Fetch recent activities
        acts = await ag.get_activities(0, args.count)
        new_rows = []

        for act in acts:
            aid = str(act["activityId"])
            if aid in seen:
                continue

            type_key = act.get("activityType", {}).get("typeKey", "unknown")
            category = type_key.replace('_', ' ').title()
            start = act.get("startTimeLocal", "")
            dist = round(act.get("distance", 0) / 1000, 2)
            steps = act.get("steps", "N/A") if type_key in ["running", "walking"] else "N/A"
            dur = round(act.get("duration", 0) / 60, 2)

            # Fetch summaryDTO for heart rate and elevation gain
            summary = await ag.get_activity_summary(aid)
            dto = summary.get("summaryDTO", {})
            hr_val = dto.get("averageHR") or dto.get("averageHeartRate")
            hr = hr_val if hr_val is not None else "N/A"
            eg = dto.get("totalElevationGain") or dto.get("elevationGain") or "N/A"

            print(f"New activity {aid}: hr={hr}, gain={eg}")
            new_rows.append([aid, category, start, dist, steps, dur, hr, eg])

        # If there are new records, write them at the top of the CSV
        if new_rows:
            header = [
                "Activity ID", "Category", "Start Time",
                "Distance (km)", "Steps", "Duration (min)",
                "Heartrate (BPM)", "Elevation Gain (m)"
            ]
            old_rows = []

            if os.path.exists(csv_file):
                with open(csv_file, 'r', encoding='utf-8', newline='') as f:
                    reader = csv.reader(f)
                    existing_header = next(reader, None)
                    for row in reader:
                        old_rows.append(row)

            # Overwrite file with header, new rows, then old rows
            with open(csv_file, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                for row in new_rows + old_rows:
                    writer.writerow(row)

        await ag.close()

    asyncio.run(fetch_and_write())
    print("✅ Finished! File:", csv_file)

if __name__ == "__main__":
    main()
