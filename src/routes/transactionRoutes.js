const express = require("express");
const { verifyToken } = require("../middleware/auth");
const Transaction = require("../models/Transaction");

const router = express.Router();

router.get("/", verifyToken, async (req, res) => {
  const page = Math.max(1, Number(req.query.page) || 1);
  const limit = Math.min(50, Math.max(1, Number(req.query.limit) || 10));
  const skip = (page - 1) * limit;
  const category = String(req.query.category || "").trim();
  const query = { userId: req.user._id };

  if (category) {
    query.category = category;
  }

  const [items, total] = await Promise.all([
    Transaction.find(query).sort({ createdAt: -1 }).skip(skip).limit(limit),
    Transaction.countDocuments(query),
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

module.exports = router;
