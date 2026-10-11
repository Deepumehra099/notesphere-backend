const express = require("express");
const bcrypt = require("bcrypt");
const User = require("../models/User");
const Note = require("../models/Note");
const Task = require("../models/Task");
const Gig = require("../models/Gig");
const Transaction = require("../models/Transaction");
const Notification = require("../models/Notification");
const Deposit = require("../models/Deposit");
const Withdrawal = require("../models/Withdrawal");
const Wallet = require("../models/Wallet");
const PlatformLedgerEntry = require("../models/PlatformLedgerEntry");
const { verifyToken, requireAdmin } = require("../middleware/auth");
const { generateToken } = require("../utils/auth");
const { formatUser, formatNote, formatTask, formatGig } = require("../utils/formatters");
const {
  buildWalletResponse,
  completeWithdrawal,
  creditWallet,
  ensureWallet,
  payoutTask,
  rejectWithdrawal,
  resolveTaskDispute,
} = require("../services/walletService");
const { notifyUser } = require("../services/notificationService");
const TokenTransaction = require("../models/TokenTransaction");
const CreditSettings = require("../models/CreditSettings");
const { changeTokens, getTokenSettings, qualifyReferral } = require("../services/tokenService");

const router = express.Router();

async function updateNoteStatus(req, res, status) {
  const note = await Note.findOneAndUpdate(
    { _id: req.params.id, status: "pending" },
    {
      status,
      rejectionReason: status === "rejected" ? String(req.body.reason || "Rejected by admin").trim() : "",
    },
    { new: true }
  );

  if (!note) {
    const existing = await Note.findById(req.params.id);
    if (!existing) return res.status(404).json({ message: "Note not found" });
    if (existing.status !== status) return res.status(409).json({ message: "Note has already been reviewed" });
    // A retry after an interrupted approval completes idempotent rewards below.
    req.noteApprovalRetry = existing;
  }
  const reviewedNote = note || req.noteApprovalRetry;

  if (status === "approved") {
    const settings = await getTokenSettings();
    if (settings.noteApprovalReward > 0) await changeTokens({ userId: reviewedNote.seller.userId, amount: settings.noteApprovalReward, type: "earned", source: "note_reward", title: `Approved note reward: ${reviewedNote.title}`, idempotencyKey: `note-approval:${reviewedNote._id}`, metadata: { noteId: reviewedNote._id.toString() } });
    await qualifyReferral(reviewedNote.seller.userId, `approved-note:${reviewedNote._id}`);
  }

  await notifyUser(
    reviewedNote.seller.userId,
    `Note ${status}`,
    status === "approved"
      ? `${reviewedNote.title} is now visible to buyers.`
      : `${reviewedNote.title} was rejected. ${reviewedNote.rejectionReason || ""}`.trim()
  );

  return res.json({ message: `Note ${status}`, note: formatNote(reviewedNote) });
}

router.post("/login", async (req, res) => {
  const { email, password } = req.body;

  if (!email || !password) {
    return res.status(400).json({ message: "Email and password are required" });
  }

  const user = await User.findOne({ email: String(email).trim().toLowerCase(), role: "admin" });

  if (!user) {
    return res.status(401).json({ message: "Invalid email or password" });
  }

  const valid = await bcrypt.compare(String(password), user.passwordHash);

  if (!valid) {
    return res.status(401).json({ message: "Invalid email or password" });
  }

  return res.json({
    token: generateToken(user),
    user: formatUser(user),
  });
});

router.use(verifyToken, requireAdmin);

router.get("/credits/settings", async (_req, res) => res.json({ settings: await getTokenSettings() }));
router.put("/credits/settings", async (req, res) => {
  const allowed = ["signupReward", "noteApprovalReward", "referralReward", "referralSignupReward", "premiumNotePrice", "platformCommissionPercent"];
  const update = {};
  const aliases = { approvedNoteReward: "noteApprovalReward" };
  for (const key of allowed) if (req.body[key] !== undefined || req.body[Object.keys(aliases).find((alias) => aliases[alias] === key)] !== undefined) {
    const input = req.body[key] ?? req.body[Object.keys(aliases).find((alias) => aliases[alias] === key)];
    const value = Number(input);
    if (!Number.isFinite(value) || value < 0 || value > (key === "platformCommissionPercent" ? 100 : 100000) || (key !== "platformCommissionPercent" && !Number.isSafeInteger(value))) return res.status(400).json({ message: `Invalid ${key}` });
    update[key] = value;
  }
  const settings = await CreditSettings.findOneAndUpdate({ key: "default" }, { $set: update, $setOnInsert: { key: "default" } }, { new: true, upsert: true, setDefaultsOnInsert: true });
  return res.json({ settings });
});
router.get("/credits/transactions", async (req, res) => {
  const items = await TokenTransaction.find().populate("userId", "name email uid").sort({ createdAt: -1 }).limit(Math.min(200, Number(req.query.limit) || 100));
  return res.json({ transactions: items.map((item) => ({ id: item._id.toString(), userId: item.userId?._id?.toString?.() || "", user_name: item.userId?.name || "Unknown", email: item.userId?.email || "", type: item.type, amount: item.type === "spent" ? -item.amount : item.amount, description: item.title, metadata: item.metadata || {}, createdAt: item.createdAt })) });
});
router.get("/credits/summary", async (_req, res) => {
  const [earned, spent, transactionCount, topEarners] = await Promise.all([
    TokenTransaction.aggregate([{ $match: { type: { $in: ["earned", "refund", "adjustment"] } } }, { $group: { _id: null, total: { $sum: "$amount" } } }]),
    TokenTransaction.aggregate([{ $match: { type: "spent" } }, { $group: { _id: null, total: { $sum: "$amount" } } }]),
    TokenTransaction.countDocuments(),
    User.find({ tokenBalance: { $gt: 0 } }).sort({ tokenBalance: -1 }).limit(10).select("name email tokenBalance"),
  ]);
  return res.json({ totalIssued: earned[0]?.total || 0, totalSpent: spent[0]?.total || 0, transactionCount, topEarners: topEarners.map((user) => ({ userId: user._id.toString(), name: user.name, email: user.email, balance: user.tokenBalance })) });
});
router.get("/credits/user", async (req, res) => {
  const value = String(req.query.user_id || req.query.userId || "").trim();
  const clauses = [{ email: value.toLowerCase() }];
  if (/^[a-f\d]{24}$/i.test(value)) clauses.push({ _id: value });
  const user = await User.findOne({ $or: clauses }).select("name email tokenBalance");
  if (!user) return res.status(404).json({ message: "User not found" });
  return res.json({ userId: user._id.toString(), name: user.name, email: user.email, balance: user.tokenBalance || 0 });
});
router.post("/credits/refund-note/:noteId", async (req, res) => {
  const note = await Note.findById(req.params.noteId);
  if (!note) return res.status(404).json({ message: "Note not found" });
  const userValue = String(req.query.user_id || "").trim();
  const clauses = [{ email: userValue.toLowerCase() }];
  if (/^[a-f\d]{24}$/i.test(userValue)) clauses.push({ _id: userValue });
  const buyer = await User.findOne({ $or: clauses });
  if (!buyer) return res.status(404).json({ message: "Buyer not found" });
  const purchase = note.buyers.find((item) => String(item.userId) === String(buyer._id));
  if (!purchase) return res.status(409).json({ message: "This user has not unlocked the note" });
  const refundAmount = Math.max(0, Number(note.unlockCost || note.price) || 0);
  if (refundAmount > 0) await changeTokens({ userId: buyer._id, amount: refundAmount, type: "refund", source: "token_refund", title: String(req.query.reason || `Refund: ${note.title}`), idempotencyKey: `note-refund:${note._id}:${buyer._id}`, metadata: { noteId: note._id.toString(), adminId: req.user._id.toString() } });
  note.buyers = note.buyers.filter((item) => String(item.userId) !== String(buyer._id));
  await note.save();
  return res.json({ message: "Note unlock refunded", amount: refundAmount, userId: buyer._id.toString() });
});
router.post("/credits/adjust", async (req, res) => {
  const userId = String(req.body.user_id || req.body.userId || "");
  const amount = Math.floor(Number(req.body.amount));
  const operation = req.body.operation || "add";
  const result = await changeTokens({ userId, amount, type: ["deduct", "remove"].includes(operation) ? "spent" : "adjustment", source: "admin_adjustment", title: String(req.body.reason || "Admin token adjustment"), idempotencyKey: String(req.body.idempotency_key || req.body.idempotencyKey || req.body.requestId || `admin:${req.user._id}:${userId}:${Date.now()}`), metadata: { adminId: req.user._id.toString() } });
  return res.json({ transaction: result.transaction, userId, balance: result.transaction.balanceAfter });
});

router.get("/dashboard", async (_req, res) => {
  const [users, notes, tasks, gigs, transactions, notifications] = await Promise.all([
    User.find().sort({ createdAt: -1 }),
    Note.find().sort({ createdAt: -1 }),
    Task.find().sort({ createdAt: -1 }),
    Gig.find().sort({ createdAt: -1 }),
    Transaction.find().sort({ createdAt: -1 }).limit(20),
    Notification.find().sort({ createdAt: -1 }).limit(20),
  ]);
  const [depositsCount, withdrawalsCount] = await Promise.all([
    Deposit.countDocuments({}),
    Withdrawal.countDocuments({}),
  ]);

  return res.json({
    stats: {
      users: users.length,
      notes: notes.length,
      tasks: tasks.length,
      gigs: gigs.length,
      deposits: depositsCount,
      withdrawals: withdrawalsCount,
      pendingNotes: notes.filter((item) => item.status === "pending").length,
      pendingWithdrawals: transactions.filter((item) => item.category === "withdraw" && item.status === "pending").length,
    },
    users: users.map(formatUser),
    notes: notes.map((note) => formatNote(note)),
    tasks: tasks.map(formatTask),
    gigs: gigs.map(formatGig),
    transactions: transactions.map((item) => ({
      id: item._id.toString(),
      userId: item.userId?.toString?.() || "",
      type: item.type,
      source: item.source,
      category: item.category,
      amount: item.amount,
      status: item.status,
      title: item.title,
      createdAt: item.createdAt,
    })),
    notifications: notifications.map((item) => ({
      id: item._id.toString(),
      title: item.title,
      body: item.body,
      createdAt: item.createdAt,
    })),
  });
});

router.get("/deposits", async (_req, res) => {
  const deposits = await Deposit.find({}).populate("userId", "name email uid").sort({ createdAt: -1 });
  return res.json({
    deposits: deposits.map((item) => ({
      id: item._id.toString(),
      userId: item.userId?._id?.toString?.() || "",
      user_name: item.userId?.name || "Unknown user",
      email: item.userId?.email || "",
      uid: item.userId?.uid || "",
      amount: item.amount,
      amount: item.amount || item.tokens || 0,
      method: item.method,
      provider: item.provider,
      accountRef: item.accountRef,
      referenceId: item.referenceId,
      reference: item.paymentReference,
      sender_details: item.senderDetails,
      status: item.status,
      adminRemark: item.adminRemark,
      createdAt: item.createdAt,
      updatedAt: item.updatedAt,
    })),
  });
});

router.post("/deposits/:id/approve", async (req, res) => {
  const deposit = await Deposit.findOneAndUpdate(
    { _id: req.params.id, status: { $in: ["pending", "processing"] } },
    { $set: { status: "processing" } },
    { new: true }
  );
  if (!deposit) {
    const exists = await Deposit.exists({ _id: req.params.id });
    return res.status(exists ? 400 : 404).json({ message: exists ? "Deposit already processed" : "Deposit not found" });
  }

  const { wallet, transaction } = await creditWallet({
    userId: deposit.userId,
    amount: deposit.amount || deposit.tokens,
    source: "deposit",
    category: "deposit",
    status: "completed",
    title: `Deposit approved (${deposit.method.toUpperCase()})`,
    metadata: {
      depositId: deposit._id.toString(),
      referenceId: deposit.referenceId,
      approvedBy: req.user._id.toString(),
    },
    idempotencyKey: `manual-deposit:${deposit._id.toString()}`,
  });

  const approvedDeposit = await Deposit.findOneAndUpdate(
    { _id: deposit._id, status: "processing" },
    {
      $set: {
        status: "approved",
        adminRemark: String(req.body.remark || "Approved by admin").trim(),
        transactionId: transaction._id,
      },
    },
    { new: true }
  );
  if (!approvedDeposit) {
    throw new Error("Deposit was credited but approval could not be finalized; retry to reconcile");
  }
  if (!transaction.metadata?.approvalNotified) {
    await notifyUser(deposit.userId, "Deposit approved", `₹${deposit.amount || deposit.tokens} was added to your real money wallet.`);
    await Transaction.findByIdAndUpdate(transaction._id, {
      $set: { "metadata.approvalNotified": true },
    });
  }

  return res.json({ message: "Deposit approved", deposit: approvedDeposit, wallet: buildWalletResponse(wallet), transaction });
});

router.post("/deposits/:id/reject", async (req, res) => {
  const deposit = await Deposit.findOneAndUpdate(
    { _id: req.params.id, status: "pending" },
    {
      $set: {
        status: "rejected",
        adminRemark: String(req.body.remark || "Rejected by admin").trim(),
      },
    },
    { new: true }
  );
  if (!deposit) {
    const exists = await Deposit.exists({ _id: req.params.id });
    return res.status(exists ? 400 : 404).json({ message: exists ? "Deposit already processed" : "Deposit not found" });
  }
  await notifyUser(deposit.userId, "Deposit rejected", deposit.adminRemark || "Your deposit request was rejected.");

  return res.json({ message: "Deposit rejected", deposit });
});

router.get("/users", async (_req, res) => {
  const users = await User.find().sort({ createdAt: -1 });
  return res.json({ users: users.map(formatUser) });
});

router.patch("/users/:id/block", async (req, res) => {
  const user = await User.findByIdAndUpdate(
    req.params.id,
    { $set: { isBlocked: Boolean(req.body.blocked ?? true) } },
    { new: true }
  );

  if (!user) {
    return res.status(404).json({ message: "User not found" });
  }

  return res.json({ message: user.isBlocked ? "User blocked" : "User unblocked", user: formatUser(user) });
});

router.get("/withdrawals", async (req, res) => {
  const userId = String(req.query.userId || "").trim();
  const query = {};

  if (userId) query.userId = userId;

  const withdrawals = await Withdrawal.find(query).populate("userId", "name email uid upiId").sort({ createdAt: -1 });

  return res.json({
    requests: withdrawals.map((item) => ({
      id: item._id.toString(),
      userId: item.userId?._id?.toString?.() || "",
      user_name: item.userId?.name || "Unknown user",
      email: item.userId?.email || "",
      uid: item.userId?.uid || "",
      method: item.method,
      upiId: item.metadata?.upiId || (item.method === "upi" ? item.accountRef : "") || item.userId?.upiId || "",
      accountName: item.metadata?.accountName || "",
      accountNumber: item.metadata?.accountNumber || (item.method === "bank" ? item.accountRef : ""),
      ifsc: item.metadata?.ifsc || "",
      accountRef: item.accountRef || "",
      amount: item.amount,
      status: item.status === "completing" ? "processing" : item.status === "approved" ? "completed" : item.status,
      referenceId: item.referenceId,
      method: item.method,
      adminRemark: item.adminRemark || "",
      createdAt: item.createdAt,
      updatedAt: item.updatedAt,
      metadata: item.metadata || {},
    })),
  });
});

router.post("/withdrawals/:id/approve", async (req, res) => {
  const withdrawal = await Withdrawal.findOneAndUpdate(
    { _id: req.params.id, status: "pending" },
    { $set: { status: "processing", adminRemark: "Accepted for processing" } },
    { new: true }
  );
  if (!withdrawal?.transactionId) {
    return res.status(404).json({ message: "Withdrawal request not found" });
  }

  await notifyUser(withdrawal.userId, "Withdrawal processing", "Your withdrawal request has been accepted and is being processed.");
  return res.json({
    message: "Withdrawal is processing",
    request: withdrawal,
  });
});

router.post("/withdrawals/:id/complete", async (req, res) => {
  const withdrawal = await Withdrawal.findOne({ _id: req.params.id, status: "processing" });
  if (!withdrawal?.transactionId) {
    return res.status(404).json({ message: "Processing withdrawal request not found" });
  }

  const { wallet, transaction } = await completeWithdrawal(withdrawal.transactionId, req.user._id);
  return res.json({
    message: "Withdrawal completed",
    wallet: buildWalletResponse(wallet),
    transaction,
  });
});

router.post("/withdrawals/:id/reject", async (req, res) => {
  const withdrawal = await Withdrawal.findById(req.params.id);
  if (!withdrawal?.transactionId) {
    return res.status(404).json({ message: "Withdrawal request not found" });
  }

  const { wallet, transaction, refundTransaction } = await rejectWithdrawal(
    withdrawal.transactionId,
    req.user._id,
    String(req.body.reason || "").trim()
  );

  return res.json({
    message: "Withdrawal rejected",
    wallet: buildWalletResponse(wallet),
    transaction,
    refundTransaction,
  });
});

router.get("/finance/overview", async (_req, res) => {
  const [depositTotals, withdrawalGroups, walletTotals, commissionTotals, refundTotals, volumeTotals, disputeCount, transactionCount] = await Promise.all([
    Transaction.aggregate([
      { $match: { source: "deposit", status: "completed" } },
      { $group: { _id: null, amount: { $sum: "$amount" }, count: { $sum: 1 } } },
    ]),
    Withdrawal.aggregate([
      { $group: { _id: "$status", amount: { $sum: "$amount" }, count: { $sum: 1 } } },
    ]),
    Wallet.aggregate([
      { $group: { _id: null, held: { $sum: { $add: ["$heldBalance", "$lockedBalance"] } } } },
    ]),
    PlatformLedgerEntry.aggregate([
      { $match: { type: "commission" } },
      { $group: { _id: null, amount: { $sum: "$amount" } } },
    ]),
    Transaction.aggregate([
      { $match: { source: "refund", status: "completed" } },
      { $group: { _id: null, amount: { $sum: "$amount" } } },
    ]),
    Transaction.aggregate([
      { $match: { status: "completed" } },
      { $group: { _id: null, amount: { $sum: "$amount" }, count: { $sum: 1 } } },
    ]),
    Task.countDocuments({ status: "disputed" }),
    Transaction.countDocuments({}),
  ]);
  const withdrawals = Object.fromEntries(withdrawalGroups.map((row) => [row._id, row]));
  const pending = withdrawals.pending || { amount: 0, count: 0 };
  const processing = withdrawals.processing || { amount: 0, count: 0 };
  const completed = withdrawals.completed || withdrawals.approved || { amount: 0, count: 0 };
  const failed = withdrawals.failed || withdrawals.rejected || { amount: 0, count: 0 };

  return res.json({
    totalDeposits: depositTotals[0]?.amount || 0,
    depositCount: depositTotals[0]?.count || 0,
    totalWithdrawals: (completed.amount || 0) + (pending.amount || 0) + (processing.amount || 0),
    pendingWithdrawals: pending.count + processing.count,
    pendingWithdrawalAmount: pending.amount + processing.amount,
    successfulWithdrawals: completed.amount || 0,
    successfulWithdrawalCount: completed.count || 0,
    failedWithdrawals: failed.amount || 0,
    escrowAmount: walletTotals[0]?.held || 0,
    platformRevenue: commissionTotals[0]?.amount || 0,
    refunds: refundTotals[0]?.amount || 0,
    disputes: disputeCount,
    transactionVolume: volumeTotals[0]?.amount || 0,
    transactionCount,
  });
});

router.get("/transactions", async (req, res) => {
  const page = Math.max(1, Number(req.query.page) || 1);
  const limit = Math.min(50, Math.max(1, Number(req.query.limit) || 20));
  const skip = (page - 1) * limit;
  const userId = String(req.query.userId || "").trim();
  const source = String(req.query.source || "").trim();
  const status = String(req.query.status || "").trim();
  const category = String(req.query.category || "").trim();
  const query = {};

  if (userId) query.userId = userId;
  if (source) query.source = source;
  if (status) query.status = status;
  if (category) query.category = category;

  const [items, total] = await Promise.all([
    Transaction.find(query).populate("userId", "name email uid").sort({ createdAt: -1 }).skip(skip).limit(limit),
    Transaction.countDocuments(query),
  ]);

  return res.json({
    transactions: items.map((item) => ({
      id: item._id.toString(),
      userId: item.userId?._id?.toString?.() || "",
      user_name: item.userId?.name || "Unknown user",
      email: item.userId?.email || "",
      uid: item.userId?.uid || "",
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

router.get("/wallets/:userId", async (req, res) => {
  const wallet = await ensureWallet(req.params.userId);
  return res.json({ wallet: buildWalletResponse(wallet) });
});

router.get("/notes", async (_req, res) => {
  const notes = await Note.find().sort({ createdAt: -1 });
  return res.json({ notes: notes.map((note) => formatNote(note)) });
});

router.get("/pending-notes", async (_req, res) => {
  const notes = await Note.find({ status: "pending" }).sort({ createdAt: 1 });
  return res.json({ notes: notes.map((note) => formatNote(note)) });
});

router.patch("/notes/:id/status", async (req, res) => {
  const status = String(req.body.status || "").trim();
  if (!["approved", "rejected"].includes(status)) {
    return res.status(400).json({ message: "Invalid note status" });
  }

  return updateNoteStatus(req, res, status);
});

router.put("/approve-note/:id", async (req, res) => updateNoteStatus(req, res, "approved"));
router.put("/reject-note/:id", async (req, res) => updateNoteStatus(req, res, "rejected"));

router.get("/tasks", async (_req, res) => {
  const tasks = await Task.find().sort({ createdAt: -1 });
  return res.json({ tasks: tasks.map(formatTask) });
});

router.patch("/tasks/:id", async (req, res) => {
  const updates = {};

  if (typeof req.body.boosted === "boolean") {
    updates.boosted = req.body.boosted;
  }

  if (typeof req.body.status === "string") {
    return res.status(400).json({ message: "Task status cannot be edited directly; use the escrow settlement actions" });
  }
  if (!Object.keys(updates).length) return res.status(400).json({ message: "No task changes provided" });
  const task = await Task.findByIdAndUpdate(req.params.id, updates, { new: true });

  if (!task) {
    return res.status(404).json({ message: "Task not found" });
  }

  return res.json({ message: "Task updated", task: formatTask(task) });
});

router.post("/tasks/:id/complete", async (req, res) => {
  const task = await Task.findById(req.params.id);
  if (!task) return res.status(404).json({ message: "Task not found" });
  if (!task.acceptedBy?.userId) return res.status(400).json({ message: "Task has no assigned worker" });
  const grossAmount = task.escrow?.amount || task.budget;
  const payout = await payoutTask({
    taskId: task._id,
    workerId: task.acceptedBy.userId,
    amount: grossAmount,
    completedBy: req.user._id,
  });
  const updatedTask = await Task.findById(task._id);
  await notifyUser(task.acceptedBy.userId, "Task completed", `${payout.sellerPayout} credits were paid after platform commission.`);
  return res.json({
    message: "Task completed and escrow settled",
    task: formatTask(updatedTask),
    grossAmount,
    commissionAmount: payout.commissionAmount,
    sellerPayout: payout.sellerPayout,
  });
});

router.post("/tasks/:id/resolve-dispute", async (req, res) => {
  const refundAmount = Number(req.body.refundAmount);
  const result = await resolveTaskDispute({
    taskId: req.params.id,
    refundAmount,
    adminUserId: req.user._id,
    adminNote: String(req.body.adminNote || ""),
  });
  await notifyUser(result.task.createdBy.userId, "Task dispute resolved", `Refund issued: ${result.refundAmount} credits.`);
  if (result.task.acceptedBy?.userId) {
    await notifyUser(result.task.acceptedBy.userId, "Task dispute resolved", `Payout issued: ${result.sellerPayout} credits.`);
  }
  return res.json({
    message: "Dispute resolved",
    task: formatTask(result.task),
    refundAmount: result.refundAmount,
    commissionAmount: result.commissionAmount,
    sellerPayout: result.sellerPayout,
  });
});

router.get("/gigs", async (_req, res) => {
  const gigs = await Gig.find().sort({ createdAt: -1 });
  return res.json({ gigs: gigs.map(formatGig) });
});

router.get("/gigs/pending", async (_req, res) => {
  const gigs = await Gig.find({ status: "pending" }).sort({ createdAt: -1 });
  return res.json({ gigs: gigs.map(formatGig) });
});

router.post("/gigs/:id/approve", async (req, res) => {
  const gig = await Gig.findOneAndUpdate(
    { _id: req.params.id, status: "pending" },
    { $set: { status: "approved", rejectionReason: "" } },
    { new: true }
  );
  if (!gig) {
    const exists = await Gig.exists({ _id: req.params.id });
    return res.status(exists ? 409 : 404).json({ message: exists ? "Gig is not pending review" : "Gig not found" });
  }
  await notifyUser(gig.seller.userId, "Gig approved", `${gig.title} is now visible in the marketplace.`);
  return res.json({ message: "Gig approved", gig: formatGig(gig) });
});

router.post("/gigs/:id/reject", async (req, res) => {
  const gig = await Gig.findOneAndUpdate(
    { _id: req.params.id, status: "pending" },
    { $set: { status: "rejected", rejectionReason: String(req.body.reason || "Rejected by admin").trim() } },
    { new: true }
  );
  if (!gig) {
    const exists = await Gig.exists({ _id: req.params.id });
    return res.status(exists ? 409 : 404).json({ message: exists ? "Gig is not pending review" : "Gig not found" });
  }
  await notifyUser(gig.seller.userId, "Gig rejected", `${gig.title} was rejected. ${gig.rejectionReason}`.trim());
  return res.json({ message: "Gig rejected", gig: formatGig(gig) });
});

router.patch("/gigs/:id", async (req, res) => {
  const gig = await Gig.findByIdAndUpdate(req.params.id, { featured: Boolean(req.body.featured) }, { new: true });

  if (!gig) {
    return res.status(404).json({ message: "Gig not found" });
  }

  return res.json({ message: "Gig updated", gig: formatGig(gig) });
});

module.exports = router;
