const express = require("express");
const bcrypt = require("bcrypt");
const Note = require("../models/Note");
const Task = require("../models/Task");
const Notification = require("../models/Notification");
const User = require("../models/User");
const { verifyToken } = require("../middleware/auth");
const { ensureWallet, buildWalletResponse } = require("../services/walletService");
const { upload } = require("../middleware/upload");
const { uploadAsset } = require("../services/storageService");
const { formatUser } = require("../utils/formatters");

const router = express.Router();

router.get("/", verifyToken, async (req, res) => {
  const wallet = await ensureWallet(req.user._id);
  const [myNotes, myTasks, inboxCount] = await Promise.all([
    Note.countDocuments({ "seller.userId": req.user._id }),
    Task.countDocuments({
      $or: [{ "createdBy.userId": req.user._id }, { "acceptedBy.userId": req.user._id }],
    }),
    Notification.countDocuments({ userId: req.user._id, read: false }),
  ]);

  return res.json({
    profile: {
      ...formatUser(req.user),
      walletSummary: buildWalletResponse(wallet),
      notes: myNotes,
      tasks: myTasks,
      inboxCount,
    },
  });
});

router.put("/", verifyToken, upload.single("avatar"), async (req, res) => {
  const updates = {
    name: String(req.body.name || req.user.name).trim(),
    email: String(req.body.email || req.user.email).trim().toLowerCase(),
    phone: String(req.body.phone || "").trim(),
    bio: String(req.body.bio || "").trim(),
    location: String(req.body.location || "").trim(),
    language: String(req.body.language || "en").trim(),
    skills: String(req.body.skills || req.user.skills?.join(",") || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    upiId: String(req.body.upiId || req.body.upi || req.user.upiId || "").trim(),
  };

  if (!updates.name || !updates.email) {
    return res.status(400).json({ message: "Name and email are required" });
  }

  const existingUser = await User.findOne({ email: updates.email, _id: { $ne: req.user._id } });
  if (existingUser) {
    return res.status(409).json({ message: "Email already in use" });
  }

  if (req.body.password) {
    if (String(req.body.password).length < 6) {
      return res.status(400).json({ message: "Password must be at least 6 characters" });
    }

    updates.passwordHash = await bcrypt.hash(String(req.body.password), 10);
  }

  if (req.file) {
    const avatar = await uploadAsset(req.file, "noteverse/avatars", "image");
    updates.avatarUrl = avatar.url;
  }

  const user = await User.findByIdAndUpdate(req.user._id, { $set: updates }, { new: true });
  return res.json({
    message: "Profile updated",
    user: formatUser(user),
  });
});

module.exports = router;
