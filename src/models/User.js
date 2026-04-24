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
    passwordHash: { type: String, required: true },
    role: { type: String, enum: ["user", "admin"], default: "user" },
    avatarUrl: { type: String, default: "" },
    bio: { type: String, default: "", trim: true },
    phone: { type: String, default: "", trim: true },
    location: { type: String, default: "", trim: true },
    language: { type: String, default: "en", trim: true },
    skills: [{ type: String, trim: true }],
    upiId: { type: String, default: "", trim: true },
    isBlocked: { type: Boolean, default: false, index: true },
    rating: { type: Number, default: 4.8 },
    wallet: { type: walletSchema, default: () => ({}) },
    stats: { type: statsSchema, default: () => ({}) },
  },
  { timestamps: true }
);

module.exports = mongoose.model("User", userSchema);
