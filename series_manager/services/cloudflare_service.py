import datetime

import requests

from .settings_service import load_config


def get_cloudflare_stats():
    config = load_config()
    account_id = config.get("cloudflare_account_id")
    api_token = config.get("cloudflare_api_token")
    bucket_name = config.get("r2_bucket_name")

    if not account_id or not api_token:
        return {"error": "Missing Cloudflare Account ID or API Token"}

    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    worker_plan = "free"

    try:
        sub_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/subscriptions"
        sub_res = requests.get(sub_url, headers=headers, timeout=20)
        if sub_res.status_code == 200:
            for sub in sub_res.json().get("result", []):
                rate_plan = sub.get("rate_plan", {}).get("id", "").lower()
                if "workers_paid" in rate_plan or "workers_unbound" in rate_plan:
                    worker_plan = "paid"
                    break
    except Exception:
        pass

    now = datetime.datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = "https://api.cloudflare.com/client/v4/graphql"

    query_workers = """
    query GetStats($accountTag: string, $monthStart: string, $todayStart: string) {
      viewer {
        accounts(filter: {accountTag: $accountTag}) {
          workersMonthly: workersInvocationsAdaptive(limit: 1, filter: {datetime_geq: $monthStart}) {
            sum { requests cpuTime }
            quantiles { cpuTimeP50 cpuTimeP99 }
          }
          workersDaily: workersInvocationsAdaptive(limit: 1, filter: {datetime_geq: $todayStart}) {
            sum { requests cpuTime }
            quantiles { cpuTimeP50 cpuTimeP99 }
          }
        }
      }
    }
    """
    query_workers_fallback = """
    query GetStats($accountTag: string, $monthStart: string, $todayStart: string) {
      viewer {
        accounts(filter: {accountTag: $accountTag}) {
          workersMonthly: workersInvocationsAdaptive(limit: 1, filter: {datetime_geq: $monthStart}) { sum { requests } }
          workersDaily: workersInvocationsAdaptive(limit: 1, filter: {datetime_geq: $todayStart}) { sum { requests } }
        }
      }
    }
    """
    query_r2 = """
    query GetR2Stats($accountTag: string, $monthStart: string, $bucketName: string) {
      viewer {
        accounts(filter: {accountTag: $accountTag}) {
          r2OperationsAdaptiveGroups(limit: 1000, filter: {datetime_geq: $monthStart, bucketName: $bucketName}) {
            dimensions { actionType }
            sum { requests }
          }
          r2StorageAdaptiveGroups(limit: 1, filter: {datetime_geq: $monthStart, bucketName: $bucketName}) {
            max { metadataSize payloadSize }
          }
        }
      }
    }
    """

    try:
        worker_limit = 10000000 if worker_plan == "paid" else 100000
        worker_label = "Monthly Invocations" if worker_plan == "paid" else "Daily Invocations"
        variables = {"accountTag": account_id, "monthStart": month_start, "todayStart": today_start}
        res = requests.post(url, headers=headers, json={"query": query_workers, "variables": variables}, timeout=30)
        data = res.json()
        if "errors" in data and any("cpuTime" in err.get("message", "") for err in data["errors"]):
            res = requests.post(url, headers=headers, json={"query": query_workers_fallback, "variables": variables}, timeout=30)
            data = res.json()

        worker_requests = 0
        worker_cpu_total_ms = 0
        worker_cpu_p50_ms = 0
        worker_cpu_p99_ms = 0
        accounts = data.get("data", {}).get("viewer", {}).get("accounts", [])
        if accounts:
            worker_data = accounts[0].get("workersMonthly", []) if worker_plan == "paid" else accounts[0].get("workersDaily", [])
            if worker_data:
                worker_requests = worker_data[0].get("sum", {}).get("requests", 0)
                worker_cpu_total_ms = worker_data[0].get("sum", {}).get("cpuTime", 0) / 1000
                worker_cpu_p50_ms = worker_data[0].get("quantiles", {}).get("cpuTimeP50", 0) / 1000
                worker_cpu_p99_ms = worker_data[0].get("quantiles", {}).get("cpuTimeP99", 0) / 1000

        r2_class_a = 0
        r2_class_b = 0
        storage_bytes = 0
        r2_vars = {"accountTag": account_id, "monthStart": month_start, "bucketName": bucket_name}
        r2_res = requests.post(url, headers=headers, json={"query": query_r2, "variables": r2_vars}, timeout=30)
        r2_accounts = r2_res.json().get("data", {}).get("viewer", {}).get("accounts", [])
        if r2_accounts:
            class_a = {"PutObject", "CopyObject", "CompleteMultipartUpload", "CreateMultipartUpload", "UploadPart", "UploadPartCopy", "ListObjects", "ListBuckets", "CreateBucket"}
            class_b = {"GetObject", "HeadObject", "HeadBucket"}
            for op in r2_accounts[0].get("r2OperationsAdaptiveGroups", []):
                action = op.get("dimensions", {}).get("actionType", "")
                requests_count = op.get("sum", {}).get("requests", 0)
                if action in class_a:
                    r2_class_a += requests_count
                elif action in class_b:
                    r2_class_b += requests_count
            storage = r2_accounts[0].get("r2StorageAdaptiveGroups", [])
            if storage:
                max_vals = storage[0].get("max", {})
                storage_bytes = max_vals.get("metadataSize", 0) + max_vals.get("payloadSize", 0)

        storage_gb = storage_bytes / (1024 ** 3)
        storage_cost = max(0, storage_gb - 10) * 0.015
        class_a_cost = (max(0, r2_class_a - 1000000) / 1000000) * 4.50
        class_b_cost = (max(0, r2_class_b - 10000000) / 1000000) * 0.36
        worker_req_cost = (max(0, worker_requests - 10000000) / 1000000) * 0.30 if worker_plan == "paid" else 0
        worker_cpu_cost = (max(0, worker_cpu_total_ms - 30000000) / 1000000) * 0.02 if worker_plan == "paid" else 0

        return {
            "worker_plan": worker_plan.upper(),
            "worker_label": worker_label,
            "storage_gb": round(storage_gb, 2),
            "storage_limit": 10,
            "storage_cost": round(storage_cost, 2),
            "r2_class_a": r2_class_a,
            "class_a_limit": 1000000,
            "class_a_cost": round(class_a_cost, 2),
            "r2_class_b": r2_class_b,
            "class_b_limit": 10000000,
            "class_b_cost": round(class_b_cost, 2),
            "worker_requests": worker_requests,
            "worker_limit": worker_limit,
            "worker_cpu_total_ms": round(worker_cpu_total_ms, 2),
            "worker_cpu_p50_ms": round(worker_cpu_p50_ms, 2),
            "worker_cpu_p99_ms": round(worker_cpu_p99_ms, 2),
            "worker_req_cost": round(worker_req_cost, 2),
            "worker_cpu_cost": round(worker_cpu_cost, 2),
            "total_overage": round(storage_cost + class_a_cost + class_b_cost + worker_req_cost + worker_cpu_cost, 2),
        }
    except requests.exceptions.RequestException as exc:
        return {"error": f"API Request failed: {exc}"}
    except Exception as exc:
        return {"error": f"Failed to parse data: {exc}"}


def get_cloudflare_billing():
    config = load_config()
    account_id = config.get("cloudflare_account_id")
    api_token = config.get("cloudflare_api_token")
    if not account_id or not api_token:
        return {"error": "Missing Cloudflare Account ID or API Token"}

    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    billing_info = {"last_payment": "-", "next_billing_date": "-", "error": None}

    try:
        sub_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/subscriptions"
        sub_res = requests.get(sub_url, headers=headers, timeout=20)
        if sub_res.status_code == 200:
            for sub in sub_res.json().get("result", []):
                current_period_end = sub.get("current_period_end")
                if current_period_end:
                    if isinstance(current_period_end, (int, float)):
                        dt = datetime.datetime.fromtimestamp(current_period_end / 1000.0)
                    else:
                        dt = datetime.datetime.fromisoformat(current_period_end.replace("Z", "+00:00"))
                    billing_info["next_billing_date"] = dt.strftime("%d %b %Y")
                    break

        history_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/billing/history"
        history_res = requests.get(history_url, headers=headers, timeout=20)
        if history_res.status_code == 200:
            for txn in history_res.json().get("result", []):
                if txn.get("action", "").lower() in {"charge", "payment"}:
                    billing_info["last_payment"] = f"{txn.get('amount', 0)} {txn.get('currency', 'USD')}"
                    break
    except Exception:
        billing_info["error"] = "Billing: Read error"

    return billing_info
