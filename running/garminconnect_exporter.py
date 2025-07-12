#!/usr/bin/env python3

"""
Modern Garmin Connect Exporter (CSV-only, flat file version)
Author: Adapted by ChatGPT
Description:
    1. 同步登录 Garmin Connect，使用 token 持久化避免重复登录
    2. 登录成功后，获取 secret_string 并调用异步 AsyncGarmin 客户端
    3. 拉取最近活动列表，遍历每个活动：
       - 从 summaryDTO 获取 averageHR 或 averageHeartRate（Heartrate）
       - 从 summaryDTO 获取 totalElevationGain 或 elevationGain（Elevation Gain）
    4. 将结果写入到 src/components/activities.csv，新纪录会插入到最上方
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

# ---------- 异步版 Garmin 客户端（简化） ----------
import httpx
class AsyncGarmin:
    def __init__(self, secret_string, auth_domain="COM", only_running=False):
        self.req = httpx.AsyncClient(timeout=Timeout(240.0, connect=360.0))
        self.cf_req = cloudscraper.CloudScraper()
        urls = {
            "COM": {"MODERN_URL": "https://connectapi.garmin.com", "ACTIVITY_URL": "https://connectapi.garmin.com/activity-service/activity/{id}"},
            "CN": {"MODERN_URL": "https://connectapi.garmin.cn",   "ACTIVITY_URL": "https://connectapi.garmin.cn/activity-service/activity/{id}"}
        }[auth_domain]
        self.base = urls["MODERN_URL"]
        self.activity_url = urls["ACTIVITY_URL"]
        # 加载并刷新 token
        garth.client.loads(secret_string)
        if garth.client.oauth2_token.expired:
            garth.client.refresh_oauth2()
        self.headers = {"Authorization": str(garth.client.oauth2_token), "User-Agent": "Mozilla/5.0"}
        self.only_running = only_running

    async def get_activities(self, start, limit):
        url = f"{self.base}/activitylist-service/activities/search/activities?start={start}&limit={limit}"
        if self.only_running:
            url += "&activityType=running"
        r = await self.req.get(url, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def get_activity_summary(self, activity_id):
        url = self.activity_url.format(id=activity_id)
        r = await self.req.get(url, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def close(self):
        await self.req.aclose()

# ---------- 主流程 ----------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--username', required=True)
    parser.add_argument('--password', help='or will be prompted')
    parser.add_argument('--count',   type=int, default=200)
    args = parser.parse_args()

    # 同步登录并持久化 token
    password = args.password or getpass.getpass("Enter your Garmin Connect password: ")
    token_dir = os.path.expanduser(os.getenv("GARMINTOKENS", "~/.garminconnect"))
    os.makedirs(token_dir, exist_ok=True)

    sync_client = Garmin()
    try:
        sync_client.login(token_dir)
        print("✅ 登录成功（缓存 token）")
    except (FileNotFoundError, GarminConnectAuthenticationError):
        try:
            sync_client = Garmin(email=args.username, password=password)
            sync_client.login()
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError) as e:
            print("❌ 登录失败：", e)
            return
        sync_client.garth.dump(token_dir)
        print("✅ 登录成功（用户名/密码），新 token 已保存")

    # 准备 CSV 文件
    base = os.path.dirname(os.path.abspath(__file__))
    csv_dir = os.path.join(os.path.dirname(base), "src", "components")
    os.makedirs(csv_dir, exist_ok=True)
    csv_file = os.path.join(csv_dir, "activities.csv")

    # 已有记录 ID
    seen = set()
    if os.path.exists(csv_file):
        with open(csv_file, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                seen.add(r["Activity ID"])

    # 异步客户端
    secret = sync_client.garth.dumps()
    auth_domain = "CN" if False else "COM"
    ag = AsyncGarmin(secret, auth_domain)

    async def fetch_and_write():
        acts = await ag.get_activities(0, args.count)
        new_rows = []
        for act in acts:
            aid = str(act["activityId"])
            if aid in seen:
                continue

            typek = act.get("activityType", {}).get("typeKey", "unknown")
            category = typek.replace('_',' ').title()
            start = act.get("startTimeLocal", "")
            dist  = round(act.get("distance", 0) / 1000, 2)
            steps = act.get("steps", "N/A") if typek in ["running","walking"] else "N/A"
            dur   = round(act.get("duration", 0) / 60, 2)

            # 获取 summaryDTO
            sumj = await ag.get_activity_summary(aid)
            dto  = sumj.get("summaryDTO", {})
            hr_val = dto.get("averageHR") or dto.get("averageHeartRate")
            hr = hr_val if hr_val is not None else "N/A"
            eg = dto.get("totalElevationGain") or dto.get("elevationGain") or "N/A"

            print(f"New activity {aid}: hr={hr}, gain={eg}")
            new_rows.append([
                aid, category, start, dist, steps, dur, hr, eg
            ])

        # 如果有新记录，则把它们写到最上方
        if new_rows:
            # 读取旧数据
            old_rows = []
            header = ["Activity ID","Category","Start Time",
                      "Distance (km)","Steps","Duration (min)",
                      "Heartrate (BPM)","Elevation Gain (m)"]
            if os.path.exists(csv_file):
                with open(csv_file, 'r', encoding='utf-8', newline='') as f:
                    reader = csv.reader(f)
                    existing_header = next(reader, None)
                    for row in reader:
                        old_rows.append(row)

            # 将新 + 旧 写回文件（覆盖）
            with open(csv_file, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                for row in new_rows + old_rows:
                    writer.writerow(row)

        await ag.close()

    asyncio.run(fetch_and_write())
    print("✅ 完成，文件：", csv_file)


if __name__ == "__main__":
    main()
