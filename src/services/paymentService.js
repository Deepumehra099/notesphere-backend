const crypto = require("crypto");
const Razorpay = require("razorpay");
const env = require("../config/env");
const PaymentOrder = require("../models/PaymentOrder");
const { creditWallet } = require("./walletService");

const WALLET_PACKAGES = [
  { id: "starter_199", label: "Starter Pack", price: 199, tokens: 199 },
  { id: "student_499", label: "Student Pack", price: 499, tokens: 499, popular: true },
  { id: "pro_999", label: "Pro Pack", price: 999, tokens: 999 },
];

function getRazorpayClient() {
  if (!env.razorpayKeyId || !env.razorpayKeySecret) {
    return null;
  }

  return new Razorpay({
    key_id: env.razorpayKeyId,
    key_secret: env.razorpayKeySecret,
  });
}

function resolvePackage(packageId) {
  return WALLET_PACKAGES.find((item) => item.id === packageId) || null;
}

async function createDepositOrder(userId, packageId) {
  const pkg = resolvePackage(packageId);

  if (!pkg) {
    const error = new Error("Wallet package not found");
    error.statusCode = 404;
    throw error;
  }

  const client = getRazorpayClient();
  const amountPaise = pkg.price * 100;

  let providerOrderId = `mock_order_${crypto.randomBytes(8).toString("hex")}`;

  if (client) {
    const order = await client.orders.create({
      amount: amountPaise,
      currency: "INR",
      receipt: `noteverse_${Date.now()}`,
      notes: {
        packageId: pkg.id,
        userId: userId.toString(),
      },
    });

    providerOrderId = order.id;
  }

  const paymentOrder = await PaymentOrder.create({
    userId,
    amount: pkg.price,
    tokens: pkg.tokens,
    currency: "INR",
    providerOrderId,
    metadata: {
      packageId: pkg.id,
      packageLabel: pkg.label,
      amountPaise,
    },
  });

  return {
    paymentOrder,
    package: pkg,
    checkout: {
      order_id: providerOrderId,
      amount: amountPaise,
      currency: "INR",
      key_id: env.razorpayKeyId || "rzp_test_mock",
      package: pkg,
    },
  };
}

function verifySignature(orderId, paymentId, signature) {
  const expected = crypto
    .createHmac("sha256", env.razorpayKeySecret)
    .update(`${orderId}|${paymentId}`)
    .digest("hex");

  return expected === signature;
}

async function verifyDepositPayment({ orderId, paymentId, signature }) {
  const paymentOrder = await PaymentOrder.findOne({ providerOrderId: orderId });

  if (!paymentOrder) {
    const error = new Error("Payment order not found");
    error.statusCode = 404;
    throw error;
  }

  if (paymentOrder.status === "paid") {
    return paymentOrder;
  }

  const isMock = String(orderId).startsWith("mock_order_");

  if (isMock) {
    if (!env.allowMockPayments) {
      const error = new Error("Mock payments are disabled");
      error.statusCode = 400;
      throw error;
    }
  } else {
    if (!env.razorpayKeySecret || !paymentId || !signature || !verifySignature(orderId, paymentId, signature)) {
      const error = new Error("Invalid Razorpay signature");
      error.statusCode = 400;
      throw error;
    }
  }

  const { wallet, transaction } = await creditWallet({
    userId: paymentOrder.userId,
    amount: paymentOrder.tokens,
    source: "deposit",
    category: "deposit",
    title: "Wallet deposit via Razorpay",
    status: "completed",
    metadata: {
      providerOrderId: orderId,
      providerPaymentId: paymentId || "",
      packageId: paymentOrder.metadata?.packageId || "",
    },
  });

  paymentOrder.status = "paid";
  paymentOrder.providerPaymentId = paymentId || paymentOrder.providerPaymentId;
  paymentOrder.providerSignature = signature || paymentOrder.providerSignature;
  paymentOrder.transactionId = transaction._id;
  await paymentOrder.save();

  return { paymentOrder, wallet, transaction };
}

module.exports = {
  WALLET_PACKAGES,
  createDepositOrder,
  resolvePackage,
  verifyDepositPayment,
};
