const mongoose = require("mongoose");

const depositSchema = new mongoose.Schema(
  {
    userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true, index: true },
    amount: { type: Number, required: true, min: 1 },
    // Legacy field retained so historical deposit documents remain readable.
    tokens: { type: Number, min: 1 },
    method: { type: String, enum: ["upi", "bank"], required: true, index: true },
    provider: { type: String, default: "", trim: true },
    accountName: { type: String, default: "", trim: true },
    accountRef: { type: String, default: "", trim: true },
    paymentReference: { type: String, default: "", trim: true },
    senderDetails: { type: String, default: "", trim: true },
    referenceId: { type: String, required: true, unique: true, index: true },
    status: { type: String, enum: ["pending", "processing", "approved", "rejected"], default: "pending", index: true },
    adminRemark: { type: String, default: "", trim: true },
    transactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", default: null },
    metadata: { type: mongoose.Schema.Types.Mixed, default: {} },
  },
  { timestamps: true }
);

module.exports = mongoose.model("Deposit", depositSchema);
