const mongoose = require("mongoose");

const creditSettingsSchema = new mongoose.Schema({
  key: { type: String, default: "default", unique: true },
  signupReward: { type: Number, default: 50, min: 0 },
  noteApprovalReward: { type: Number, default: 100, min: 0 },
  referralReward: { type: Number, default: 50, min: 0 },
  referralSignupReward: { type: Number, default: 25, min: 0 },
  premiumNotePrice: { type: Number, default: 0, min: 0 },
  platformCommissionPercent: { type: Number, default: null, min: 0, max: 100 },
}, { timestamps: true });

module.exports = mongoose.model("CreditSettings", creditSettingsSchema);
