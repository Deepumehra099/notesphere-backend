const mongoose = require("mongoose");

const gigSchema = new mongoose.Schema(
  {
    title: { type: String, required: true, trim: true },
    description: { type: String, required: true, trim: true },
    category: { type: String, default: "General" },
    price: { type: Number, required: true },
    featured: { type: Boolean, default: false },
    seller: {
      userId: { type: mongoose.Schema.Types.ObjectId, ref: "User", required: true },
      name: { type: String, required: true },
      email: { type: String, required: true },
    },
  },
  { timestamps: true }
);

module.exports = mongoose.model("Gig", gigSchema);
