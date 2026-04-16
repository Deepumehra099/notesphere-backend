"""
NotesSphere Iteration 3 Backend Tests
Tests for: Upload, Unlock, Download, AI Chat, User Chat
"""
import pytest
import requests
import os
import io
from datetime import datetime

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
STUDENT_EMAIL = "student@test.com"
STUDENT_PASSWORD = "test123"

# Global variables
admin_token = None
student_token = None
uploaded_note_id = None
uploaded_file_url = None
test_chat_id = None
ai_conversation_id = None


class TestAuth:
    """Login to get tokens"""

    def test_login_admin(self):
        """Login as admin"""
        global admin_token
        payload = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        response = requests.post(f"{BASE_URL}/api/auth/login", json=payload)
        assert response.status_code == 200, f"Admin login failed: {response.text}"
        data = response.json()
        assert "access_token" in data
        admin_token = data["access_token"]
        print(f"✓ Admin logged in")

    def test_login_student(self):
        """Login as student"""
        global student_token
        payload = {"email": STUDENT_EMAIL, "password": STUDENT_PASSWORD}
        response = requests.post(f"{BASE_URL}/api/auth/login", json=payload)
        assert response.status_code == 200, f"Student login failed: {response.text}"
        data = response.json()
        assert "access_token" in data
        student_token = data["access_token"]
        print(f"✓ Student logged in")


class TestNoteUpload:
    """Test note upload flow"""

    def test_upload_pdf_note(self):
        """Test POST /api/notes/upload with PDF file"""
        global uploaded_note_id, uploaded_file_url
        if not admin_token:
            pytest.skip("Admin token not available")

        # Create a dummy PDF file
        pdf_content = b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n2 0 obj\n<<\n/Type /Pages\n/Kids [3 0 R]\n/Count 1\n>>\nendobj\n3 0 obj\n<<\n/Type /Page\n/Parent 2 0 R\n/Resources <<\n/Font <<\n/F1 <<\n/Type /Font\n/Subtype /Type1\n/BaseFont /Helvetica\n>>\n>>\n>>\n/MediaBox [0 0 612 792]\n/Contents 4 0 R\n>>\nendobj\n4 0 obj\n<<\n/Length 44\n>>\nstream\nBT\n/F1 12 Tf\n100 700 Td\n(Test PDF) Tj\nET\nendstream\nendobj\nxref\n0 5\n0000000000 65535 f\n0000000009 00000 n\n0000000058 00000 n\n0000000115 00000 n\n0000000317 00000 n\ntrailer\n<<\n/Size 5\n/Root 1 0 R\n>>\nstartxref\n408\n%%EOF"
        
        files = {"file": ("test_upload.pdf", io.BytesIO(pdf_content), "application/pdf")}
        data = {
            "title": "TEST_Uploaded_Note_Iteration3",
            "description": "Test note uploaded via pytest",
            "subject": "Computer Science",
            "topic": "Testing",
            "unlock_cost": "15"
        }
        headers = {"Authorization": f"Bearer {admin_token}"}
        
        response = requests.post(
            f"{BASE_URL}/api/notes/upload",
            files=files,
            data=data,
            headers=headers
        )
        assert response.status_code == 200, f"Upload failed: {response.text}"
        
        result = response.json()
        assert "note" in result
        assert "message" in result
        assert result["note"]["title"] == "TEST_Uploaded_Note_Iteration3"
        assert result["note"]["status"] == "pending"
        assert "file_url" in result["note"]
        assert result["note"]["file_url"].startswith("/api/uploads/")
        
        uploaded_note_id = result["note"]["id"]
        uploaded_file_url = result["note"]["file_url"]
        print(f"✓ Note uploaded: {uploaded_note_id}, file_url: {uploaded_file_url}")

    def test_uploaded_file_accessible(self):
        """Test GET /api/uploads/{filename} returns HTTP 200"""
        if not uploaded_file_url:
            pytest.skip("No uploaded file URL")
        
        # Extract filename from file_url
        filename = uploaded_file_url.split("/")[-1]
        response = requests.get(f"{BASE_URL}/api/uploads/{filename}")
        assert response.status_code == 200, f"File not accessible: {response.status_code}"
        assert len(response.content) > 0, "File content is empty"
        print(f"✓ Uploaded file accessible via GET {uploaded_file_url}")

    def test_approve_uploaded_note(self):
        """Approve the uploaded note (admin action)"""
        if not uploaded_note_id or not admin_token:
            pytest.skip("No uploaded note or admin token")
        
        # Directly update DB to approve (no admin approve endpoint in routes)
        import sys
        sys.path.insert(0, '/app/backend')
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path('/app/backend/.env'))
        
        from utils.db import get_db
        from bson import ObjectId
        import asyncio
        
        async def approve():
            db = get_db()
            await db.notes.update_one(
                {"_id": ObjectId(uploaded_note_id)},
                {"$set": {"status": "approved"}}
            )
        
        asyncio.run(approve())
        print(f"✓ Note {uploaded_note_id} approved")


class TestNoteUnlock:
    """Test note unlock flow"""

    def test_get_student_initial_balance(self):
        """Get student's initial token balance"""
        global student_initial_balance
        if not student_token:
            pytest.skip("Student token not available")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        assert response.status_code == 200
        data = response.json()
        student_initial_balance = data["tokens"]
        print(f"✓ Student initial balance: {student_initial_balance} tokens")

    def test_unlock_note_deducts_tokens(self):
        """Test POST /api/notes/{id}/unlock deducts tokens from buyer"""
        if not uploaded_note_id or not student_token:
            pytest.skip("No uploaded note or student token")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        response = requests.post(f"{BASE_URL}/api/notes/{uploaded_note_id}/unlock", headers=headers)
        assert response.status_code == 200, f"Unlock failed: {response.text}"
        
        data = response.json()
        assert "message" in data
        print(f"✓ Note unlocked: {data['message']}")
        
        # Verify tokens deducted
        wallet_response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        assert wallet_response.status_code == 200
        wallet_data = wallet_response.json()
        new_balance = wallet_data["tokens"]
        
        # Should have deducted 15 tokens (unlock_cost)
        expected_balance = student_initial_balance - 15
        assert new_balance == expected_balance, f"Expected {expected_balance} tokens, got {new_balance}"
        print(f"✓ Tokens deducted: {student_initial_balance} → {new_balance} (-15)")

    def test_unlock_already_unlocked(self):
        """Test unlocking already unlocked note returns message"""
        if not uploaded_note_id or not student_token:
            pytest.skip("No uploaded note or student token")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        response = requests.post(f"{BASE_URL}/api/notes/{uploaded_note_id}/unlock", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert "Already unlocked" in data["message"]
        print(f"✓ Already unlocked message returned")


class TestNoteDownload:
    """Test note download flow"""

    def test_download_unlocked_note(self):
        """Test POST /api/notes/{id}/download returns file_url for unlocked notes"""
        if not uploaded_note_id or not student_token:
            pytest.skip("No uploaded note or student token")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        response = requests.post(f"{BASE_URL}/api/notes/{uploaded_note_id}/download", headers=headers)
        assert response.status_code == 200, f"Download failed: {response.text}"
        
        data = response.json()
        assert "file_url" in data
        assert "file_name" in data
        assert data["file_url"] == uploaded_file_url
        print(f"✓ Download returned file_url: {data['file_url']}")

    def test_download_not_unlocked_note(self):
        """Test downloading non-unlocked note returns 403"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        # Get a note that student hasn't unlocked (use a seeded note)
        headers_student = {"Authorization": f"Bearer {student_token}"}
        feed_response = requests.get(f"{BASE_URL}/api/notes/feed", headers=headers_student)
        assert feed_response.status_code == 200
        notes = feed_response.json()["notes"]
        
        # Find a note that's not unlocked
        locked_note = None
        for note in notes:
            if not note["is_unlocked"] and note["id"] != uploaded_note_id:
                locked_note = note
                break
        
        if not locked_note:
            pytest.skip("No locked notes available for testing")
        
        # Try to download without unlocking
        response = requests.post(f"{BASE_URL}/api/notes/{locked_note['id']}/download", headers=headers_student)
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        print(f"✓ Download blocked for locked note (403)")


class TestPayments:
    """Test payment flow (already tested in iteration 2, quick verification)"""

    def test_create_order_demo_mode(self):
        """Test POST /api/payments/create-order creates demo order"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        headers = {"Authorization": f"Bearer {admin_token}"}
        payload = {"package_id": "pack_50"}
        response = requests.post(f"{BASE_URL}/api/payments/create-order", json=payload, headers=headers)
        assert response.status_code == 200, f"Create order failed: {response.text}"
        
        data = response.json()
        assert "order_id" in data
        assert data["demo_mode"] == True
        assert data["order_id"].startswith("order_demo_")
        print(f"✓ Demo order created: {data['order_id']}")

    def test_verify_payment_credits_tokens(self):
        """Test POST /api/payments/verify credits tokens correctly"""
        if not admin_token:
            pytest.skip("Admin token not available")
        
        # Create order first
        headers = {"Authorization": f"Bearer {admin_token}"}
        create_response = requests.post(
            f"{BASE_URL}/api/payments/create-order",
            json={"package_id": "pack_50"},
            headers=headers
        )
        assert create_response.status_code == 200
        order_id = create_response.json()["order_id"]
        
        # Get initial balance
        wallet_response = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        initial_balance = wallet_response.json()["tokens"]
        
        # Verify payment
        verify_response = requests.post(
            f"{BASE_URL}/api/payments/verify",
            json={"order_id": order_id, "payment_id": "demo", "signature": "demo"},
            headers=headers
        )
        assert verify_response.status_code == 200
        data = verify_response.json()
        assert data["tokens_added"] == 50
        
        # Check balance increased
        wallet_response2 = requests.get(f"{BASE_URL}/api/tokens/wallet", headers=headers)
        new_balance = wallet_response2.json()["tokens"]
        assert new_balance == initial_balance + 50
        print(f"✓ Payment verified, 50 tokens credited: {initial_balance} → {new_balance}")


class TestAIChat:
    """Test AI chat functionality"""

    def test_ai_chat_returns_response(self):
        """Test POST /api/ai/chat returns real AI response"""
        global ai_conversation_id
        if not student_token:
            pytest.skip("Student token not available")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        payload = {"message": "What is Python?", "conversation_id": ""}
        
        response = requests.post(f"{BASE_URL}/api/ai/chat", json=payload, headers=headers)
        assert response.status_code == 200, f"AI chat failed: {response.text}"
        
        data = response.json()
        assert "response" in data
        assert "conversation_id" in data
        assert len(data["response"]) > 0, "AI response is empty"
        assert "python" in data["response"].lower(), "AI response doesn't mention Python"
        
        ai_conversation_id = data["conversation_id"]
        print(f"✓ AI chat returned response: {data['response'][:100]}...")

    def test_ai_chat_with_conversation_id(self):
        """Test AI chat maintains conversation history"""
        if not student_token or not ai_conversation_id:
            pytest.skip("Student token or conversation_id not available")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        payload = {"message": "Give me a simple example", "conversation_id": ai_conversation_id}
        
        response = requests.post(f"{BASE_URL}/api/ai/chat", json=payload, headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "response" in data
        assert len(data["response"]) > 0
        print(f"✓ AI chat with history returned response: {data['response'][:100]}...")


class TestUserChat:
    """Test user-to-user chat"""

    def test_send_message_creates_chat(self):
        """Test POST /api/chat/send creates message between users"""
        global test_chat_id
        if not admin_token or not student_token:
            pytest.skip("Tokens not available")
        
        # Get student user ID
        headers_student = {"Authorization": f"Bearer {student_token}"}
        me_response = requests.get(f"{BASE_URL}/api/auth/me", headers=headers_student)
        student_id = me_response.json()["user"]["id"]
        
        # Admin sends message to student
        headers_admin = {"Authorization": f"Bearer {admin_token}"}
        payload = {"receiver_id": student_id, "text": "TEST: Hello from admin"}
        
        response = requests.post(f"{BASE_URL}/api/chat/send", json=payload, headers=headers_admin)
        assert response.status_code == 200, f"Send message failed: {response.text}"
        
        data = response.json()
        assert "message" in data
        assert "chat_id" in data
        
        test_chat_id = data["chat_id"]
        print(f"✓ Message sent, chat_id: {test_chat_id}")

    def test_get_chat_rooms(self):
        """Test GET /api/chat/rooms returns chat list"""
        if not student_token:
            pytest.skip("Student token not available")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        response = requests.get(f"{BASE_URL}/api/chat/rooms", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "rooms" in data
        assert len(data["rooms"]) > 0, "No chat rooms found"
        print(f"✓ Chat rooms returned: {len(data['rooms'])} rooms")

    def test_get_chat_messages(self):
        """Test GET /api/chat/messages/{chat_id} returns messages"""
        if not student_token or not test_chat_id:
            pytest.skip("Student token or chat_id not available")
        
        headers = {"Authorization": f"Bearer {student_token}"}
        response = requests.get(f"{BASE_URL}/api/chat/messages/{test_chat_id}", headers=headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "messages" in data
        assert len(data["messages"]) > 0, "No messages found"
        
        # Verify our test message is there
        found_test_msg = any("TEST: Hello from admin" in msg.get("text", "") for msg in data["messages"])
        assert found_test_msg, "Test message not found in chat history"
        print(f"✓ Chat messages returned: {len(data['messages'])} messages")


class TestCleanup:
    """Cleanup test data"""

    def test_cleanup_uploaded_note(self):
        """Delete uploaded test note"""
        if not uploaded_note_id:
            pytest.skip("No uploaded note to clean up")
        
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
            # Delete note
            await db.notes.delete_one({"_id": ObjectId(uploaded_note_id)})
            # Delete transactions related to this note
            await db.transactions.delete_many({"reason": {"$regex": "TEST_Uploaded_Note_Iteration3"}})
            # Delete AI conversation
            if ai_conversation_id:
                await db.ai_conversations.delete_one({"conversation_id": ai_conversation_id})
            # Delete chat messages
            if test_chat_id:
                await db.messages.delete_many({"chat_id": test_chat_id})
                await db.chats.delete_one({"_id": ObjectId(test_chat_id)})
        
        asyncio.run(cleanup())
        print(f"✓ Cleaned up test data: note {uploaded_note_id}")
