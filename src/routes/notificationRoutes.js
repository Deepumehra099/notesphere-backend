const express = require("express");
const Notification = require("../models/Notification");
const { verifyToken } = require("../middleware/auth");

const router = express.Router();

router.get("/", verifyToken, async (req, res) => {
  const notifications = await Notification.find({ userId: req.user._id }).sort({ createdAt: -1 }).limit(50);
  return res.json({
    notifications: notifications.map((item) => ({
      id: item._id.toString(),
      title: item.title,
      body: item.body,
      read: item.read,
      createdAt: item.createdAt,
    })),
  });
});

router.post("/:id/read", verifyToken, async (req, res) => {
  const notification = await Notification.findOneAndUpdate(
    { _id: req.params.id, userId: req.user._id },
    { $set: { read: true } },
    { new: true }
  );

  if (!notification) {
    return res.status(404).json({ message: "Notification not found" });
  }

  return res.json({ message: "Notification marked as read" });
});

module.exports = router;
