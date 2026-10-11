const mongoose = require("mongoose");

const withdrawalSchema = new mongoose.Schema(
  {
    userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true, index: true },
    amount: { type: Number, required: true, min: 1 },
    method: { type: String, enum: ["upi", "bank"], required: true, index: true },
    accountLabel: { type: String, default: "", trim: true },
    accountRef: { type: String, default: "", trim: true },
    referenceId: { type: String, required: true, unique: true, index: true },
    status: { type: String, enum: ["pending", "processing", "completing", "completed", "approved", "rejected"], default: "pending", index: true },
    adminRemark: { type: String, default: "", trim: true },
    transactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", required: true, index: true },
    metadata: { type: mongoose.Schema.Types.Mixed, default: {} },
  },
  { timestamps: true }
);

module.exports = mongoose.model("Withdrawal", withdrawalSchema);
