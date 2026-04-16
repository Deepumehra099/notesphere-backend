"""
NotesSphere Backend API Tests
Tests for: Auth, Notes, Tokens, Search, Admin endpoints
"""
import pytest
import requests
import os
from datetime import datetime

# Get backend URL from environment
# Try multiple sources for the backend URL
BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL', '')
if not BASE_URL:
    # Read from frontend .env file
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
TEST_USER_EMAIL = f"test_{int(datetime.now().timestamp())}@example.com"
TEST_USER_PASSWORD = "test123"

# Global variables for tokens
admin_token = None
test_user_token = None
test_user_id = None


class TestHealth:
    """Health check endpoint"""

    def test_health_endpoint(self):
        """Test /api/health returns 200"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        print("✓ Health check passed")


class TestAuth:
    """Authentication endpoints"""

    def test_register_new_user(self):
        """Test POST /api/auth/register creates new user"""
        global test_user_token, test_user_id
        payload = {
            "name": "Test User",
            "email": TEST_USER_EMAIL,
            "password": TEST_USER_PASSWORD,
            "branch": "CS",
            "semester": 3
        }
        response = requests.post(f"{BASE_URL}/api/auth/register", json=payload)
        assert response.status_code == 200, f"Register failed: {response.text}"
        
        data = response.json()
        assert "access_token" in data
        assert "user" in data
        assert data["user"]["email"] == TEST_USER_EMAIL.lower()
        assert data["user"]["tokens"] == 100  # Welcome bonus
        
        test_user_token = data["access_token"]
        test_user_id = data["user"]["id"]
        print(f"✓ User registered: {TEST_USER_EMAIL}")

        # Verify user was persisted by fetching /me
        headers = {"Authorization": f"Bearer {test_user_token}"}
        me_response = requests.get(f"{BASE_URL}/api/auth/me", headers=headers)
        assert me_response.status_code == 200
        me_data = me_response.json()
        assert me_data["user"]["email"] == TEST_USER_EMAIL.lower()
        print("✓ User registration persisted in database")

    def test_login_admin(self):
        """Test POST /api/auth/login with admin credentials"""
        global admin_token
        payload = {
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        }
        response = requests.post(f"{BASE_URL}/api/auth/login", json=payload)
        assert response.status_code == 200, f"Admin login failed: {response.text}"
        
        data = response.json()
        assert "access_token" in data
        assert "user" in data
        assert data["user"]["email"] == ADMIN_EMAIL
        assert data["user"]["role"] == "admin"
        
        admin_token = data["access_token"]
        print(f"✓ Admin logged in: {ADMIN_EMAIL}")

    def test_get_me_with_token(self):
        """Test GET /api/auth/me with Bearer token"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "user" in data
        assert data["user"]["email"] == ADMIN_EMAIL
        assert data["user"]["role"] == "admin"
        print("✓ /api/auth/me returned user data")

    def test_login_invalid_credentials(self):
        """Test login with invalid credentials returns 401"""
        payload = {
            "email": "wrong@example.com",
            "password": "wrongpass"
        }
        response = requests.post(f"{BASE_URL}/api/auth/login", json=payload)
        assert response.status_code == 401
        print("✓ Invalid login rejected with 401")


class TestNotes:
    """Notes endpoints"""

    def test_get_notes_feed(self):
        """Test GET /api/notes/feed returns seeded notes"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/notes/feed", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "notes" in data
        assert "total" in data
        assert len(data["notes"]) > 0  # Should have seeded notes
        
        # Verify note structure
        note = data["notes"][0]
        assert "id" in note
        assert "title" in note
        assert "subject" in note
        assert "rating" in note
        assert "is_unlocked" in note
        print(f"✓ Notes feed returned {len(data['notes'])} notes")

    def test_notes_feed_with_filters(self):
        """Test notes feed with subject filter"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(
            f"{BASE_URL}/api/notes/feed",
            headers=headers,
            params={"subject": "Computer Science", "sort": "rating"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "notes" in data
        print(f"✓ Notes feed with filters returned {len(data['notes'])} notes")


class TestTokens:
    """Token wallet and transactions"""

    def test_get_wallet(self):
        """Test GET /api/tokens/wallet returns token balance"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "tokens" in data
        assert "xp" in data
        assert "streak" in data
        assert isinstance(data["tokens"], int)
        print(f"✓ Wallet returned: {data['tokens']} tokens, {data['xp']} XP")

    def test_get_transactions(self):
        """Test GET /api/tokens/transactions returns transaction list"""
        if not test_user_token:
            pytest.skip("Test user token not available")
        
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/transactions", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "transactions" in data
        assert "total" in data
        assert len(data["transactions"]) > 0  # Should have welcome bonus
        
        # Verify transaction structure
        txn = data["transactions"][0]
        assert "amount" in txn
        assert "type" in txn
        assert "reason" in txn
        print(f"✓ Transactions returned {len(data['transactions'])} records")


class TestSearch:
    """Search endpoints"""

    def test_search_notes_with_query(self):
        """Test GET /api/search/notes?q=data returns matching notes"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(
            f"{BASE_URL}/api/search/notes",
            headers=headers,
            params={"q": "data"}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "notes" in data
        assert "total" in data
        assert "subjects" in data
        print(f"✓ Search for 'data' returned {len(data['notes'])} notes")

    def test_search_suggestions(self):
        """Test GET /api/search/suggestions"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(
            f"{BASE_URL}/api/search/suggestions",
            headers=headers,
            params={"q": "cal"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "suggestions" in data
        print(f"✓ Search suggestions returned {len(data['suggestions'])} results")


class TestAdmin:
    """Admin-only endpoints"""

    def test_get_analytics_admin(self):
        """Test GET /api/admin/analytics returns stats (admin only)"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/admin/analytics", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "total_users" in data
        assert "total_notes" in data
        assert "pending_notes" in data
        assert "approved_notes" in data
        assert "total_transactions" in data
        assert data["total_users"] >= 1  # At least admin
        assert data["total_notes"] >= 6  # Seeded notes
        print(f"✓ Analytics: {data['total_users']} users, {data['total_notes']} notes")

    def test_analytics_non_admin_forbidden(self):
        """Test non-admin user cannot access analytics"""
        if not test_user_token:
            pytest.skip("Test user token not available")
        
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = requests.get(f"{BASE_URL}/api/admin/analytics", headers=headers)
        assert response.status_code == 403
        print("✓ Non-admin user blocked from analytics (403)")

    def test_get_all_users_admin(self):
        """Test GET /api/admin/users returns user list"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(f"{BASE_URL}/api/admin/users", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "users" in data
        assert len(data["users"]) >= 1
        print(f"✓ Admin users list returned {len(data['users'])} users")


class TestCleanup:
    """Cleanup test data"""

    def test_cleanup_test_user(self):
        """Delete test user created during tests"""
        if not test_user_id:
            pytest.skip("No test user to clean up")
        
        # Use MongoDB to delete test user
        import sys
        sys.path.insert(0, '/app/backend')
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path('/app/backend/.env'))
        
        from utils.db import get_db
        from bson import ObjectId
        import asyncio
        
        async def cleanup():
            db = get_db()
            await db.users.delete_one({"_id": ObjectId(test_user_id)})
            await db.transactions.delete_many({"user_id": test_user_id})
        
        asyncio.run(cleanup())
        print(f"✓ Cleaned up test user: {TEST_USER_EMAIL}")
