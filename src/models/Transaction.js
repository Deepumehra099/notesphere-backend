const mongoose = require("mongoose");

const transactionSchema = new mongoose.Schema(
  {
    userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true, index: true },
    type: { type: String, enum: ["credit", "debit"], required: true },
    source: {
      type: String,
      enum: ["deposit", "withdraw", "task", "purchase", "sale", "refund", "bonus", "admin"],
      required: true,
      index: true,
    },
    amount: { type: Number, required: true, min: 0 },
    status: {
      type: String,
      enum: ["pending", "completed", "failed", "rejected"],
      default: "completed",
      index: true,
    },
    title: { type: String, default: "", trim: true },
    referenceId: { type: String, default: "", trim: true, index: true },
    category: {
      type: String,
      enum: ["deposit", "withdraw", "task", "purchase", "earning", "refund", "bonus", "admin"],
      required: true,
      index: true,
    },
    metadata: { type: mongoose.Schema.Types.Mixed, default: {} },
  },
  { timestamps: true }
);

module.exports = mongoose.model("Transaction", transactionSchema);
