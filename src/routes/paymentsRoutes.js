const express = require("express");
const { verifyToken } = require("../middleware/auth");
const {
  WALLET_PACKAGES,
  createDepositOrder,
  handleRazorpayWebhook,
  verifyDepositPayment,
} = require("../services/paymentService");

const router = express.Router();

router.post("/webhook", async (req, res) => {
  const result = await handleRazorpayWebhook({
    rawBody: req.rawBody,
    signature: String(req.headers["x-razorpay-signature"] || ""),
    event: req.body,
  });
  return res.json({ message: "Webhook received", ...result });
});

router.get("/packages", verifyToken, async (_req, res) => {
  return res.json({ packages: WALLET_PACKAGES });
});

router.post("/create-order", verifyToken, async (req, res) => {
  const packageId = String(req.body.package_id || req.body.packageId || "").trim();
  if (!packageId) {
    return res.status(400).json({ message: "package_id is required" });
  }

  const { checkout } = await createDepositOrder(req.user._id, packageId);
  return res.status(201).json(checkout);
});

router.post("/verify", verifyToken, async (req, res) => {
  const orderId = String(req.body.order_id || req.body.orderId || "").trim();
  const paymentId = String(req.body.payment_id || req.body.paymentId || "").trim();
  const signature = String(req.body.signature || "").trim();

  if (!orderId) {
    return res.status(400).json({ message: "order_id is required" });
  }

  const { paymentOrder, wallet, transaction, alreadyPaid } = await verifyDepositPayment({
    orderId,
    paymentId,
    signature,
    userId: req.user._id,
  });
  return res.json({
    message: alreadyPaid ? "Payment was already verified" : "Payment verified successfully",
    wallet,
    transaction,
    paymentOrderId: paymentOrder._id.toString(),
    status: paymentOrder.status,
  });
});

module.exports = router;
