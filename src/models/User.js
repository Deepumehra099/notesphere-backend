const mongoose = require("mongoose");

const walletSchema = new mongoose.Schema(
  {
    balance: { type: Number, default: 0 },
    earnings: { type: Number, default: 0 },
    pendingAmount: { type: Number, default: 0 },
    totalWithdrawn: { type: Number, default: 0 },
  },
  { _id: false }
);

const statsSchema = new mongoose.Schema(
  {
    notesCount: { type: Number, default: 0 },
    tasksPosted: { type: Number, default: 0 },
    tasksCompleted: { type: Number, default: 0 },
    gigsCount: { type: Number, default: 0 },
    downloads: { type: Number, default: 0 },
  },
  { _id: false }
);

const userSchema = new mongoose.Schema(
  {
    uid: { type: String, required: true, unique: true, index: true },
    name: { type: String, required: true, trim: true },
    email: { type: String, required: true, unique: true, lowercase: true, trim: true, index: true },
    passwordHash: { type: String, default: "" },
    googleId: { type: String, default: "", index: true },
    otp: { type: String, default: "" },
    otpExpiry: { type: Date, default: null },
    role: { type: String, enum: ["user", "admin"], default: "user" },
    tokenBalance: { type: Number, default: 0, min: 0 },
    tokenEarned: { type: Number, default: 0, min: 0 },
    tokenSpent: { type: Number, default: 0, min: 0 },
    referralCode: { type: String, unique: true, sparse: true, index: true },
    referredBy: { type: mongoose.Schema.Types.ObjectId, ref: "User", default: null },
    walletBalance: { type: Number, default: 0, min: 0 },
    pendingBalance: { type: Number, default: 0, min: 0 },
    isVerified: { type: Boolean, default: false, index: true },
    avatarUrl: { type: String, default: "" },
    bio: { type: String, default: "", trim: true },
    phone: { type: String, default: "", trim: true },
    location: { type: String, default: "", trim: true },
    language: { type: String, default: "en", trim: true },
    skills: [{ type: String, trim: true }],
    upiId: { type: String, default: "", trim: true },
    payoutAccounts: [
      {
        type: {
          type: String,
          enum: ["upi", "bank"],
          required: true,
        },
        label: { type: String, default: "", trim: true },
        accountName: { type: String, default: "", trim: true },
        accountNumber: { type: String, default: "", trim: true },
        ifsc: { type: String, default: "", trim: true },
        upiId: { type: String, default: "", trim: true },
        isDefault: { type: Boolean, default: false },
      },
    ],
    isBlocked: { type: Boolean, default: false, index: true },
    rating: { type: Number, default: 4.8 },
    wallet: { type: walletSchema, default: () => ({}) },
    stats: { type: statsSchema, default: () => ({}) },
  },
  { timestamps: true }
);

module.exports = mongoose.model("User", userSchema);
