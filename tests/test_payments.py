"""
NotesSphere Payment System Tests
Tests for: Payment packages, order creation, payment verification
"""
import os

import pytest
import requests

# Get backend URL from environment
BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "")
if not BASE_URL:
    try:
        with open("/app/frontend/.env", "r") as f:
            for line in f:
                if "EXPO_PUBLIC_BACKEND_URL" in line:
                    BASE_URL = line.split("=")[1].strip().strip('"')
                    break
    except Exception:
        pass

BASE_URL = BASE_URL.rstrip("/")
if not BASE_URL:
    pytest.skip("Backend URL not found", allow_module_level=True)

# Test credentials
ADMIN_EMAIL = "admin@notessphere.com"
ADMIN_PASSWORD = "Admin@123"

# Global variables
admin_token = None
initial_wallet_balance = 0
test_order_id = None
payment_configured = bool(os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"))


class TestPaymentPackages:
    """Test payment packages endpoint"""

    def test_login_admin(self):
        global admin_token
        payload = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        response = requests.post(f"{BASE_URL}/api/auth/login", json=payload)
        assert response.status_code == 200, f"Login failed: {response.text}"
        data = response.json()
        assert "access_token" in data
        admin_token = data["access_token"]

    def test_get_packages(self):
        if not admin_token:
            pytest.skip("Admin token not available")

        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/payments/packages", headers=headers)
        assert response.status_code == 200, f"Get packages failed: {response.text}"

        data = response.json()
        assert "packages" in data
        assert len(data["packages"]) == 4

        package_ids = [p["id"] for p in data["packages"]]
        assert {"pack_50", "pack_100", "pack_250", "pack_500"}.issubset(set(package_ids))

        pack_100 = next(p for p in data["packages"] if p["id"] == "pack_100")
        assert pack_100["tokens"] == 100
        assert pack_100["price"] == 89
        assert pack_100["popular"] is True


class TestPaymentFlow:
    """Test secure payment order handling."""

    def test_get_initial_wallet_balance(self):
        global initial_wallet_balance
        if not admin_token:
            pytest.skip("Admin token not available")

        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        assert response.status_code == 200

        data = response.json()
        initial_wallet_balance = data["tokens"]

    def test_create_order_pack_100(self):
        global test_order_id
        if not admin_token:
            pytest.skip("Admin token not available")

        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.post(f"{BASE_URL}/api/payments/create-order", json={"package_id": "pack_100"}, headers=headers)

        if not payment_configured:
            assert response.status_code == 503
            assert "not configured" in response.json()["detail"].lower()
            pytest.skip("Razorpay not configured in this environment")

        assert response.status_code == 200, f"Create order failed: {response.text}"
        data = response.json()
        assert "order_id" in data
        assert "amount" in data
        assert "currency" in data
        assert "key_id" in data
        assert data["amount"] == 8900
        assert data["currency"] == "INR"

        test_order_id = data["order_id"]

    def test_verify_payment(self):
        if not payment_configured:
            pytest.skip("Razorpay not configured in this environment")
        if not admin_token or not test_order_id:
            pytest.skip("Admin token or order_id not available")

        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {
            "order_id": test_order_id,
            "payment_id": "demo_payment",
            "signature": "demo_signature",
        }
        response = requests.post(f"{BASE_URL}/api/payments/verify", json=payload, headers=headers)

        # Without a valid Razorpay signature, verification must fail.
        assert response.status_code == 400
        assert "verification failed" in response.json()["detail"].lower()

    def test_wallet_balance_unchanged_without_verified_payment(self):
        if not admin_token:
            pytest.skip("Admin token not available")

        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        assert response.status_code == 200

        data = response.json()
        assert data["tokens"] == initial_wallet_balance


class TestPaymentEdgeCases:
    """Test edge cases and error handling"""

    def test_create_order_invalid_package(self):
        if not admin_token:
            pytest.skip("Admin token not available")

        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.post(f"{BASE_URL}/api/payments/create-order", json={"package_id": "pack_invalid"}, headers=headers)
        assert response.status_code == 400
        assert "Invalid package" in response.json()["detail"]

    def test_verify_nonexistent_order(self):
        if not admin_token:
            pytest.skip("Admin token not available")

        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {
            "order_id": "order_nonexistent",
            "payment_id": "demo",
            "signature": "demo",
        }
        response = requests.post(f"{BASE_URL}/api/payments/verify", json=payload, headers=headers)
        assert response.status_code == 404
        assert "Order not found" in response.json()["detail"]

    def test_packages_requires_auth(self):
        response = requests.get(f"{BASE_URL}/api/payments/packages")
        assert response.status_code == 401
