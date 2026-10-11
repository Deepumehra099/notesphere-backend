const express = require("express");
const User = require("../models/User");
const TokenTransaction = require("../models/TokenTransaction");
const { verifyToken } = require("../middleware/auth");
const { getTokenSettings } = require("../services/tokenService");

const router = express.Router();
router.use(verifyToken);
router.get(["/", "/balance"], async (req, res) => res.json({ balance: req.user.tokenBalance || 0, referralCode: req.user.referralCode || "" }));
router.get("/summary", async (req, res) => {
  const rows = await TokenTransaction.aggregate([{ $match: { userId: req.user._id } }, { $group: { _id: null, earned: { $sum: { $cond: [{ $in: ["$type", ["earned", "refund", "adjustment"]] }, "$amount", 0] } }, spent: { $sum: { $cond: [{ $eq: ["$type", "spent"] }, "$amount", 0] } } } }]);
  return res.json({ balance: req.user.tokenBalance || 0, earned: rows[0]?.earned || 0, spent: rows[0]?.spent || 0, pending: 0 });
});
router.get("/history", async (req, res) => {
  const limit = Math.min(100, Math.max(1, Number(req.query.limit) || 50));
  const rows = await TokenTransaction.find({ userId: req.user._id }).sort({ createdAt: -1 }).limit(limit);
  return res.json({ transactions: rows.map((item) => ({ id: item._id.toString(), userId: item.userId.toString(), type: item.type.toUpperCase(), amount: item.type === "spent" ? -item.amount : item.amount, balanceAfter: item.balanceAfter, description: item.title, metadata: item.metadata, createdAt: item.createdAt })), hasMore: rows.length === limit, nextCursor: rows.at(-1)?._id.toString() || null });
});
router.get("/earning-methods", async (_req, res) => {
  const s = await getTokenSettings();
  return res.json({ methods: [{ id: "signup", title: "Signup Reward", description: "One-time reward for creating an account.", amount: s.signupReward, enabled: s.signupReward > 0 }, { id: "approved_note", title: "Upload Approved Note", description: "Earn Tokens after approval.", amount: s.noteApprovalReward, enabled: s.noteApprovalReward > 0 }, { id: "referral", title: "Refer a Friend", description: "Earn Tokens after a referred user's first approved note.", amount: s.referralReward, referredAmount: s.referralSignupReward, enabled: s.referralReward > 0 || s.referralSignupReward > 0 }] });
});
router.get("/referral", async (req, res) => {
  const referrals = await User.find({ referredBy: req.user._id }).select("name createdAt").sort({ createdAt: -1 });
  return res.json({ referralCode: req.user.referralCode || "", referrals: referrals.map((u) => ({ id: u._id.toString(), name: u.name, status: "registered", createdAt: u.createdAt })) });
});
module.exports = router;
