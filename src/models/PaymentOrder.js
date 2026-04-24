const mongoose = require("mongoose");

const paymentOrderSchema = new mongoose.Schema(
  {
    userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true, index: true },
    amount: { type: Number, required: true, min: 1 },
    tokens: { type: Number, required: true, min: 1 },
    currency: { type: String, default: "INR" },
    provider: { type: String, default: "razorpay" },
    providerOrderId: { type: String, required: true, unique: true, index: true },
    providerPaymentId: { type: String, default: "", index: true },
    providerSignature: { type: String, default: "" },
    status: { type: String, enum: ["created", "paid", "failed"], default: "created", index: true },
    transactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", default: null },
    metadata: { type: mongoose.Schema.Types.Mixed, default: {} },
  },
  { timestamps: true }
);

module.exports = mongoose.model("PaymentOrder", paymentOrderSchema);
