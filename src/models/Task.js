const mongoose = require("mongoose");

const taskParticipantSchema = new mongoose.Schema(
  {
    userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true },
    name: { type: String, required: true },
    email: { type: String, required: true },
  },
  { _id: false }
);

const taskSchema = new mongoose.Schema(
  {
    title: { type: String, required: true, trim: true },
    description: { type: String, required: true, trim: true },
    budget: { type: Number, required: true },
    location: { type: String, default: "" },
    mode: { type: String, enum: ["remote", "nearby"], default: "remote" },
    urgency: { type: String, enum: ["normal", "urgent"], default: "normal" },
    boosted: { type: Boolean, default: false, index: true },
    trendingScore: { type: Number, default: 0, index: true },
    status: {
      type: String,
      enum: ["open", "accepted", "submitted", "completed", "cancelled"],
      default: "open",
      index: true,
    },
    createdBy: {
      userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true },
      name: { type: String, required: true },
      email: { type: String, required: true },
    },
    acceptedBy: { type: taskParticipantSchema, default: null },
    submission: {
      text: { type: String, default: "", trim: true },
      fileUrl: { type: String, default: "" },
      submittedAt: { type: Date, default: null },
    },
    completedAt: { type: Date, default: null },
    escrow: {
      amount: { type: Number, default: 0 },
      fundedTransactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", default: null },
      payoutTransactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", default: null },
    },
  },
  { timestamps: true }
);

module.exports = mongoose.model("Task", taskSchema);
