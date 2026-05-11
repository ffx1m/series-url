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
                # Strictly check for Worker paid plans, do not trigger on 'r2_paid'
                if "workers_paid" in rate_plan or "workers_unbound" in rate_plan:
                    worker_plan = "paid"
                    break
    except:
        pass

    # 2. Get GraphQL Stats for the current month
    now = datetime.datetime.utcnow()
    # Use 1st of current month as approximate billing cycle start
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    datetime_geq = first_of_month.strftime("%Y-%m-%dT%H:%M:%SZ")

    url = "https://api.cloudflare.com/client/v4/graphql"
    query = """
    query GetStats($accountTag: string, $datetimeGeq: string, $bucketName: string) {
      viewer {
        accounts(filter: {accountTag: $accountTag}) {
          # R2 Operations (Requests)
          r2OperationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $datetimeGeq, bucketName: $bucketName}) {
            dimensions {
              actionType
            }
            sum {
              requests
            }
          }
          # R2 Storage (Peak in the selected period)
          r2StorageAdaptiveGroups(limit: 1, filter: {datetime_geq: $datetimeGeq, bucketName: $bucketName}) {
            max {
              metadataSize
              payloadSize
            }
          }
          # Workers Invocations (Requests)
          workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $datetimeGeq}) {
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
        "datetimeGeq": datetime_geq,
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
        
        # Parse R2 Requests by Class
        class_a_types = ['PutObject', 'CopyObject', 'CompleteMultipartUpload', 'CreateMultipartUpload', 'UploadPart', 'UploadPartCopy', 'ListObjects', 'ListBuckets', 'CreateBucket']
        class_b_types = ['GetObject', 'HeadObject', 'HeadBucket']
        
        r2_class_a = 0
        r2_class_b = 0
        r2_ops = account_data.get('r2OperationsAdaptiveGroups', [])
        for op in r2_ops:
            action = op.get('dimensions', {}).get('actionType', '')
            reqs = op.get('sum', {}).get('requests', 0)
            if action in class_a_types:
                r2_class_a += reqs
            elif action in class_b_types:
                r2_class_b += reqs
            
        # Parse Worker Requests
        worker_requests = 0
        worker_invocations = account_data.get('workersInvocationsAdaptive', [])
        if worker_invocations and len(worker_invocations) > 0:
            worker_requests = worker_invocations[0].get('sum', {}).get('requests', 0)
            
        # Parse R2 Storage Size
        storage_bytes = 0
        r2_storage = account_data.get('r2StorageAdaptiveGroups', [])
        if r2_storage and len(r2_storage) > 0:
            max_vals = r2_storage[0].get('max', {})
            storage_bytes = max_vals.get('metadataSize', 0) + max_vals.get('payloadSize', 0)

        # Calculate Quotas and Overages
        storage_gb = storage_bytes / (1024**3)
        storage_limit = 10.0
        storage_overage = max(0, storage_gb - storage_limit)
        storage_cost = storage_overage * 0.015

        class_a_limit = 1000000
        class_a_overage = max(0, r2_class_a - class_a_limit)
        class_a_cost = (class_a_overage / 1000000) * 4.50

        class_b_limit = 10000000
        class_b_overage = max(0, r2_class_b - class_b_limit)
        class_b_cost = (class_b_overage / 1000000) * 0.36

        worker_limit = 10000000 if worker_plan == "paid" else 100000
        worker_overage = max(0, worker_requests - worker_limit)
        worker_cost = (worker_overage / 1000000) * 0.30 if worker_plan == "paid" else 0.0

        total_est_overage = storage_cost + class_a_cost + class_b_cost + worker_cost

        return {
            "worker_plan": worker_plan.upper(),
            "storage_gb": round(storage_gb, 2),
            "storage_limit": storage_limit,
            "storage_cost": round(storage_cost, 2),
            
            "r2_class_a": r2_class_a,
            "class_a_limit": class_a_limit,
            "class_a_cost": round(class_a_cost, 2),
            
            "r2_class_b": r2_class_b,
            "class_b_limit": class_b_limit,
            "class_b_cost": round(class_b_cost, 2),
            
            "worker_requests": worker_requests,
            "worker_limit": worker_limit,
            "worker_cost": round(worker_cost, 2),
            
            "total_est_overage": round(total_est_overage, 2)
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
        # 1. Fetch Subscriptions to get next billing date (current_period_end)
        sub_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/subscriptions"
        sub_res = requests.get(sub_url, headers=headers)
        
        if sub_res.status_code == 200:
            sub_data = sub_res.json()
            if sub_data.get("success") and sub_data.get("result"):
                subs = sub_data["result"]
                for sub in subs:
                    # Usually Cloudflare sets 'current_period_end' in milliseconds or ISO string
                    current_period_end = sub.get("current_period_end")
                    if current_period_end:
                        # Find the furthest date in all active subscriptions
                        try:
                            # It's usually a timestamp in milliseconds
                            if isinstance(current_period_end, (int, float)):
                                dt = datetime.datetime.fromtimestamp(current_period_end / 1000.0)
                            else:
                                dt = datetime.datetime.fromisoformat(current_period_end.replace("Z", "+00:00"))
                            
                            formatted_date = dt.strftime("%d %b %Y")
                            billing_info["next_billing_date"] = formatted_date
                            break # Found a valid date, break
                        except:
                            pass
        
        # 2. Fetch Billing History to get the last paid invoice amount
        history_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/billing/history"
        history_res = requests.get(history_url, headers=headers)
        
        if history_res.status_code == 200:
            history_data = history_res.json()
            if history_data.get("success") and history_data.get("result"):
                transactions = history_data["result"]
                for txn in transactions:
                    txn_action = txn.get("action", "").lower()
                    txn_type = txn.get("type", "").lower()
                    
                    # We look for a charge or payment
                    if txn_action in ["charge", "payment"] or txn_type in ["charge", "payment"]:
                        amount = txn.get("amount", 0)
                        currency = txn.get("currency", "USD")
                        billing_info["last_payment"] = f"{amount} {currency}"
                        break

    except Exception as e:
        billing_info["error"] = "ไม่มีสิทธิ์ (Billing: Read) หรือดึงข้อมูลล้มเหลว"

    if billing_info["last_payment"] == "-" and billing_info["next_billing_date"] == "-" and not billing_info["error"]:
        billing_info["error"] = "ไม่พบประวัติบิล หรือ Token ไม่มีสิทธิ์เข้าถึงข้อมูล Billing"

    return billing_info
