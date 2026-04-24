const express = require("express");
const bcrypt = require("bcrypt");
const User = require("../models/User");
const Note = require("../models/Note");
const Task = require("../models/Task");
const Gig = require("../models/Gig");
const Transaction = require("../models/Transaction");
const Notification = require("../models/Notification");
const { verifyToken, requireAdmin } = require("../middleware/auth");
const { generateToken } = require("../utils/auth");
const { formatUser, formatNote, formatTask, formatGig } = require("../utils/formatters");
const {
  addBonus,
  approveWithdrawal,
  buildWalletResponse,
  ensureWallet,
  rejectWithdrawal,
} = require("../services/walletService");
const { notifyUser } = require("../services/notificationService");

const router = express.Router();

async function updateNoteStatus(req, res, status) {
  const note = await Note.findByIdAndUpdate(
    req.params.id,
    {
      status,
      rejectionReason: status === "rejected" ? String(req.body.reason || "Rejected by admin").trim() : "",
    },
    { new: true }
  );

  if (!note) {
    return res.status(404).json({ message: "Note not found" });
  }

  await notifyUser(
    note.seller.userId,
    `Note ${status}`,
    status === "approved"
      ? `${note.title} is now visible to buyers.`
      : `${note.title} was rejected. ${note.rejectionReason || ""}`.trim()
  );

  return res.json({ message: `Note ${status}`, note: formatNote(note) });
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
    token: generateToken(user._id.toString()),
    user: formatUser(user),
  });
});

router.get("/dashboard", verifyToken, requireAdmin, async (_req, res) => {
  const [users, notes, tasks, gigs, transactions, notifications] = await Promise.all([
    User.find().sort({ createdAt: -1 }),
    Note.find().sort({ createdAt: -1 }),
    Task.find().sort({ createdAt: -1 }),
    Gig.find().sort({ createdAt: -1 }),
    Transaction.find().sort({ createdAt: -1 }).limit(20),
    Notification.find().sort({ createdAt: -1 }).limit(20),
  ]);

  return res.json({
    stats: {
      users: users.length,
      notes: notes.length,
      tasks: tasks.length,
      gigs: gigs.length,
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

router.get("/users", verifyToken, requireAdmin, async (_req, res) => {
  const users = await User.find().sort({ createdAt: -1 });
  return res.json({ users: users.map(formatUser) });
});

router.patch("/users/:id/block", verifyToken, requireAdmin, async (req, res) => {
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

router.get("/withdrawals", verifyToken, requireAdmin, async (req, res) => {
  const userId = String(req.query.userId || "").trim();
  const query = { source: "withdraw" };

  if (userId) {
    query.userId = userId;
  }

  const withdrawals = await Transaction.find(query).populate("userId", "name email uid upiId").sort({ createdAt: -1 });

  return res.json({
    requests: withdrawals.map((item) => ({
      id: item._id.toString(),
      userId: item.userId?._id?.toString?.() || "",
      user_name: item.userId?.name || "Unknown user",
      email: item.userId?.email || "",
      uid: item.userId?.uid || "",
      upiId: item.metadata?.upiId || item.userId?.upiId || "",
      amount: item.amount,
      status: item.status,
      category: item.category,
      referenceId: item.referenceId,
      createdAt: item.createdAt,
      updatedAt: item.updatedAt,
      metadata: item.metadata || {},
    })),
  });
});

router.post("/withdrawals/:id/approve", verifyToken, requireAdmin, async (req, res) => {
  const { wallet, transaction } = await approveWithdrawal(req.params.id, req.user._id);
  return res.json({
    message: "Withdrawal approved",
    wallet: buildWalletResponse(wallet),
    transaction,
  });
});

router.post("/withdrawals/:id/reject", verifyToken, requireAdmin, async (req, res) => {
  const { wallet, transaction, refundTransaction } = await rejectWithdrawal(
    req.params.id,
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

router.post("/wallets/:userId/bonus", verifyToken, requireAdmin, async (req, res) => {
  const amount = Number(req.body.amount);
  const note = String(req.body.note || "").trim();

  if (!Number.isFinite(amount) || amount <= 0) {
    return res.status(400).json({ message: "Amount must be a positive number" });
  }

  const { wallet, transaction } = await addBonus(req.params.userId, Math.floor(amount), req.user._id, note);

  return res.status(201).json({
    message: "Bonus added successfully",
    wallet: buildWalletResponse(wallet),
    transaction,
  });
});

router.get("/transactions", verifyToken, requireAdmin, async (req, res) => {
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

router.get("/wallets/:userId", verifyToken, requireAdmin, async (req, res) => {
  const wallet = await ensureWallet(req.params.userId);
  return res.json({ wallet: buildWalletResponse(wallet) });
});

router.get("/notes", verifyToken, requireAdmin, async (_req, res) => {
  const notes = await Note.find().sort({ createdAt: -1 });
  return res.json({ notes: notes.map((note) => formatNote(note)) });
});

router.get("/pending-notes", verifyToken, requireAdmin, async (_req, res) => {
  const notes = await Note.find({ status: "pending" }).sort({ createdAt: 1 });
  return res.json({ notes: notes.map((note) => formatNote(note)) });
});

router.patch("/notes/:id/status", verifyToken, requireAdmin, async (req, res) => {
  const status = String(req.body.status || "").trim();
  if (!["approved", "rejected"].includes(status)) {
    return res.status(400).json({ message: "Invalid note status" });
  }

  return updateNoteStatus(req, res, status);
});

router.put("/approve-note/:id", verifyToken, requireAdmin, async (req, res) => updateNoteStatus(req, res, "approved"));
router.put("/reject-note/:id", verifyToken, requireAdmin, async (req, res) => updateNoteStatus(req, res, "rejected"));

router.get("/tasks", verifyToken, requireAdmin, async (_req, res) => {
  const tasks = await Task.find().sort({ createdAt: -1 });
  return res.json({ tasks: tasks.map(formatTask) });
});

router.patch("/tasks/:id", verifyToken, requireAdmin, async (req, res) => {
  const updates = {};

  if (typeof req.body.boosted === "boolean") {
    updates.boosted = req.body.boosted;
  }

  if (typeof req.body.status === "string") {
    updates.status = req.body.status;
  }

  const task = await Task.findByIdAndUpdate(req.params.id, updates, { new: true });

  if (!task) {
    return res.status(404).json({ message: "Task not found" });
  }

  return res.json({ message: "Task updated", task: formatTask(task) });
});

router.get("/gigs", verifyToken, requireAdmin, async (_req, res) => {
  const gigs = await Gig.find().sort({ createdAt: -1 });
  return res.json({ gigs: gigs.map(formatGig) });
});

router.patch("/gigs/:id", verifyToken, requireAdmin, async (req, res) => {
  const gig = await Gig.findByIdAndUpdate(req.params.id, { featured: Boolean(req.body.featured) }, { new: true });

  if (!gig) {
    return res.status(404).json({ message: "Gig not found" });
  }

  return res.json({ message: "Gig updated", gig: formatGig(gig) });
});

module.exports = router;
