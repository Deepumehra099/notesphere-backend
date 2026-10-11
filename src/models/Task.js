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
    deadline: { type: String, default: "Flexible", trim: true },
    location: { type: String, default: "" },
    mode: { type: String, enum: ["remote", "nearby"], default: "remote" },
    urgency: { type: String, enum: ["normal", "urgent"], default: "normal" },
    boosted: { type: Boolean, default: false, index: true },
    trendingScore: { type: Number, default: 0, index: true },
    status: {
      type: String,
      enum: ["funding", "open", "accepted", "submitted", "disputed", "completed", "cancelled"],
      default: "open",
      index: true,
    },
    createdBy: {
      userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true },
      name: { type: String, required: true },
      email: { type: String, required: true },
      isVerified: { type: Boolean, default: false },
      eliteBuyer: { type: Boolean, default: false },
      eliteSeller: { type: Boolean, default: false },
    },
    acceptedBy: { type: taskParticipantSchema, default: null },
    chatContext: {
      purpose: { type: String, enum: ["post", "service"], default: undefined },
      category: { type: String, enum: ["task", "requirement", "service"], default: undefined },
    },
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
      refundedTransactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", default: null },
      fundingVersion: { type: Number, default: 0 },
      commissionPercent: { type: Number, default: 0, min: 0, max: 100 },
      expectedCommission: { type: Number, default: 0, min: 0 },
      expectedStudentAmount: { type: Number, default: 0, min: 0 },
      settlementStatus: { type: String, enum: ["none", "held", "releasing", "released", "refunded", "disputed"], default: "none" },
    },
    creationRequestId: { type: String, default: "", trim: true },
    commissionAmount: { type: Number, default: 0, min: 0 },
    sellerPayout: { type: Number, default: 0, min: 0 },
    dispute: {
      openedBy: { type: mongoose.Schema.Types.ObjectId, ref: "User", default: null },
      reason: { type: String, default: "", trim: true },
      openedAt: { type: Date, default: null },
      resolvedBy: { type: mongoose.Schema.Types.ObjectId, ref: "User", default: null },
      resolvedAt: { type: Date, default: null },
      refundAmount: { type: Number, default: 0, min: 0 },
      adminNote: { type: String, default: "", trim: true },
    },
  },
  { timestamps: true }
);

taskSchema.index({ "createdBy.userId": 1, creationRequestId: 1 }, { unique: true, partialFilterExpression: { creationRequestId: { $type: "string", $gt: "" } } });

module.exports = mongoose.model("Task", taskSchema);
