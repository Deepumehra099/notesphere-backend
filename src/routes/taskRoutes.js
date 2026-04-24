const express = require("express");
const Task = require("../models/Task");
const User = require("../models/User");
const { verifyToken } = require("../middleware/auth");
const { formatTask } = require("../utils/formatters");
const { fundTaskEscrow, payoutTask } = require("../services/walletService");
const { notifyUser } = require("../services/notificationService");

const router = express.Router();

router.get("/", verifyToken, async (_req, res) => {
  const tasks = await Task.find({ status: { $in: ["open", "accepted", "submitted"] } }).sort({
    boosted: -1,
    trendingScore: -1,
    createdAt: -1,
  });
  const formatted = tasks.map(formatTask);

  return res.json({
    boostedTasks: tasks.filter((task) => task.boosted).slice(0, 6).map(formatTask),
    trendingTasks: [...tasks].sort((a, b) => b.trendingScore - a.trendingScore).slice(0, 6).map(formatTask),
    taskFeed: formatted,
    tasks: formatted,
  });
});

router.post("/", verifyToken, async (req, res) => {
  const { title, description, budget, location, mode, urgency, boosted } = req.body;

  if (!title || !description || !budget) {
    return res.status(400).json({ message: "Title, description, and budget are required" });
  }

  const numericBudget = Number(budget);
  if (!Number.isFinite(numericBudget) || numericBudget <= 0) {
    return res.status(400).json({ message: "Budget must be a positive number" });
  }

  const task = await Task.create({
    title: String(title).trim(),
    description: String(description).trim(),
    budget: numericBudget,
    location: String(location || "").trim(),
    mode: mode === "nearby" ? "nearby" : "remote",
    urgency: urgency === "urgent" ? "urgent" : "normal",
    boosted: Boolean(boosted),
    trendingScore: boosted ? 90 : 68,
    createdBy: {
      userId: req.user._id,
      name: req.user.name,
      email: req.user.email,
    },
    escrow: { amount: numericBudget },
  });

  try {
    await fundTaskEscrow({
      taskId: task._id,
      creatorId: req.user._id,
      amount: numericBudget,
      title: `Task funded: ${task.title}`,
    });
  } catch (error) {
    await Task.findByIdAndDelete(task._id);
    throw error;
  }

  await User.findByIdAndUpdate(req.user._id, { $inc: { "stats.tasksPosted": 1 } });

  return res.status(201).json({
    message: "Task posted successfully",
    task: formatTask(task),
  });
});

router.get("/my-tasks", verifyToken, async (req, res) => {
  const [createdTasks, assignedTasks] = await Promise.all([
    Task.find({ "createdBy.userId": req.user._id }).sort({ createdAt: -1 }),
    Task.find({ "acceptedBy.userId": req.user._id }).sort({ createdAt: -1 }),
  ]);

  return res.json({
    created_tasks: createdTasks.map(formatTask),
    assigned_tasks: assignedTasks.map(formatTask),
  });
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

  await notifyUser(task.createdBy.userId, "Task accepted", `${req.user.name} accepted "${task.title}".`);

  return res.json({ message: "Task accepted", task: formatTask(task) });
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
  return res.json({ message: "Task submitted", task: formatTask(task) });
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
    `${task.escrow?.amount || task.budget} tokens were added for "${task.title}".`
  );

  return res.json({
    message: "Task marked as completed",
    task: formatTask(freshTask),
    payoutTransactionId: payout.transaction._id.toString(),
  });
});

module.exports = router;
