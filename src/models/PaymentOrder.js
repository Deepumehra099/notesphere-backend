const mongoose = require("mongoose");

const paymentOrderSchema = new mongoose.Schema(
  {
    userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true, index: true },
    amount: { type: Number, required: true, min: 1 },
    walletAmount: { type: Number, min: 1 },
    // Read legacy orders during transition; new orders store walletAmount.
    tokens: { type: Number, min: 1 },
    currency: { type: String, default: "INR" },
    provider: { type: String, default: "razorpay" },
    providerOrderId: { type: String, required: true, unique: true, index: true },
    providerPaymentId: { type: String, default: "" },
    providerSignature: { type: String, default: "" },
    status: { type: String, enum: ["created", "processing", "paid", "failed", "cancelled", "refunded"], default: "created", index: true },
    transactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", default: null },
    metadata: { type: mongoose.Schema.Types.Mixed, default: {} },
  },
  { timestamps: true }
);

paymentOrderSchema.index(
  { providerPaymentId: 1 },
  { unique: true, partialFilterExpression: { providerPaymentId: { $type: "string", $gt: "" } } }
);

module.exports = mongoose.model("PaymentOrder", paymentOrderSchema);
