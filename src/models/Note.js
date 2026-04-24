const mongoose = require("mongoose");

const buyerSchema = new mongoose.Schema(
  {
    userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true, index: true },
    purchasedAt: { type: Date, default: Date.now },
    transactionId: { type: mongoose.Schema.Types.ObjectId, ref: "Transaction", default: null },
  },
  { _id: false }
);

const noteSchema = new mongoose.Schema(
  {
    title: { type: String, required: true, trim: true },
    subject: { type: String, required: true, trim: true },
    topic: { type: String, required: true, trim: true },
    description: { type: String, default: "", trim: true },
    tags: [{ type: String, trim: true }],
    price: { type: Number, default: 0 },
    thumbnailUrl: { type: String, default: "" },
    pdfUrl: { type: String, default: "" },
    pdfPublicId: { type: String, default: "" },
    thumbnailPublicId: { type: String, default: "" },
    status: { type: String, enum: ["pending", "approved", "rejected"], default: "pending", index: true },
    rejectionReason: { type: String, default: "", trim: true },
    seller: {
      userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true },
      name: { type: String, required: true },
      email: { type: String, required: true },
    },
    downloads: { type: Number, default: 0 },
    buyers: { type: [buyerSchema], default: [] },
  },
  { timestamps: true }
);

module.exports = mongoose.model("Note", noteSchema);
