const express = require("express");
const Note = require("../models/Note");
const Task = require("../models/Task");
const Notification = require("../models/Notification");
const { verifyToken } = require("../middleware/auth");
const { formatNote, formatTask, formatUser } = require("../utils/formatters");

const router = express.Router();

router.get("/", verifyToken, async (req, res) => {
  const [notifications, notes, tasks] = await Promise.all([
    Notification.find({ userId: req.user._id }).sort({ createdAt: -1 }).limit(5),
    Note.find({ status: "approved" }).sort({ createdAt: -1 }).limit(3),
    Task.find({ status: "open" }).sort({ trendingScore: -1, createdAt: -1 }).limit(12),
  ]);

  const boostedTasks = tasks.filter((task) => task.boosted).slice(0, 5).map(formatTask);
  const trendingTasks = [...tasks].sort((a, b) => b.trendingScore - a.trendingScore).slice(0, 5).map(formatTask);
  const taskFeed = tasks.map(formatTask);

  return res.json({
    user: formatUser(req.user),
    notifications: notifications.map((item) => ({
      id: item._id.toString(),
      title: item.title,
      body: item.body,
      read: item.read,
      createdAt: item.createdAt,
    })),
    metrics: {
      walletBalance: req.user.wallet?.balance || 0,
      notesCount: req.user.stats?.notesCount || 0,
      tasksCount: req.user.stats?.tasksPosted || 0,
      earnings: req.user.wallet?.earnings || 0,
      rating: req.user.rating || 0,
    },
    boostedTasks,
    trendingTasks,
    taskFeed,
    notesPreview: notes.map(formatNote),
  });
});

module.exports = router;
