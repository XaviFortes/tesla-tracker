import hashlib
import base64
import os
import random
import string
import httpx
from urllib.parse import urlparse, parse_qs

def generate_code_verifier_and_challenge():
    # standard PKCE verifier generation
    code_verifier = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(86))
    code_challenge = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(code_challenge).decode('utf-8').rstrip('=')
    return code_verifier, code_challenge

def get_auth_code(code_challenge):
    base_url = "https://auth.tesla.com/oauth2/v3/authorize"
    params = {
        "client_id": "ownerapi",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": "https://auth.tesla.com/void/callback",
        "response_type": "code",
        "scope": "openid email offline_access",
        "state": "123"
    }
    
    # Build complete URL
    req = httpx.Request("GET", base_url, params=params)
    print("\n--- Step 1: Login ---")
    print("1. Open this URL in your browser:")
    print(f"\n{req.url}\n")
    print("2. Log in with your Tesla account.")
    print("3. You will be redirected to a 'Page Not Found' (https://auth.tesla.com/void/callback...).")
    print("4. Copy the full URL from your browser address bar and paste it below.")
    
    url_input = input("\nPaste Redirected URL: ").strip()
    
    try:
        parsed = urlparse(url_input)
        query_params = parse_qs(parsed.query)
        if 'code' not in query_params:
            print("Error: URL does not contain an authorization code.")
            exit(1)
        return query_params['code'][0]
    except Exception as e:
        print(f"Error parsing URL: {e}")
        exit(1)

def exchange_code_for_tokens(auth_code, code_verifier):
    url = "https://auth.tesla.com/oauth2/v3/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": "ownerapi",
        "code": auth_code,
        "code_verifier": code_verifier,
        "redirect_uri": "https://auth.tesla.com/void/callback"
    }
    
    print("\n--- Step 2: Exchanging Code for Tokens ---")
    try:
        with httpx.Client() as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        print(f"HTTP Error exchanging tokens: {e.response.text}")
        exit(1)
    except Exception as e:
        print(f"Error exchanging tokens: {e}")
        exit(1)

if __name__ == "__main__":
    print("--- Tesla Auth Helper ---")
    # Generate PKCE
    code_verifier, code_challenge = generate_code_verifier_and_challenge()
    
    # Get Auth Code
    auth_code = get_auth_code(code_challenge)
    
    # Exchange for Tokens
    tokens = exchange_code_for_tokens(auth_code, code_verifier)
    
    print("\nSUCCESS! =========================================")
    print("Save these values for your Kubernetes Secret (k8s/secrets.yaml):")
    print(f"\nRefresh Token:\n{tokens['refresh_token']}")
    print(f"\nAccess Token (valid for 8h, use if needed immediately):\n{tokens['access_token']}")
    print("\n==================================================")