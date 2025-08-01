# payrex_api.py
import requests
import os
from dotenv import load_dotenv

# Load .env.local
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env.local')
load_dotenv(dotenv_path)

# Get your secret key from env
PAYREX_SECRET_KEY = os.getenv('PAYREX_SECRET_KEY')

# Toggle test mode
TEST_MODE = True  # ✅ Set to False when you're ready to go live

# Real or simulated balance endpoint
BASE_URL = 'https://api.payrex.ph'
BALANCE_ENDPOINT = f"{BASE_URL}/v1/wallet"

def get_balance():
    if TEST_MODE:
        # ✅ Simulate balance in test mode
        print("[Payrex] Using simulated balance")
        return {"balance": 999}  # Replace with any fake test value

    # ✅ Real API call (live only)
    headers = {
        "Authorization": f"Bearer {PAYREX_SECRET_KEY}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(BALANCE_ENDPOINT, headers=headers)
        response.raise_for_status()
        data = response.json()
        return {
            "balance": data.get("balance", "unknown")
        }
    except requests.RequestException as e:
        print(f"[Payrex API error] {e}")
        return None
