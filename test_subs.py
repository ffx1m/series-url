import json
from utils.r2 import load_config
import requests

config = load_config()
account_id = config.get('cloudflare_account_id')
api_token = config.get('cloudflare_api_token')

headers = {
    "Authorization": f"Bearer {api_token}",
    "Content-Type": "application/json"
}

sub_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/subscriptions"
res = requests.get(sub_url, headers=headers)
print(json.dumps(res.json(), indent=2))
