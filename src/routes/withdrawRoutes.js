const express = require("express");
const User = require("../models/User");
const Withdrawal = require("../models/Withdrawal");
const { verifyToken } = require("../middleware/auth");
const { rateLimit } = require("../middleware/rateLimit");
const { requestWithdrawal, buildWalletResponse, ensureWallet } = require("../services/walletService");

const router = express.Router();

function sanitizeAccounts(accounts = []) {
  return accounts.map((item) => ({
    id: item._id?.toString?.() || `${item.type}-${item.upiId || item.accountNumber || item.label}`,
    type: item.type,
    label: item.label || "",
    accountName: item.accountName || "",
    accountNumber: item.accountNumber || "",
    ifsc: item.ifsc || "",
    upiId: item.upiId || "",
    isDefault: Boolean(item.isDefault),
  }));
}

router.get("/accounts", verifyToken, async (req, res) => {
  const user = await User.findById(req.user._id);
  return res.json({ accounts: sanitizeAccounts(user?.payoutAccounts || []) });
});

router.post("/accounts", verifyToken, async (req, res) => {
  const type = String(req.body.type || "").trim().toLowerCase();
  const account = {
    type: type === "bank" ? "bank" : "upi",
    label: String(req.body.label || "").trim(),
    accountName: String(req.body.accountName || req.user.name || "").trim(),
    accountNumber: String(req.body.accountNumber || "").trim(),
    ifsc: String(req.body.ifsc || "").trim(),
    upiId: String(req.body.upiId || "").trim(),
    isDefault: Boolean(req.body.isDefault ?? true),
  };

  if (account.type === "upi" && !account.upiId) {
    return res.status(400).json({ message: "UPI ID is required" });
  }

  if (account.type === "bank" && (!account.accountNumber || !account.ifsc)) {
    return res.status(400).json({ message: "Bank account number and IFSC are required" });
  }

  const user = await User.findById(req.user._id);
  user.payoutAccounts = account.isDefault
    ? (user.payoutAccounts || []).map((item) => ({ ...item.toObject(), isDefault: false })).concat(account)
    : (user.payoutAccounts || []).concat(account);

  if (account.type === "upi" && account.isDefault) {
    user.upiId = account.upiId;
  }

  await user.save();

  return res.status(201).json({
    message: "Withdrawal account added",
    accounts: sanitizeAccounts(user.payoutAccounts || []),
  });
});

router.get("/history", verifyToken, async (req, res) => {
  const items = await Withdrawal.find({ userId: req.user._id }).sort({ createdAt: -1 });
  const withdrawals = items.map((item) => ({
    id: item._id.toString(),
    amount: item.amount,
    method: item.method,
    accountLabel: item.accountLabel,
    accountName: item.metadata?.accountName || "",
    accountRef: item.accountRef,
    upi: item.metadata?.upiId || "",
    ifsc: item.metadata?.ifsc || "",
    referenceId: item.referenceId,
    status: item.status === "completing" ? "processing" : item.status === "approved" ? "completed" : item.status,
    adminRemark: item.adminRemark,
    createdAt: item.createdAt,
    updatedAt: item.updatedAt,
    metadata: item.metadata || {},
  }));
  return res.json({
    requests: withdrawals,
    withdrawals,
  });
});

async function createWithdrawalRequest(req, res) {
  const amount = Number(req.body.amount);
  const user = await User.findById(req.user._id);
  const accountId = String(req.body.accountId || "").trim();
  const account =
    (user.payoutAccounts || []).find((item) => item._id?.toString?.() === accountId) ||
    (user.payoutAccounts || []).find((item) => item.isDefault) ||
    null;
  const method = String(req.body.method || account?.type || "").trim().toLowerCase();
  const upiId = String(req.body.upi || req.body.upiId || account?.upiId || "").trim();
  const accountName = String(req.body.account_name || req.body.accountName || account?.accountName || req.user.name || "").trim();
  const accountNumber = String(req.body.account_number || req.body.accountNumber || account?.accountNumber || "").trim();
  const ifsc = String(req.body.ifsc || account?.ifsc || "").trim().toUpperCase();

  if (!Number.isInteger(amount) || amount < 100 || amount > 50000) {
    return res.status(400).json({ message: "Withdrawal amount must be between 100 and 50000" });
  }

  if (!["upi", "bank"].includes(method)) {
    return res.status(400).json({ message: "Withdrawal method must be UPI or bank" });
  }
  if (method === "upi" && !/^[^\s@]+@[^\s@]+$/.test(upiId)) {
    return res.status(400).json({ message: "Enter a valid UPI ID" });
  }
  if (method === "bank" && (!accountName || !/^\d{6,20}$/.test(accountNumber) || !/^[A-Z]{4}0[A-Z0-9]{6}$/.test(ifsc))) {
    return res.status(400).json({ message: "Enter a valid account name, account number and IFSC code" });
  }

  if (await Withdrawal.exists({ userId: req.user._id, status: "pending" })) {
    return res.status(400).json({ message: "You already have a pending withdrawal request" });
  }

  const { wallet, transaction, withdrawal } = await requestWithdrawal(req.user._id, amount, {
    method,
    accountLabel: method === "bank" ? "BANK" : "UPI",
    accountRef: method === "bank" ? accountNumber : upiId,
    accountName,
    accountNumber,
    ifsc,
    upiId,
  });

  return res.status(201).json({
    message: "Withdrawal request submitted",
    wallet: buildWalletResponse(wallet),
    transaction,
    withdrawal: {
      id: withdrawal._id.toString(),
      referenceId: withdrawal.referenceId,
      status: withdrawal.status,
      amount: withdrawal.amount,
      method: withdrawal.method,
      accountName,
      accountRef: withdrawal.accountRef,
      ifsc,
      createdAt: withdrawal.createdAt,
    },
  });
}

const withdrawalRateLimit = rateLimit({ keyPrefix: "withdraw:request", limit: 5, windowMs: 60 * 1000 });
router.post("/", verifyToken, withdrawalRateLimit, createWithdrawalRequest);
router.post("/request", verifyToken, withdrawalRateLimit, createWithdrawalRequest);
router.get("/requests", verifyToken, async (req, res) => {
  const items = await Withdrawal.find({ userId: req.user._id }).sort({ createdAt: -1 });
  const requests = items.map((item) => ({
    id: item._id.toString(),
    amount: item.amount,
    method: item.method,
    accountLabel: item.accountLabel,
    accountName: item.metadata?.accountName || "",
    accountRef: item.accountRef,
    upi: item.metadata?.upiId || "",
    ifsc: item.metadata?.ifsc || "",
    referenceId: item.referenceId,
    status: item.status === "completing" ? "processing" : item.status === "approved" ? "completed" : item.status,
    adminRemark: item.adminRemark,
    createdAt: item.createdAt,
    updatedAt: item.updatedAt,
    metadata: item.metadata || {},
  }));
  return res.json({ requests });
});

router.get("/", verifyToken, async (req, res) => {
  const wallet = await ensureWallet(req.user._id);
  return res.json({ wallet: buildWalletResponse(wallet) });
});

module.exports = router;
