import asyncio
import unittest
from copy import deepcopy

from bson import ObjectId

from utils.credits import InsufficientCredits, apply_credit_change, qualify_referral


def matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(document, branch) for branch in expected):
                return False
            continue
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$exists" in expected and ((key in document) != expected["$exists"]):
                return False
            if "$gte" in expected and (actual is None or actual < expected["$gte"]):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = rows or []

    async def find_one(self, query, projection=None, session=None):
        row = next((item for item in self.rows if matches(item, query)), None)
        return deepcopy(row) if row else None

    async def update_one(self, query, update, session=None):
        row = next((item for item in self.rows if matches(item, query)), None)
        if not row:
            return type("Result", (), {"matched_count": 0, "modified_count": 0})()
        for key, value in update.get("$set", {}).items():
            row[key] = value
        for key in update.get("$inc", {}):
            row[key] = row.get(key, 0) + update["$inc"][key]
        return type("Result", (), {"matched_count": 1, "modified_count": 1})()

    async def find_one_and_update(self, query, update, return_document=None, projection=None, session=None):
        row = next((item for item in self.rows if matches(item, query)), None)
        if not row:
            return None
        for key, value in update.get("$set", {}).items():
            row[key] = value
        for key in update.get("$inc", {}):
            row[key] = row.get(key, 0) + update["$inc"][key]
        for key, value in update.get("$addToSet", {}).items():
            row.setdefault(key, [])
            if value not in row[key]:
                row[key].append(value)
        return deepcopy(row)

    async def insert_one(self, document, session=None):
        row = deepcopy(document)
        row.setdefault("_id", ObjectId())
        self.rows.append(row)
        return type("Result", (), {"inserted_id": row["_id"]})()


class FakeDB:
    def __init__(self, users, referrals=None):
        self.users = FakeCollection(users)
        self.credit_transactions = FakeCollection()
        self.referrals = FakeCollection(referrals or [])
        self.credit_settings = FakeCollection()


class CreditLedgerTests(unittest.TestCase):
    def test_spend_is_idempotent_and_never_goes_negative_or_changes_cash_wallet(self):
        user_id = str(ObjectId())
        user = {"_id": ObjectId(user_id), "credits_balance": 200, "tokens": 75}
        db = FakeDB([user])

        async def scenario():
            first = await apply_credit_change(
                db,
                user_id=user_id,
                amount=-150,
                transaction_type="CREDIT_SPEND",
                description="Premium note unlock",
                idempotency_key="purchase-1",
            )
            repeated = await apply_credit_change(
                db,
                user_id=user_id,
                amount=-150,
                transaction_type="CREDIT_SPEND",
                description="Premium note unlock",
                idempotency_key="purchase-1",
            )
            self.assertEqual(first["balance"], 50)
            self.assertTrue(repeated["already_applied"])
            self.assertEqual(user["credits_balance"], 50)
            self.assertEqual(user["tokens"], 75)
            self.assertEqual(len(db.credit_transactions.rows), 1)
            with self.assertRaises(InsufficientCredits):
                await apply_credit_change(
                    db,
                    user_id=user_id,
                    amount=-51,
                    transaction_type="CREDIT_SPEND",
                    description="Insufficient purchase",
                    idempotency_key="purchase-2",
                )
            self.assertEqual(user["credits_balance"], 50)
            self.assertEqual(len(db.credit_transactions.rows), 1)

        asyncio.run(scenario())

    def test_referral_rewards_are_granted_once_after_qualification(self):
        referrer_id = str(ObjectId())
        referred_id = str(ObjectId())
        referral_id = ObjectId()
        db = FakeDB(
            [
                {"_id": ObjectId(referrer_id), "credits_balance": 10},
                {"_id": ObjectId(referred_id), "credits_balance": 5},
            ],
            [{"_id": referral_id, "referrer_user_id": referrer_id, "referred_user_id": referred_id, "status": "registered"}],
        )

        async def scenario():
            self.assertTrue(await qualify_referral(db, referred_id, session=None))
            self.assertFalse(await qualify_referral(db, referred_id, session=None))
            self.assertEqual(db.users.rows[0]["credits_balance"], 60)
            self.assertEqual(db.users.rows[1]["credits_balance"], 30)
            self.assertEqual(db.referrals.rows[0]["status"], "rewarded")
            self.assertEqual(len(db.credit_transactions.rows), 2)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
