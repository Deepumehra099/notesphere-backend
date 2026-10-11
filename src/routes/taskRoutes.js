const express = require("express");
const Task = require("../models/Task");
const User = require("../models/User");
const { verifyToken } = require("../middleware/auth");
const { formatTask } = require("../utils/formatters");
const { buildWalletResponse, cancelTaskEscrow, ensureWallet, fundTaskEscrow, payoutTask } = require("../services/walletService");
const { notifyUser } = require("../services/notificationService");
const Chat = require("../models/Chat");
const { getPlatformCommissionPercent } = require("../services/tokenService");

const router = express.Router();

function toFrontendTask(task, includeDispute = false) {
  const baseTask = formatTask(task);
  if (!includeDispute) baseTask.dispute = null;
  return {
    ...baseTask,
    status: task.status === "accepted" ? "assigned" : baseTask.status,
  };
}

router.get("/", verifyToken, async (_req, res) => {
  const tasks = await Task.find({ status: { $in: ["open", "accepted", "submitted"] } }).sort({
    boosted: -1,
    trendingScore: -1,
    createdAt: -1,
  });
  const formatted = tasks.map(toFrontendTask);

  return res.json({
    boostedTasks: tasks.filter((task) => task.boosted).slice(0, 6).map(toFrontendTask),
    trendingTasks: [...tasks].sort((a, b) => b.trendingScore - a.trendingScore).slice(0, 6).map(toFrontendTask),
    taskFeed: formatted,
    tasks: formatted,
  });
});

router.get("/commission", verifyToken, async (_req, res) => {
  return res.json({ commissionPercent: await getPlatformCommissionPercent() });
});

async function createTask(req, res) {
  const { title, description, budget, price, deadline, location, mode, urgency, boosted, is_urgent } = req.body;
  const creationRequestId = String(req.body.requestId || req.body.request_id || "").trim().slice(0, 120);
  const requestedChatCategory = String(req.body.chat_context_type || "task").trim().toLowerCase();
  const chatCategory = requestedChatCategory === "requirement" ? "requirement" : "task";
  const requestedBudget = budget ?? price;

  if (!String(title || "").trim() || !String(description || "").trim() || requestedBudget === undefined || requestedBudget === null || requestedBudget === "") {
    return res.status(400).json({ message: "Title, description, and budget are required" });
  }

  const numericBudget = Number(requestedBudget);
  if (!Number.isSafeInteger(numericBudget) || numericBudget < 1) {
    return res.status(400).json({ message: "Task budget must be at least ₹1" });
  }
  if (String(title).trim().length < 3 || String(title).trim().length > 160 || String(description).trim().length > 2000) {
    return res.status(400).json({ message: "Task title or description is outside the allowed length" });
  }
  const cleanDeadline = String(deadline || "").trim();
  if (!cleanDeadline || (cleanDeadline !== "Flexible" && (!Number.isFinite(Date.parse(cleanDeadline)) || Date.parse(cleanDeadline) <= Date.now()))) {
    return res.status(400).json({ message: "Choose a valid future task deadline" });
  }

  if (creationRequestId) {
    const existing = await Task.findOne({ "createdBy.userId": req.user._id, creationRequestId });
    if (existing) {
      if (existing.status === "open" && existing.escrow?.settlementStatus === "held") {
        return res.status(200).json({ message: "Task already published", task: toFrontendTask(existing), alreadyCreated: true });
      }
      return res.status(409).json({ message: "This task publish request is already being processed" });
    }
  }

  let task;
  try {
    task = await Task.create({
    title: String(title).trim(),
    description: String(description).trim(),
    budget: numericBudget,
    deadline: cleanDeadline,
    location: String(location || "").trim(),
    mode: mode === "nearby" ? "nearby" : "remote",
    urgency: urgency === "urgent" || Boolean(is_urgent) ? "urgent" : "normal",
    boosted: Boolean(boosted),
    trendingScore: boosted ? 90 : 68,
    createdBy: {
      userId: req.user._id,
      name: req.user.name,
      email: req.user.email,
      isVerified: Boolean(req.user.isVerified),
      eliteBuyer: req.user.role === "admin",
      eliteSeller: Boolean((req.user.stats?.tasksCompleted || 0) >= 5),
    },
    chatContext: { purpose: "post", category: chatCategory },
    status: "funding",
    creationRequestId,
    escrow: { amount: numericBudget },
    });
  } catch (error) {
    if (creationRequestId && error.code === 11000) {
      const existing = await Task.findOne({ "createdBy.userId": req.user._id, creationRequestId });
      if (existing?.status === "open" && existing.escrow?.settlementStatus === "held") {
        return res.status(200).json({ message: "Task already published", task: toFrontendTask(existing), alreadyCreated: true });
      }
      return res.status(409).json({ message: "This task publish request is already being processed" });
    }
    throw error;
  }

  try {
    await fundTaskEscrow({
      taskId: task._id,
      creatorId: req.user._id,
      amount: numericBudget,
      title: `Task funded: ${task.title}`,
    });
  } catch (error) {
    await Task.findOneAndDelete({ _id: task._id, status: "funding", "escrow.settlementStatus": "none" });
    throw error;
  }

  await User.findByIdAndUpdate(req.user._id, { $inc: { "stats.tasksPosted": 1 } });
  const fundedTask = await Task.findById(task._id);

  return res.status(201).json({
    message: "Task posted successfully",
    task: toFrontendTask(fundedTask),
  });
}

router.post("/", verifyToken, createTask);
router.post("/create", verifyToken, createTask);

router.get("/trending", verifyToken, async (_req, res) => {
  const tasks = await Task.find({ status: { $in: ["open", "accepted", "submitted"] } })
    .sort({ boosted: -1, trendingScore: -1, createdAt: -1 })
    .limit(12);

  return res.json({ tasks: tasks.map(toFrontendTask) });
});

router.get("/my-tasks", verifyToken, async (req, res) => {
  const [createdTasks, assignedTasks] = await Promise.all([
    Task.find({ "createdBy.userId": req.user._id }).sort({ createdAt: -1 }),
    Task.find({ "acceptedBy.userId": req.user._id }).sort({ createdAt: -1 }),
  ]);

  return res.json({
    created_tasks: createdTasks.map(toFrontendTask),
    assigned_tasks: assignedTasks.map(toFrontendTask),
  });
});

router.get("/:id", verifyToken, async (req, res) => {
  const task = await Task.findById(req.params.id);
  if (!task) {
    return res.status(404).json({ message: "Task not found" });
  }

  const userId = String(req.user._id);
  const includeDispute = req.user.role === "admin"
    || userId === String(task.createdBy.userId)
    || userId === String(task.acceptedBy?.userId || "");
  return res.json({ task: toFrontendTask(task, includeDispute) });
});

router.post("/:id/accept", verifyToken, async (req, res) => {
  const task = await Task.findById(req.params.id);

  if (!task) {
    return res.status(404).json({ message: "Task not found" });
  }

  if (task.status !== "open") {
    return res.status(400).json({ message: "Task is no longer available" });
  }

  if (String(task.createdBy.userId) === String(req.user._id)) {
    return res.status(400).json({ message: "You cannot accept your own task" });
  }

  task.acceptedBy = {
    userId: req.user._id,
    name: req.user.name,
    email: req.user.email,
  };
  task.status = "accepted";
  await task.save();
  await Chat.findOneAndUpdate(
    { taskId: task._id },
    {
      $setOnInsert: {
        taskId: task._id,
        participants: [task.createdBy.userId, req.user._id],
      },
      $set: {
        lastMessage: `${req.user.name} accepted the task`,
        lastMessageAt: new Date(),
      },
    },
    { upsert: true, new: true }
  );

  await notifyUser(task.createdBy.userId, "Task accepted", `${req.user.name} accepted "${task.title}".`);

  return res.json({ message: "Task accepted", task: toFrontendTask(task) });
});

router.post("/:id/submit", verifyToken, async (req, res) => {
  const task = await Task.findById(req.params.id);

  if (!task) {
    return res.status(404).json({ message: "Task not found" });
  }

  if (String(task.acceptedBy?.userId || "") !== String(req.user._id)) {
    return res.status(403).json({ message: "Only the assigned user can submit work" });
  }

  const submissionText = String(req.body.submission || req.body.text || "").trim();
  if (!submissionText) {
    return res.status(400).json({ message: "Submission details are required" });
  }

  task.submission = {
    text: submissionText,
    fileUrl: String(req.body.fileUrl || "").trim(),
    submittedAt: new Date(),
  };
  task.status = "submitted";
  await task.save();

  await notifyUser(task.createdBy.userId, "Task submitted", `Work was submitted for "${task.title}".`);
  return res.json({ message: "Task submitted", task: toFrontendTask(task) });
});

router.post("/:id/dispute", verifyToken, async (req, res) => {
  const task = await Task.findById(req.params.id);
  if (!task) return res.status(404).json({ message: "Task not found" });
  const userId = String(req.user._id);
  const isBuyer = String(task.createdBy.userId) === userId;
  const isWorker = String(task.acceptedBy?.userId || "") === userId;
  if (!isBuyer && !isWorker) return res.status(403).json({ message: "Only task participants can dispute this task" });
  if (!["accepted", "submitted"].includes(task.status) || task.escrow.fundingVersion !== 2 || task.escrow.settlementStatus !== "held") {
    return res.status(409).json({ message: "This task is not eligible for an escrow dispute" });
  }
  const reason = String(req.body.reason || "").trim();
  if (reason.length < 5 || reason.length > 1000) {
    return res.status(400).json({ message: "Dispute reason must be between 5 and 1000 characters" });
  }
  const disputedTask = await Task.findOneAndUpdate(
    {
      _id: task._id,
      status: { $in: ["accepted", "submitted"] },
      "escrow.settlementStatus": "held",
    },
    {
      $set: {
        status: "disputed",
        "escrow.settlementStatus": "disputed",
        dispute: { openedBy: req.user._id, reason, openedAt: new Date() },
      },
    },
    { new: true }
  );
  if (!disputedTask) return res.status(409).json({ message: "Task changed before dispute could be opened" });
  await notifyUser(task.createdBy.userId, "Task dispute opened", `A dispute was opened for "${task.title}".`);
  if (String(task.acceptedBy.userId) !== String(task.createdBy.userId)) {
    await notifyUser(task.acceptedBy.userId, "Task dispute opened", `A dispute was opened for "${task.title}".`);
  }
  return res.json({ message: "Task dispute opened for admin review", task: toFrontendTask(disputedTask) });
});

router.post("/:id/complete", verifyToken, async (req, res) => {
  const task = await Task.findById(req.params.id);

  if (!task) {
    return res.status(404).json({ message: "Task not found" });
  }

  const allowed = req.user.role === "admin" || String(task.createdBy.userId) === String(req.user._id);
  if (!allowed) {
    return res.status(403).json({ message: "Only the task owner or admin can mark this complete" });
  }

  if (!task.acceptedBy?.userId) {
    return res.status(400).json({ message: "Task has no assigned user" });
  }

  const payout = await payoutTask({
    taskId: task._id,
    workerId: task.acceptedBy.userId,
    amount: task.escrow?.amount || task.budget,
    completedBy: req.user._id,
  });

  const freshTask = await Task.findById(task._id);
  await notifyUser(
    task.acceptedBy.userId,
    "Task completed",
    `₹${payout.sellerPayout} was added to your INR wallet for "${task.title}" after platform commission.`
  );

  return res.json({
    message: "Task marked as completed",
    task: toFrontendTask(freshTask),
    payoutTransactionId: payout.transaction._id.toString(),
    grossAmount: task.escrow?.amount || task.budget,
    commissionAmount: payout.commissionAmount,
    sellerPayout: payout.sellerPayout,
  });
});

router.post("/:id/cancel", verifyToken, async (req, res) => {
  const result = await cancelTaskEscrow({ taskId: req.params.id, buyerId: req.user._id });
  if (!result.alreadyRefunded) await notifyUser(req.user._id, "Task cancelled", `₹${result.transaction?.amount || result.task.escrow.amount} held for "${result.task.title}" was returned to your available wallet.`);
  return res.json({ message: result.alreadyRefunded ? "Task refund already completed" : "Task cancelled and escrow refunded", task: toFrontendTask(result.task), wallet: buildWalletResponse(await ensureWallet(req.user._id)), refundTransactionId: result.transaction?._id?.toString?.() || "" });
});

module.exports = router;
