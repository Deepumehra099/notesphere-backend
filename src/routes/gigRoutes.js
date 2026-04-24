const express = require("express");
const Gig = require("../models/Gig");
const User = require("../models/User");
const { verifyToken } = require("../middleware/auth");
const { formatGig } = require("../utils/formatters");

const router = express.Router();

router.get("/", verifyToken, async (_req, res) => {
  const gigs = await Gig.find().sort({ featured: -1, createdAt: -1 });
  return res.json({ gigs: gigs.map(formatGig) });
});

router.post("/", verifyToken, async (req, res) => {
  const { title, description, category, price } = req.body;

  if (!title || !description || !price) {
    return res.status(400).json({ message: "Title, description, and price are required" });
  }

  const gig = await Gig.create({
    title: String(title).trim(),
    description: String(description).trim(),
    category: String(category || "General").trim(),
    price: Number(price),
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
    message: "Gig created successfully",
    gig: formatGig(gig),
  });
});

module.exports = router;
