const mongoose = require("mongoose");

const tokenTransactionSchema = new mongoose.Schema({
  userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true, index: true },
  type: { type: String, enum: ["earned", "spent", "refund", "adjustment"], required: true },
  source: { type: String, required: true, index: true },
  amount: { type: Number, required: true, min: 1 },
  balanceAfter: { type: Number, required: true, min: 0 },
  title: { type: String, required: true },
  idempotencyKey: { type: String, required: true, unique: true },
  metadata: { type: mongoose.Schema.Types.Mixed, default: {} },
}, { timestamps: true });

module.exports = mongoose.model("TokenTransaction", tokenTransactionSchema);
