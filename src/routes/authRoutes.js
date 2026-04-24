const express = require("express");
const bcrypt = require("bcrypt");
const User = require("../models/User");
const { generateToken } = require("../utils/auth");
const { formatUser } = require("../utils/formatters");
const { verifyToken } = require("../middleware/auth");
const { ensureWallet } = require("../services/walletService");

const router = express.Router();

function createUid() {
  return `NV${Math.floor(100000 + Math.random() * 900000)}`;
}

async function signup(req, res) {
  try {
    const { name, email, password } = req.body;

    if (!name || !email || !password) {
      return res.status(400).json({ message: "Name, email, and password are required" });
    }

    const normalizedEmail = String(email).trim().toLowerCase();
    const existingUser = await User.findOne({ email: normalizedEmail });

    if (existingUser) {
      return res.status(409).json({ message: "Email already registered" });
    }

    const passwordHash = await bcrypt.hash(String(password), 10);
    const user = await User.create({
      uid: createUid(),
      name: String(name).trim(),
      email: normalizedEmail,
      passwordHash,
      wallet: { balance: 0, earnings: 0 },
      stats: { notesCount: 0, tasksPosted: 0, tasksCompleted: 0, gigsCount: 0, downloads: 0 },
    });

    const token = generateToken(user._id.toString());
    await ensureWallet(user._id);

    return res.status(201).json({
      token,
      access_token: token,
      user: formatUser(user),
    });
  } catch (_error) {
    return res.status(500).json({ message: "Failed to create account" });
  }
}

router.post("/signup", signup);
router.post("/register", signup);

router.post("/login", async (req, res) => {
  try {
    const { email, password } = req.body;

    if (!email || !password) {
      return res.status(400).json({ message: "Email and password are required" });
    }

    const normalizedEmail = String(email).trim().toLowerCase();
    const user = await User.findOne({ email: normalizedEmail });

    if (!user) {
      return res.status(401).json({ message: "Invalid email or password" });
    }

    if (user.isBlocked) {
      return res.status(403).json({ message: "Your account has been blocked by admin" });
    }

    const isMatch = await bcrypt.compare(String(password), user.passwordHash);

    if (!isMatch) {
      return res.status(401).json({ message: "Invalid email or password" });
    }

    const token = generateToken(user._id.toString());
    await ensureWallet(user._id);

    return res.json({
      token,
      access_token: token,
      user: formatUser(user),
    });
  } catch (_error) {
    return res.status(500).json({ message: "Login failed" });
  }
});

router.get("/me", verifyToken, async (req, res) => {
  await ensureWallet(req.user._id);
  const freshUser = await User.findById(req.user._id);
  return res.json({ user: formatUser(freshUser) });
});

module.exports = router;
