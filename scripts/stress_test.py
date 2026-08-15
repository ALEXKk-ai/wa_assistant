"""Stress & Load Testing Script for WhatsApp Business Assistant.

Simulates multiple concurrent customer turns hitting the webhook API to measure:
1. Throughput (requests/sec)
2. Average, Min, Max, and 95th Percentile Latency (ms)
3. Success rate vs Rate-Limiting protection (200 OK vs 429 Rate-Limited)
"""
import argparse
import asyncio
import os
import statistics
import sys
import time
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_URL = "http://localhost:8000/webhook/whatsapp"


def make_payload(phone_number: str, message_text: str) -> dict:
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "100000000000000")
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": phone_id,
                "changes": [
                    {
                        "value": phone_id,
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "254700000000",
                                "phone_number_id": phone_id,
                            },
                            "contacts": [{"profile": {"name": f"User_{phone_number[-4:]}"}, "wa_id": phone_number}],
                            "messages": [
                                {
                                    "from": phone_number,
                                    "id": f"wamid_test_{time.time_ns()}",
                                    "timestamp": str(int(time.time())),
                                    "text": {"body": message_text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


import hashlib
import hmac
import json
from app.config import get_settings


def compute_sig(body_bytes: bytes) -> str:
    secret = get_settings().whatsapp_app_secret
    digest = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def send_single_request(client: httpx.AsyncClient, url: str, phone: str, text: str) -> tuple[int, float]:
    payload = make_payload(phone, text)
    body_bytes = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": compute_sig(body_bytes),
    }
    start = time.perf_counter()
    try:
        resp = await client.post(url, content=body_bytes, headers=headers, timeout=20.0)
        latency = (time.perf_counter() - start) * 1000
        return resp.status_code, latency
    except Exception:
        latency = (time.perf_counter() - start) * 1000
        return 500, latency


async def run_stress_test(target_url: str, total_users: int, messages_per_user: int):
    print(f"\n🚀 STARTING STRESS TEST ON: {target_url}")
    print(f"👥 Virtual Concurrent Customers: {total_users}")
    print(f"📩 Messages per Customer: {messages_per_user}")
    print(f"📊 Total Concurrent Requests: {total_users * messages_per_user}\n")

    sample_messages = [
        "Are you free tomorrow at 11?",
        "How much is a haircut?",
        "Do you offer braiding?",
        "What time do you open on Friday?",
    ]

    async with httpx.AsyncClient() as client:
        tasks = []
        overall_start = time.perf_counter()

        for user_idx in range(total_users):
            phone = f"2547{user_idx:08d}"
            for msg_idx in range(messages_per_user):
                text = sample_messages[msg_idx % len(sample_messages)]
                tasks.append(send_single_request(client, target_url, phone, text))

        results = await asyncio.gather(*tasks)
        total_time = time.perf_counter() - overall_start

    status_codes = [r[0] for r in results]
    latencies = [r[1] for r in results]

    success_count = status_codes.count(200)
    rate_limited_count = status_codes.count(429)
    error_count = len(status_codes) - success_count - rate_limited_count

    avg_latency = statistics.mean(latencies)
    min_latency = min(latencies)
    max_latency = max(latencies)
    latencies_sorted = sorted(latencies)
    p95_latency = latencies_sorted[int(len(latencies_sorted) * 0.95)]

    print("=" * 60)
    print("📊 BENCHMARK RESULTS")
    print("=" * 60)
    print(f"⏱️  Total Duration       : {total_time:.2f} seconds")
    print(f"⚡ Throughput           : {len(results) / total_time:.2f} req/sec")
    print(f"✅ Successful (200 OK)  : {success_count}/{len(results)} ({success_count/len(results)*100:.1f}%)")
    print(f"🛡️  Rate Limited (429)   : {rate_limited_count}/{len(results)} ({rate_limited_count/len(results)*100:.1f}%)")
    print(f"❌ Error Failures       : {error_count}/{len(results)}")
    print("-" * 60)
    print("⏱️  LATENCY STATS")
    print("-" * 60)
    print(f"• Average Latency : {avg_latency:.1f} ms")
    print(f"• Min Latency     : {min_latency:.1f} ms")
    print(f"• Max Latency     : {max_latency:.1f} ms")
    print(f"• 95th Percentile : {p95_latency:.1f} ms")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stress Test WhatsApp Assistant API")
    parser.add_argument("--url", default=DEFAULT_URL, help="Target URL endpoint")
    parser.add_argument("--users", type=int, default=10, help="Number of concurrent virtual users")
    parser.add_argument("--msgs", type=int, default=2, help="Messages per user")
    args = parser.parse_args()

    asyncio.run(run_stress_test(args.url, args.users, args.msgs))
