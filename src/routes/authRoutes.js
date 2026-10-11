const express = require("express");
const bcrypt = require("bcrypt");
const crypto = require("crypto");
const { OAuth2Client } = require("google-auth-library");
const User = require("../models/User");
const env = require("../config/env");
const { generateToken } = require("../utils/auth");
const { formatUser } = require("../utils/formatters");
const { verifyToken } = require("../middleware/auth");
const { ensureWallet } = require("../services/walletService");
const { sendOtpEmail } = require("../utils/sendEmail");
const { changeTokens, getTokenSettings } = require("../services/tokenService");

const router = express.Router();
const googleClient = env.googleClientId ? new OAuth2Client(env.googleClientId) : null;

function createUid() {
  return `NV${Math.floor(100000 + Math.random() * 900000)}`;
}

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(email || "").trim().toLowerCase());
}

function createOtp() {
  return String(Math.floor(100000 + Math.random() * 900000));
}

async function issueTokenResponse(user) {
  const token = generateToken(user);
  await ensureWallet(user._id);
  const currentUser = await User.findById(user._id);

  return {
    token,
    access_token: token,
    user: formatUser(currentUser),
  };
}

async function signup(req, res) {
  try {
    const { name, email, password, referralCode } = req.body;

    if (!name || !email || !password) {
      return res.status(400).json({ message: "Name, email, and password are required" });
    }

    const normalizedEmail = String(email).trim().toLowerCase();
    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: "Please enter a valid email address" });
    }

    const existingUser = await User.findOne({ email: normalizedEmail });

    if (existingUser) {
      return res.status(409).json({ message: "Email already registered" });
    }

    const passwordHash = await bcrypt.hash(String(password), 10);
    const referrer = referralCode ? await User.findOne({ referralCode: String(referralCode).trim().toUpperCase() }) : null;
    if (referralCode && (!referrer || referrer.email === normalizedEmail)) return res.status(400).json({ message: "Referral code is invalid" });
    const user = await User.create({
      uid: createUid(),
      name: String(name).trim(),
      email: normalizedEmail,
      passwordHash,
      referralCode: `NS${crypto.randomBytes(5).toString("hex").toUpperCase()}`,
      referredBy: referrer?._id || null,
      wallet: { balance: 0, earnings: 0 },
      stats: { notesCount: 0, tasksPosted: 0, tasksCompleted: 0, gigsCount: 0, downloads: 0 },
    });

    const rewards = await getTokenSettings();
    if (rewards.signupReward > 0) await changeTokens({ userId: user._id, amount: rewards.signupReward, type: "earned", source: "signup_reward", title: "Signup reward", idempotencyKey: `signup:${user._id}` });
    if (referrer) {
      if (rewards.referralSignupReward > 0) await changeTokens({ userId: user._id, amount: rewards.referralSignupReward, type: "earned", source: "referral_reward", title: "Referral signup reward", idempotencyKey: `referral-signup:${user._id}`, metadata: { referrerId: referrer._id.toString() } });
    }

    return res.status(201).json(await issueTokenResponse(user));
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
    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: "Please enter a valid email address" });
    }

    const user = await User.findOne({ email: normalizedEmail });

    if (!user) {
      return res.status(401).json({ message: "Invalid email or password" });
    }

    if (user.isBlocked) {
      return res.status(403).json({ message: "Your account has been blocked by admin" });
    }

    if (user.role === "admin") {
      return res.status(403).json({ message: "Use the admin login flow for this account" });
    }

    const isMatch = await bcrypt.compare(String(password), user.passwordHash);

    if (!isMatch) {
      return res.status(401).json({ message: "Invalid email or password" });
    }

    return res.json(await issueTokenResponse(user));
  } catch (_error) {
    return res.status(500).json({ message: "Login failed" });
  }
});

async function sendOtp(req, res) {
  try {
    const normalizedEmail = String(req.body.email || "").trim().toLowerCase();
    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: "Please enter a valid email address" });
    }

    const user = await User.findOne({ email: normalizedEmail });
    if (!user) {
      return res.status(404).json({ message: "Account not found for this email" });
    }

    const otp = createOtp();
    user.otp = otp;
    user.otpExpiry = new Date(Date.now() + 5 * 60 * 1000);
    await user.save();

    await sendOtpEmail(user.email, otp, user.name || "there");
    return res.json({ message: "OTP sent to your email" });
  } catch (_error) {
    return res.status(500).json({ message: "Could not send OTP" });
  }
}

router.post("/send-otp", sendOtp);
router.post("/forgot-password", sendOtp);

router.post("/verify-otp", async (req, res) => {
  const normalizedEmail = String(req.body.email || "").trim().toLowerCase();
  const otp = String(req.body.otp || "").trim();

  if (!isValidEmail(normalizedEmail) || otp.length !== 6) {
    return res.status(400).json({ message: "Email and 6-digit OTP are required" });
  }

  const user = await User.findOne({ email: normalizedEmail });
  if (!user || !user.otp || !user.otpExpiry) {
    return res.status(400).json({ message: "OTP not found" });
  }

  if (user.otp !== otp || user.otpExpiry.getTime() < Date.now()) {
    return res.status(400).json({ message: "OTP is invalid or expired" });
  }

  return res.json({ message: "OTP verified" });
});

router.post("/reset-password", async (req, res) => {
  const normalizedEmail = String(req.body.email || "").trim().toLowerCase();
  const otp = String(req.body.otp || "").trim();
  const newPassword = String(req.body.newPassword || "").trim();

  if (!isValidEmail(normalizedEmail) || otp.length !== 6 || newPassword.length < 6) {
    return res.status(400).json({ message: "Email, valid OTP, and 6+ character password are required" });
  }

  const user = await User.findOne({ email: normalizedEmail });
  if (!user || user.otp !== otp || !user.otpExpiry || user.otpExpiry.getTime() < Date.now()) {
    return res.status(400).json({ message: "OTP is invalid or expired" });
  }

  user.passwordHash = await bcrypt.hash(newPassword, 10);
  user.otp = "";
  user.otpExpiry = null;
  await user.save();

  return res.json({ message: "Password reset successful" });
});

router.post("/google", async (req, res) => {
  try {
    let email = String(req.body.email || "").trim().toLowerCase();
    let name = String(req.body.name || "").trim();
    let googleId = String(req.body.googleId || "").trim();
    const idToken = String(req.body.idToken || "").trim();

    if (idToken && googleClient) {
      const ticket = await googleClient.verifyIdToken({
        idToken,
        audience: env.googleClientId,
      });
      const payload = ticket.getPayload();
      email = String(payload?.email || email).trim().toLowerCase();
      name = String(payload?.name || name).trim();
      googleId = String(payload?.sub || googleId).trim();
    }

    if (!isValidEmail(email) || !name) {
      return res.status(400).json({ message: "Google account details are invalid" });
    }

    let user = await User.findOne({ email });
    let createdUser = false;

    if (!user) {
      user = await User.create({
        uid: createUid(),
        name,
        email,
        googleId,
        passwordHash: crypto.randomBytes(24).toString("hex"),
        wallet: { balance: 0, earnings: 0 },
        stats: { notesCount: 0, tasksPosted: 0, tasksCompleted: 0, gigsCount: 0, downloads: 0 },
      });
      createdUser = true;
    } else if (!user.googleId && googleId) {
      user.googleId = googleId;
      await user.save();
    }

    if (createdUser) {
      const settings = await getTokenSettings();
      if (settings.signupReward > 0) await changeTokens({ userId: user._id, amount: settings.signupReward, type: "earned", source: "signup_reward", title: "Signup reward", idempotencyKey: `signup:${user._id}` });
    }

    return res.json(await issueTokenResponse(user));
  } catch (_error) {
    return res.status(500).json({ message: "Google login failed" });
  }
});

router.get("/me", verifyToken, async (req, res) => {
  await ensureWallet(req.user._id);
  const freshUser = await User.findById(req.user._id);
  return res.json({ user: formatUser(freshUser) });
});

module.exports = router;
