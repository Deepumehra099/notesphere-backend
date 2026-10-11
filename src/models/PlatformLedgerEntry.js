const mongoose = require("mongoose");

const platformLedgerEntrySchema = new mongoose.Schema(
  {
    type: { type: String, enum: ["commission", "refund", "adjustment"], required: true, index: true },
    taskId: { type: mongoose.Schema.Types.ObjectId, ref: "Task", default: null, index: true },
    amount: { type: Number, required: true, min: 0 },
    currency: { type: String, default: "INR" },
    referenceId: { type: String, required: true, unique: true, index: true },
    metadata: { type: mongoose.Schema.Types.Mixed, default: {} },
  },
  { timestamps: true }
);

platformLedgerEntrySchema.index({ taskId: 1, type: 1 }, { unique: true, partialFilterExpression: { taskId: { $type: "objectId" } } });

module.exports = mongoose.model("PlatformLedgerEntry", platformLedgerEntrySchema);
