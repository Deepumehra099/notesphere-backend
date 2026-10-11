const crypto = require("crypto");
const express = require("express");
const Deposit = require("../models/Deposit");
const PaymentOrder = require("../models/PaymentOrder");
const { verifyToken } = require("../middleware/auth");
const { ensureWallet, buildWalletResponse } = require("../services/walletService");
const env = require("../config/env");

const router = express.Router();

function buildReference() {
  return `DEP${Date.now()}${crypto.randomBytes(3).toString("hex").toUpperCase()}`;
}

router.get("/details", verifyToken, (_req, res) => {
  return res.json({
    upi: {
      id: env.depositUpiId,
      name: env.depositUpiName,
      configured: Boolean(env.depositUpiId),
    },
    bank: {
      name: env.depositBankName,
      account_name: env.depositBankAccountName,
      account_number: env.depositBankAccountNumber,
      ifsc: env.depositBankIfsc,
      configured: Boolean(env.depositBankAccountNumber && env.depositBankIfsc),
    },
  });
});

async function listDeposits(req, res) {
  const [deposits, paymentOrders] = await Promise.all([
    Deposit.find({ userId: req.user._id }).sort({ createdAt: -1 }),
    PaymentOrder.find({ userId: req.user._id }).sort({ createdAt: -1 }),
  ]);
  return res.json({
    deposits: [
      ...deposits.map((item) => ({
        id: item._id.toString(),
        amount: item.amount || item.tokens || 0,
        method: item.method,
        provider: item.provider,
        accountName: item.accountName,
        accountRef: item.accountRef,
        referenceId: item.referenceId,
        reference: item.paymentReference,
        sender_details: item.senderDetails,
        status: item.status,
        admin_note: item.adminRemark,
        createdAt: item.createdAt,
        updatedAt: item.updatedAt,
      })),
      ...paymentOrders.map((item) => ({
        id: item._id.toString(),
        amount: item.amount,
        method: "Razorpay",
        provider: item.provider,
        referenceId: item.providerPaymentId || item.providerOrderId,
        reference: item.providerPaymentId || item.providerOrderId,
        status: item.status === "paid" ? "success" : item.status,
        admin_note: "",
        createdAt: item.createdAt,
        updatedAt: item.updatedAt,
      })),
    ].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()),
  });
}

router.get("/", verifyToken, listDeposits);
router.get("/history", verifyToken, listDeposits);

async function createDepositRequest(req, res) {
  const amount = Number(req.body.amount);
  const method = String(req.body.method || "upi").trim().toLowerCase();
  const reference = String(req.body.reference || "").trim();
  const senderDetails = String(req.body.sender_details || "").trim();

  if (!Number.isInteger(amount) || amount <= 0) {
    return res.status(400).json({ message: "Amount must be a positive integer" });
  }

  if (!["upi", "bank"].includes(method)) {
    return res.status(400).json({ message: "Method must be upi or bank" });
  }
  if (!reference || !senderDetails) {
    return res.status(400).json({ message: "Sender details and transfer reference are required" });
  }
  if (method === "upi" && !env.depositUpiId) {
    return res.status(503).json({ message: "UPI deposits are not configured yet" });
  }
  if (method === "bank" && (!env.depositBankAccountNumber || !env.depositBankIfsc)) {
    return res.status(503).json({ message: "Bank deposits are not configured yet" });
  }

  const deposit = await Deposit.create({
    userId: req.user._id,
    amount,
    method,
    accountName: req.user.name || "",
    accountRef: senderDetails,
    paymentReference: reference,
    senderDetails,
    referenceId: buildReference(),
    status: "pending",
    metadata: req.body.metadata || {},
  });

  return res.status(201).json({
    message: "Deposit request submitted for admin approval",
    deposit: {
      id: deposit._id.toString(),
      amount: deposit.amount,
      method: deposit.method,
      reference,
      sender_details: senderDetails,
      referenceId: deposit.referenceId,
      status: deposit.status,
      createdAt: deposit.createdAt,
    },
  });
}

router.post("/", verifyToken, createDepositRequest);
router.post("/request", verifyToken, createDepositRequest);

router.get("/summary", verifyToken, async (req, res) => {
  const wallet = await ensureWallet(req.user._id);
  return res.json({ wallet: buildWalletResponse(wallet) });
});

module.exports = router;
