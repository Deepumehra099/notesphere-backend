const express = require('express');
const bcrypt = require('bcrypt');
const jwt = require('jsonwebtoken');
const User = require('../models/User');
const { sendOTP } = require('../utils/sendEmail');

const router = express.Router();

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const OTP_EXPIRY_MS = 5 * 60 * 1000;

function normalizeEmail(email) {
  return String(email || '').trim().toLowerCase();
}

function isValidEmail(email) {
  return EMAIL_REGEX.test(normalizeEmail(email));
}

function generateOTP() {
  return String(Math.floor(100000 + Math.random() * 900000));
}

function signToken(user) {
  return jwt.sign(
    {
      userId: user._id.toString(),
      email: user.email,
    },
    process.env.JWT_SECRET,
    { expiresIn: '7d' }
  );
}

function serializeUser(user) {
  return {
    id: user._id.toString(),
    name: user.name,
    email: user.email,
    googleId: user.googleId || '',
  };
}

router.post('/signup', async (req, res) => {
  try {
    const { name, email, password } = req.body || {};
    const normalizedEmail = normalizeEmail(email);

    if (!name || String(name).trim().length < 2) {
      return res.status(400).json({ message: 'Name is required' });
    }

    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: 'Enter a valid email address' });
    }

    if (!password || String(password).length < 6) {
      return res.status(400).json({ message: 'Password must be at least 6 characters' });
    }

    const existingUser = await User.findOne({ email: normalizedEmail });
    if (existingUser) {
      return res.status(409).json({ message: 'User already exists' });
    }

    const hashedPassword = await bcrypt.hash(String(password), 10);
    const user = await User.create({
      name: String(name).trim(),
      email: normalizedEmail,
      password: hashedPassword,
    });

    return res.status(201).json({
      message: 'User created successfully',
      user: serializeUser(user),
    });
  } catch (error) {
    return res.status(500).json({ message: 'Signup failed', error: error.message });
  }
});

router.post('/login', async (req, res) => {
  try {
    const { email, password } = req.body || {};
    const normalizedEmail = normalizeEmail(email);

    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: 'Enter a valid email address' });
    }

    if (!password) {
      return res.status(400).json({ message: 'Password is required' });
    }

    const user = await User.findOne({ email: normalizedEmail });
    if (!user || !user.password) {
      return res.status(401).json({ message: 'Invalid email or password' });
    }

    const isMatch = await bcrypt.compare(String(password), user.password);
    if (!isMatch) {
      return res.status(401).json({ message: 'Invalid email or password' });
    }

    const token = signToken(user);

    return res.status(200).json({
      message: 'Login successful',
      token,
      user: serializeUser(user),
    });
  } catch (error) {
    return res.status(500).json({ message: 'Login failed', error: error.message });
  }
});

router.post('/send-otp', async (req, res) => {
  try {
    const { email } = req.body || {};
    const normalizedEmail = normalizeEmail(email);

    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: 'Enter a valid email address' });
    }

    const user = await User.findOne({ email: normalizedEmail });
    if (!user) {
      return res.status(404).json({ message: 'User not found' });
    }

    const otp = generateOTP();
    user.otp = otp;
    user.otpExpiry = new Date(Date.now() + OTP_EXPIRY_MS);
    await user.save();

    await sendOTP(normalizedEmail, otp);

    return res.status(200).json({ message: 'OTP sent successfully' });
  } catch (error) {
    return res.status(500).json({ message: 'Could not send OTP', error: error.message });
  }
});

router.post('/verify-otp', async (req, res) => {
  try {
    const { email, otp } = req.body || {};
    const normalizedEmail = normalizeEmail(email);

    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: 'Enter a valid email address' });
    }

    if (!otp || String(otp).trim().length !== 6) {
      return res.status(400).json({ message: 'Enter a valid 6 digit OTP' });
    }

    const user = await User.findOne({ email: normalizedEmail });
    if (!user || !user.otp || !user.otpExpiry) {
      return res.status(400).json({ message: 'OTP not found. Please request a new one.' });
    }

    if (user.otp !== String(otp).trim()) {
      return res.status(400).json({ message: 'Invalid OTP' });
    }

    if (user.otpExpiry.getTime() < Date.now()) {
      return res.status(400).json({ message: 'OTP expired' });
    }

    user.otp = '';
    user.otpExpiry = null;
    await user.save();

    return res.status(200).json({ message: 'OTP verified successfully' });
  } catch (error) {
    return res.status(500).json({ message: 'OTP verification failed', error: error.message });
  }
});

router.post('/reset-password', async (req, res) => {
  try {
    const { email, otp, newPassword } = req.body || {};
    const normalizedEmail = normalizeEmail(email);

    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: 'Enter a valid email address' });
    }

    if (!newPassword || String(newPassword).length < 6) {
      return res.status(400).json({ message: 'New password must be at least 6 characters' });
    }

    const submittedOtp = String(otp || '').trim();
    if (!/^\d{6}$/.test(submittedOtp)) {
      return res.status(400).json({ message: 'Enter the 6-digit OTP sent to your email' });
    }

    const user = await User.findOne({ email: normalizedEmail });
    if (!user) {
      return res.status(404).json({ message: 'User not found' });
    }

    if (!user.otp || user.otp !== submittedOtp || !user.otpExpiry || user.otpExpiry.getTime() < Date.now()) {
      return res.status(400).json({ message: 'OTP is invalid or expired. Request a new one.' });
    }

    user.password = await bcrypt.hash(String(newPassword), 10);
    user.otp = '';
    user.otpExpiry = null;
    await user.save();

    return res.status(200).json({ message: 'Password reset successful' });
  } catch (error) {
    return res.status(500).json({ message: 'Reset password failed', error: error.message });
  }
});

router.post('/google', async (req, res) => {
  try {
    const { email, name } = req.body || {};
    const normalizedEmail = normalizeEmail(email);

    if (!isValidEmail(normalizedEmail)) {
      return res.status(400).json({ message: 'Enter a valid email address' });
    }

    if (!name || String(name).trim().length < 2) {
      return res.status(400).json({ message: 'Name is required' });
    }

    let user = await User.findOne({ email: normalizedEmail });

    if (!user) {
      user = await User.create({
        name: String(name).trim(),
        email: normalizedEmail,
        googleId: `google-${Date.now()}`,
        password: '',
      });
    } else if (!user.googleId) {
      user.googleId = `google-${Date.now()}`;
      await user.save();
    }

    const token = signToken(user);

    return res.status(200).json({
      message: 'Google authentication successful',
      token,
      user: serializeUser(user),
    });
  } catch (error) {
    return res.status(500).json({ message: 'Google authentication failed', error: error.message });
  }
});

module.exports = router;
