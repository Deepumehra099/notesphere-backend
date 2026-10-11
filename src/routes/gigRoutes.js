const express = require("express");
const Gig = require("../models/Gig");
const User = require("../models/User");
const Task = require("../models/Task");
const Chat = require("../models/Chat");
const { verifyToken } = require("../middleware/auth");
const { formatGig } = require("../utils/formatters");
const { fundTaskEscrow } = require("../services/walletService");
const { notifyUser } = require("../services/notificationService");

const router = express.Router();

router.get("/", verifyToken, async (req, res) => {
  const page = Math.max(1, Number.parseInt(req.query.page, 10) || 1);
  const limit = Math.min(10, Math.max(1, Number.parseInt(req.query.limit, 10) || 10));
  const filter = {
    $and: [
      { $or: [{ status: "approved" }, { status: { $exists: false } }] },
      { "seller.userId": { $ne: req.user._id } },
    ],
  };
  const [gigs, total] = await Promise.all([
    Gig.find(filter).sort({ featured: -1, createdAt: -1 }).skip((page - 1) * limit).limit(limit),
    Gig.countDocuments(filter),
  ]);
  return res.json({
    gigs: gigs.map(formatGig),
    total,
    page,
    pages: Math.ceil(total / limit),
    has_more: page * limit < total,
  });
});

router.get("/mine", verifyToken, async (req, res) => {
  const gigs = await Gig.find({ "seller.userId": req.user._id }).sort({ createdAt: -1 }).limit(50);
  return res.json({ gigs: gigs.map(formatGig) });
});

router.post("/", verifyToken, async (req, res) => {
  const { title, description, category, price } = req.body;

  const normalizedTitle = String(title || "").trim();
  const normalizedDescription = String(description || "").trim();
  const numericPrice = Number(price);
  const normalizedCategory = String(category || "General").trim() || "General";
  if (
    normalizedTitle.length < 3
    || normalizedTitle.length > 160
    || !normalizedDescription
    || normalizedDescription.length > 2000
    || normalizedCategory.length > 80
    || !Number.isSafeInteger(numericPrice)
    || numericPrice <= 0
  ) {
    return res.status(400).json({ message: "Title, description, and price are required" });
  }

  const gig = await Gig.create({
    title: normalizedTitle,
    description: normalizedDescription,
    category: normalizedCategory,
    price: numericPrice,
    status: "pending",
    seller: {
      userId: req.user._id,
      name: req.user.name,
      email: req.user.email,
    },
  });

  await User.findByIdAndUpdate(req.user._id, {
    $inc: { "stats.gigsCount": 1 },
  });

  return res.status(201).json({
    message: "Gig submitted for review",
    gig: formatGig(gig),
  });
});

router.post("/:gigId/hire", verifyToken, async (req, res) => {
  const gig = await Gig.findOne({
    _id: req.params.gigId,
    $or: [{ status: "approved" }, { status: { $exists: false } }],
  });
  if (!gig) return res.status(404).json({ message: "Gig not found" });
  if (String(gig.seller.userId) === String(req.user._id)) {
    return res.status(400).json({ message: "You cannot hire your own gig" });
  }

  const amount = Number(gig.price);
  if (!Number.isSafeInteger(amount) || amount <= 0) {
    return res.status(400).json({ message: "Gig price must be a positive whole number" });
  }
  const brief = String(req.body.brief || "Direct hire from marketplace").trim();
  const deadline = String(req.body.deadline || "Flexible").trim();
  if (brief.length > 500 || !deadline || deadline.length > 120) {
    return res.status(400).json({ message: "The service brief or deadline is invalid" });
  }

  const task = await Task.create({
    title: gig.title,
    description: brief || gig.description,
    budget: amount,
    deadline,
    location: "",
    mode: "remote",
    urgency: "normal",
    trendingScore: 0,
    status: "accepted",
    createdBy: {
      userId: req.user._id,
      name: req.user.name,
      email: req.user.email,
      isVerified: Boolean(req.user.isVerified),
    },
    acceptedBy: {
      userId: gig.seller.userId,
      name: gig.seller.name,
      email: gig.seller.email,
    },
    chatContext: { purpose: "service", category: "service" },
    escrow: { amount },
  });

  try {
    await fundTaskEscrow({
      taskId: task._id,
      creatorId: req.user._id,
      amount,
      title: `Gig hire funded: ${gig.title}`,
    });
  } catch (error) {
    await Task.findByIdAndDelete(task._id);
    throw error;
  }

  await Chat.findOneAndUpdate(
    { taskId: task._id },
    {
      $setOnInsert: { taskId: task._id, participants: [req.user._id, gig.seller.userId] },
      $set: { lastMessage: "Direct hire started", lastMessageAt: new Date() },
    },
    { upsert: true, new: true }
  );
  await notifyUser(gig.seller.userId, "Gig hired", `A buyer started an escrow-funded order for "${gig.title}".`);
  return res.status(201).json({ message: "Gig hired successfully", gig: formatGig(gig), task: task.toJSON(), task_id: task._id.toString() });
});

module.exports = router;
