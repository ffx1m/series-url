import requests
import datetime
from utils.r2 import load_config

def get_cloudflare_stats():
    config = load_config()
    account_id = config.get('cloudflare_account_id')
    api_token = config.get('cloudflare_api_token')
    bucket_name = config.get('r2_bucket_name')

    if not account_id or not api_token:
        return {"error": "Missing Cloudflare Account ID or API Token"}

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }

    # 1. Fetch Subscriptions to determine Worker Plan
    worker_plan = "free"
    try:
        sub_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/subscriptions"
        sub_res = requests.get(sub_url, headers=headers)
        if sub_res.status_code == 200:
            subs = sub_res.json().get("result", [])
            for sub in subs:
                rate_plan = sub.get("rate_plan", {}).get("id", "").lower()
                if "workers_paid" in rate_plan or "workers_unbound" in rate_plan:
                    worker_plan = "paid"
                    break
    except:
        pass

    # 2. Get Dates
    now = datetime.datetime.utcnow()
    # Monthly start (for R2 and Paid Workers)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_str = first_of_month.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Daily start (for Free Workers)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_str = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    url = "https://api.cloudflare.com/client/v4/graphql"
    query = """
    query GetStats($accountTag: string, $monthStart: string, $todayStart: string, $bucketName: string) {
      viewer {
        accounts(filter: {accountTag: $accountTag}) {
          # R2 Operations (Monthly)
          r2OperationsAdaptiveGroups(limit: 1000, filter: {datetime_geq: $monthStart, bucketName: $bucketName}) {
            dimensions {
              actionType
            }
            sum {
              requests
            }
          }
          # R2 Storage (Current)
          r2StorageAdaptiveGroups(limit: 1, filter: {datetime_geq: $monthStart, bucketName: $bucketName}) {
            max {
              metadataSize
              payloadSize
            }
          }
          # Workers Monthly (for Paid Plan)
          workersMonthly: workersInvocationsAdaptive(limit: 1, filter: {datetime_geq: $monthStart}) {
            sum {
              requests
            }
          }
          # Workers Daily (for Free Plan)
          workersDaily: workersInvocationsAdaptive(limit: 1, filter: {datetime_geq: $todayStart}) {
            sum {
              requests
            }
          }
        }
      }
    }
    """

    variables = {
        "accountTag": account_id,
        "monthStart": month_start_str,
        "todayStart": today_start_str,
        "bucketName": bucket_name
    }

    try:
        response = requests.post(url, headers=headers, json={"query": query, "variables": variables})
        response.raise_for_status()
        data = response.json()
        
        if 'errors' in data and data['errors']:
            return {"error": data['errors'][0]['message']}

        accounts = data.get('data', {}).get('viewer', {}).get('accounts', [])
        if not accounts:
            return {"error": "No data returned for this account"}

        account_data = accounts[0]
        
        # Parse R2
        r2_class_a = 0
        r2_class_b = 0
        class_a_types = ['PutObject', 'CopyObject', 'CompleteMultipartUpload', 'CreateMultipartUpload', 'UploadPart', 'UploadPartCopy', 'ListObjects', 'ListBuckets', 'CreateBucket']
        class_b_types = ['GetObject', 'HeadObject', 'HeadBucket']
        
        for op in account_data.get('r2OperationsAdaptiveGroups', []):
            action = op.get('dimensions', {}).get('actionType', '')
            reqs = op.get('sum', {}).get('requests', 0)
            if action in class_a_types: r2_class_a += reqs
            elif action in class_b_types: r2_class_b += reqs
            
        # Parse Workers based on Plan
        if worker_plan == "paid":
            worker_requests = account_data.get('workersMonthly', [{}])[0].get('sum', {}).get('requests', 0)
            worker_limit = 10000000
            worker_label = "Monthly Invocations"
        else:
            worker_requests = account_data.get('workersDaily', [{}])[0].get('sum', {}).get('requests', 0)
            worker_limit = 100000
            worker_label = "Daily Invocations"
            
        # Parse Storage
        storage_bytes = 0
        r2_storage = account_data.get('r2StorageAdaptiveGroups', [])
        if r2_storage:
            max_vals = r2_storage[0].get('max', {})
            storage_bytes = max_vals.get('metadataSize', 0) + max_vals.get('payloadSize', 0)

        # Calculate Costs
        storage_gb = storage_bytes / (1024**3)
        storage_cost = max(0, storage_gb - 10) * 0.015
        class_a_cost = (max(0, r2_class_a - 1000000) / 1000000) * 4.50
        class_b_cost = (max(0, r2_class_b - 10000000) / 1000000) * 0.36
        
        # Worker cost (Paid plan only, $0.30 per million overage)
        worker_cost = 0.0
        if worker_plan == "paid":
            worker_cost = (max(0, worker_requests - 10000000) / 1000000) * 0.30

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
            "worker_cost": round(worker_cost, 2),
            "total_overage": round(storage_cost + class_a_cost + class_b_cost + worker_cost, 2)
        }

    except requests.exceptions.RequestException as e:
        return {"error": f"API Request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Failed to parse data: {str(e)}"}

def get_cloudflare_billing():
    config = load_config()
    account_id = config.get('cloudflare_account_id')
    api_token = config.get('cloudflare_api_token')

    if not account_id or not api_token:
        return {"error": "Missing Cloudflare Account ID or API Token"}

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }

    billing_info = {
        "last_payment": "-",
        "next_billing_date": "-",
        "error": None
    }

    try:
        sub_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/subscriptions"
        sub_res = requests.get(sub_url, headers=headers)
        if sub_res.status_code == 200:
            sub_data = sub_res.json()
            if sub_data.get("success") and sub_data.get("result"):
                subs = sub_data["result"]
                for sub in subs:
                    current_period_end = sub.get("current_period_end")
                    if current_period_end:
                        try:
                            if isinstance(current_period_end, (int, float)):
                                dt = datetime.datetime.fromtimestamp(current_period_end / 1000.0)
                            else:
                                dt = datetime.datetime.fromisoformat(current_period_end.replace("Z", "+00:00"))
                            billing_info["next_billing_date"] = dt.strftime("%d %b %Y")
                            break
                        except: pass
        
        history_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/billing/history"
        history_res = requests.get(history_url, headers=headers)
        if history_res.status_code == 200:
            history_data = history_res.json()
            if history_data.get("success") and history_data.get("result"):
                transactions = history_data["result"]
                for txn in transactions:
                    txn_action = txn.get("action", "").lower()
                    if txn_action in ["charge", "payment"]:
                        billing_info["last_payment"] = f"{txn.get('amount', 0)} {txn.get('currency', 'USD')}"
                        break
    except:
        billing_info["error"] = "Billing: Read error"

    return billing_info
