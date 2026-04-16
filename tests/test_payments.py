"""
NotesSphere Payment System Tests
Tests for: Payment packages, order creation, payment verification
"""
import pytest
import requests
import os

# Get backend URL from environment
BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL', '')
if not BASE_URL:
    try:
        with open('/app/frontend/.env', 'r') as f:
            for line in f:
                if 'EXPO_PUBLIC_BACKEND_URL' in line:
                    BASE_URL = line.split('=')[1].strip().strip('"')
                    break
    except:
        pass

BASE_URL = BASE_URL.rstrip('/')
if not BASE_URL:
    pytest.skip("Backend URL not found", allow_module_level=True)

# Test credentials
ADMIN_EMAIL = "admin@notessphere.com"
ADMIN_PASSWORD = "Admin@123"

# Global variables
admin_token = None
initial_wallet_balance = 0
test_order_id = None


class TestPaymentPackages:
    """Test payment packages endpoint"""

    def test_login_admin(self):
        """Login as admin to get token"""
        global admin_token
        payload = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        response = requests.post(f"{BASE_URL}/api/auth/login", json=payload)
        assert response.status_code == 200, f"Login failed: {response.text}"
        data = response.json()
        assert "access_token" in data
        admin_token = data["access_token"]
        print(f"✓ Admin logged in")

    def test_get_packages(self):
        """Test GET /api/payments/packages returns 4 packages"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/payments/packages", headers=headers)
        assert response.status_code == 200, f"Get packages failed: {response.text}"
        
        data = response.json()
        assert "packages" in data
        assert len(data["packages"]) == 4, f"Expected 4 packages, got {len(data['packages'])}"
        
        # Verify package structure
        package_ids = [p["id"] for p in data["packages"]]
        assert "pack_50" in package_ids
        assert "pack_100" in package_ids
        assert "pack_250" in package_ids
        assert "pack_500" in package_ids
        
        # Verify pack_100 details
        pack_100 = next(p for p in data["packages"] if p["id"] == "pack_100")
        assert pack_100["tokens"] == 100
        assert pack_100["price"] == 89
        assert pack_100["popular"] == True
        
        print(f"✓ Packages endpoint returned 4 packages: {package_ids}")


class TestPaymentFlow:
    """Test complete payment flow: create order → verify → check wallet"""

    def test_get_initial_wallet_balance(self):
        """Get initial wallet balance before purchase"""
        global initial_wallet_balance
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        initial_wallet_balance = data["tokens"]
        print(f"✓ Initial wallet balance: {initial_wallet_balance} tokens")

    def test_create_order_pack_100(self):
        """Test POST /api/payments/create-order with pack_100"""
        global test_order_id
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {"package_id": "pack_100"}
        response = requests.post(f"{BASE_URL}/api/payments/create-order", json=payload, headers=headers)
        assert response.status_code == 200, f"Create order failed: {response.text}"
        
        data = response.json()
        assert "order_id" in data
        assert "amount" in data
        assert "currency" in data
        assert "demo_mode" in data
        
        # Verify demo mode is active (since RAZORPAY_KEY_ID not configured)
        assert data["demo_mode"] == True, "Expected demo mode to be active"
        assert data["order_id"].startswith("order_demo_"), f"Expected demo order_id, got {data['order_id']}"
        assert data["amount"] == 8900  # 89 * 100
        assert data["currency"] == "INR"
        
        test_order_id = data["order_id"]
        print(f"✓ Demo order created: {test_order_id}")

    def test_verify_payment(self):
        """Test POST /api/payments/verify credits tokens"""
        if not admin_token or not test_order_id:
            pytest.skip("Admin token or order_id not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {
            "order_id": test_order_id,
            "payment_id": "demo_payment",
            "signature": "demo_signature"
        }
        response = requests.post(f"{BASE_URL}/api/payments/verify", json=payload, headers=headers)
        assert response.status_code == 200, f"Verify payment failed: {response.text}"
        
        data = response.json()
        assert "message" in data
        assert "tokens_added" in data
        assert data["tokens_added"] == 100
        
        print(f"✓ Payment verified, {data['tokens_added']} tokens credited")

    def test_wallet_balance_increased(self):
        """Test wallet balance increased by 100 tokens"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        new_balance = data["tokens"]
        expected_balance = initial_wallet_balance + 100
        
        assert new_balance == expected_balance, f"Expected {expected_balance} tokens, got {new_balance}"
        print(f"✓ Wallet balance increased: {initial_wallet_balance} → {new_balance} (+100)")

    def test_transaction_history_updated(self):
        """Test transaction history shows purchase"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/transactions", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "transactions" in data
        
        # Find the purchase transaction
        purchase_txn = None
        for txn in data["transactions"]:
            if "Purchased 100 tokens" in txn.get("reason", ""):
                purchase_txn = txn
                break
        
        assert purchase_txn is not None, "Purchase transaction not found in history"
        assert purchase_txn["amount"] == 100
        assert purchase_txn["type"] == "earn"
        
        print(f"✓ Transaction history updated with purchase record")

    def test_verify_already_processed_order(self):
        """Test verifying same order again returns already processed"""
        if not admin_token or not test_order_id:
            pytest.skip("Admin token or order_id not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {
            "order_id": test_order_id,
            "payment_id": "demo_payment",
            "signature": "demo_signature"
        }
        response = requests.post(f"{BASE_URL}/api/payments/verify", json=payload, headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "Already processed" in data["message"]
        print(f"✓ Duplicate verification handled correctly")


class TestPaymentEdgeCases:
    """Test edge cases and error handling"""

    def test_create_order_invalid_package(self):
        """Test creating order with invalid package_id"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {"package_id": "pack_invalid"}
        response = requests.post(f"{BASE_URL}/api/payments/create-order", json=payload, headers=headers)
        assert response.status_code == 400
        
        data = response.json()
        assert "Invalid package" in data["detail"]
        print(f"✓ Invalid package rejected with 400")

    def test_verify_nonexistent_order(self):
        """Test verifying non-existent order"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {
            "order_id": "order_demo_nonexistent",
            "payment_id": "demo",
            "signature": "demo"
        }
        response = requests.post(f"{BASE_URL}/api/payments/verify", json=payload, headers=headers)
        assert response.status_code == 404
        
        data = response.json()
        assert "Order not found" in data["detail"]
        print(f"✓ Non-existent order rejected with 404")

    def test_packages_requires_auth(self):
        """Test packages endpoint requires authentication"""
        response = requests.get(f"{BASE_URL}/api/payments/packages")
        assert response.status_code == 401
        print(f"✓ Packages endpoint requires authentication")
