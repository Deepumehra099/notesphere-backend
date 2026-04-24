const express = require("express");
const { verifyToken } = require("../middleware/auth");
const { rateLimit } = require("../middleware/rateLimit");
const Transaction = require("../models/Transaction");
const { buildWalletResponse, ensureWallet, requestWithdrawal } = require("../services/walletService");

const router = express.Router();

function parseAmount(value) {
  const amount = Number(value);
  return Number.isFinite(amount) ? Math.floor(amount) : NaN;
}

router.get("/", verifyToken, async (req, res) => {
  const wallet = await ensureWallet(req.user._id);
  return res.json(buildWalletResponse(wallet));
});

router.get("/wallet", verifyToken, async (req, res) => {
  const wallet = await ensureWallet(req.user._id);
  return res.json(buildWalletResponse(wallet));
});

router.get("/transactions", verifyToken, async (req, res) => {
  const page = Math.max(1, Number(req.query.page) || 1);
  const limit = Math.min(50, Math.max(1, Number(req.query.limit) || 10));
  const skip = (page - 1) * limit;

  const [items, total] = await Promise.all([
    Transaction.find({ userId: req.user._id }).sort({ createdAt: -1 }).skip(skip).limit(limit),
    Transaction.countDocuments({ userId: req.user._id }),
  ]);

  return res.json({
    transactions: items.map((item) => ({
      id: item._id.toString(),
      type: item.type,
      source: item.source,
      category: item.category,
      amount: item.amount,
      status: item.status,
      title: item.title,
      referenceId: item.referenceId,
      metadata: item.metadata || {},
      createdAt: item.createdAt,
      updatedAt: item.updatedAt,
    })),
    pagination: {
      page,
      limit,
      total,
      hasMore: skip + items.length < total,
    },
  });
});

router.post(
  "/withdraw",
  verifyToken,
  rateLimit({ keyPrefix: "wallet:withdraw", limit: 5, windowMs: 60 * 1000 }),
  async (req, res) => {
    const amount = parseAmount(req.body.amount);
    const upiId = String(req.body.upiId || req.body.upi || req.user.upiId || "").trim();

    if (!Number.isInteger(amount) || amount <= 0) {
      return res.status(400).json({ message: "Amount must be a positive integer" });
    }

    if (amount < 100) {
      return res.status(400).json({ message: "Minimum withdrawal is 100 tokens" });
    }

    if (!upiId) {
      return res.status(400).json({ message: "UPI ID is required for withdrawal" });
    }

    const { wallet, transaction } = await requestWithdrawal(req.user._id, amount, {
      requestedAt: new Date().toISOString(),
      upiId,
    });

    return res.status(201).json({
      message: "Withdrawal request created",
      wallet: buildWalletResponse(wallet),
      transaction,
    });
  }
);

module.exports = router;
