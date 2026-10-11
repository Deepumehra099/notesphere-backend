const crypto = require("crypto");
const Razorpay = require("razorpay");
const env = require("../config/env");
const PaymentOrder = require("../models/PaymentOrder");
const Transaction = require("../models/Transaction");
const { buildWalletResponse, creditWallet, ensureWallet } = require("./walletService");

const WALLET_PACKAGES = [
  { id: "wallet_100", label: "₹100 INR", price: 100, walletAmount: 100 },
  { id: "wallet_250", label: "₹250 INR", price: 250, walletAmount: 250 },
  { id: "wallet_500", label: "₹500 INR", price: 500, walletAmount: 500, popular: true },
  { id: "wallet_1000", label: "₹1,000 INR", price: 1000, walletAmount: 1000 },
  { id: "wallet_2000", label: "₹2,000 INR", price: 2000, walletAmount: 2000 },
  { id: "wallet_5000", label: "₹5,000 INR", price: 5000, walletAmount: 5000 },
];

function getRazorpayClient() {
  if (!env.razorpayKeyId || !env.razorpayKeySecret) {
    const error = new Error("Razorpay deposits are not configured");
    error.statusCode = 503;
    throw error;
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
  const order = await client.orders.create({
    amount: amountPaise,
    currency: "INR",
    receipt: `NoteSphere_${Date.now()}`,
    notes: {
      packageId: pkg.id,
      userId: userId.toString(),
    },
  });
  const providerOrderId = order.id;

  const paymentOrder = await PaymentOrder.create({
    userId,
    amount: pkg.price,
    walletAmount: pkg.walletAmount,
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
      key_id: env.razorpayKeyId,
      package: pkg,
    },
  };
}

function verifySignature(orderId, paymentId, signature) {
  if (!env.razorpayKeySecret || !orderId || !paymentId || !signature) return false;
  const expected = crypto
    .createHmac("sha256", env.razorpayKeySecret)
    .update(`${orderId}|${paymentId}`)
    .digest("hex");

  const expectedBuffer = Buffer.from(expected, "hex");
  const signatureBuffer = Buffer.from(signature, "hex");
  return expectedBuffer.length === signatureBuffer.length
    && crypto.timingSafeEqual(expectedBuffer, signatureBuffer);
}

async function verifyWebhookSignature(rawBody, signature) {
  if (!env.razorpayWebhookSecret || !rawBody || !signature) return false;
  const expected = crypto
    .createHmac("sha256", env.razorpayWebhookSecret)
    .update(rawBody)
    .digest("hex");
  const expectedBuffer = Buffer.from(expected, "hex");
  const signatureBuffer = Buffer.from(signature, "hex");
  return expectedBuffer.length === signatureBuffer.length
    && crypto.timingSafeEqual(expectedBuffer, signatureBuffer);
}

async function creditVerifiedPayment({ orderId, paymentId, signature = "" }) {
  const paymentOrder = await PaymentOrder.findOne({ providerOrderId: orderId });

  if (!paymentOrder) {
    const error = new Error("Payment order not found");
    error.statusCode = 404;
    throw error;
  }

  if (paymentOrder.status === "paid") {
    const [wallet, transaction] = await Promise.all([
      ensureWallet(paymentOrder.userId),
      Transaction.findById(paymentOrder.transactionId),
    ]);
    return { paymentOrder, alreadyPaid: true, wallet: buildWalletResponse(wallet), transaction };
  }

  let claimedOrder = paymentOrder;
  if (paymentOrder.status === "created") {
    claimedOrder = await PaymentOrder.findOneAndUpdate(
      { _id: paymentOrder._id, status: "created" },
      {
        $set: {
          status: "processing",
          providerPaymentId: paymentId,
          providerSignature: signature,
        },
      },
      { new: true }
    );
    if (!claimedOrder) {
      claimedOrder = await PaymentOrder.findOne({ _id: paymentOrder._id });
    }
  }
  if (
    !claimedOrder
    || claimedOrder.status !== "processing"
    || claimedOrder.providerPaymentId !== paymentId
  ) {
    const error = new Error("Payment order is already being processed or is not payable");
    error.statusCode = 409;
    throw error;
  }

  const { wallet, transaction } = await creditWallet({
    userId: claimedOrder.userId,
    amount: Number(claimedOrder.walletAmount ?? claimedOrder.tokens ?? 0),
    source: "deposit",
    category: "deposit",
    title: "Wallet deposit via Razorpay",
    status: "completed",
    metadata: {
      providerOrderId: orderId,
      providerPaymentId: paymentId,
      packageId: claimedOrder.metadata?.packageId || "",
    },
    idempotencyKey: `razorpay:${orderId}`,
  });
  const paidOrder = await PaymentOrder.findOneAndUpdate(
    { _id: claimedOrder._id, status: "processing" },
    { $set: { status: "paid", transactionId: transaction._id } },
    { new: true }
  );
  if (!paidOrder) {
    throw new Error("Wallet was credited but payment order could not be finalized; reconciliation is required");
  }
  return { paymentOrder: paidOrder, wallet, transaction };
}

async function verifyDepositPayment({ orderId, paymentId, signature, userId }) {
  const paymentOrder = await PaymentOrder.findOne({ providerOrderId: orderId, userId });
  if (!paymentOrder) {
    const error = new Error("Payment order not found");
    error.statusCode = 404;
    throw error;
  }
  if (paymentOrder.status === "paid") {
    return { paymentOrder, alreadyPaid: true };
  }
  if (!verifySignature(orderId, paymentId, signature)) {
    const error = new Error("Invalid Razorpay signature");
    error.statusCode = 400;
    throw error;
  }
  const client = getRazorpayClient();
  const payment = await client.payments.fetch(paymentId);
  if (
    payment.order_id !== orderId
    || Number(payment.amount) !== Number(paymentOrder.metadata?.amountPaise)
    || payment.currency !== paymentOrder.currency
    || payment.status !== "captured"
  ) {
    const error = new Error("Razorpay payment is not captured for this order");
    error.statusCode = 400;
    throw error;
  }
  return creditVerifiedPayment({ orderId, paymentId, signature });
}

async function handleRazorpayWebhook({ rawBody, signature, event }) {
  if (!await verifyWebhookSignature(rawBody, signature)) {
    const error = new Error("Invalid Razorpay webhook signature");
    error.statusCode = 400;
    throw error;
  }
  const paymentEntity = event?.payload?.payment?.entity;
  if (event?.event === "payment.captured" && paymentEntity?.order_id && paymentEntity?.id) {
    const order = await PaymentOrder.findOne({ providerOrderId: paymentEntity.order_id });
    if (!order) {
      const error = new Error("Payment order not found");
      error.statusCode = 404;
      throw error;
    }
    if (Number(paymentEntity.amount) !== Number(order.metadata?.amountPaise) || paymentEntity.currency !== order.currency) {
      const error = new Error("Captured payment does not match the deposit order");
      error.statusCode = 400;
      throw error;
    }
    if (order.status === "paid") return { alreadyPaid: true };
    if (!paymentEntity.captured) {
      const error = new Error("Razorpay payment is not captured");
      error.statusCode = 400;
      throw error;
    }
    await creditVerifiedPayment({ orderId: paymentEntity.order_id, paymentId: paymentEntity.id });
  } else if (event?.event === "payment.failed" && paymentEntity?.order_id) {
    await PaymentOrder.findOneAndUpdate(
      { providerOrderId: paymentEntity.order_id, status: "created" },
      {
        $set: {
          "metadata.lastPaymentFailure": {
            paymentId: paymentEntity.id || "",
            code: paymentEntity.error_code || "",
            description: paymentEntity.error_description || "",
            recordedAt: new Date(),
          },
        },
      }
    );
  }
  return { received: true };
}

module.exports = {
  WALLET_PACKAGES,
  createDepositOrder,
  handleRazorpayWebhook,
  resolvePackage,
  verifyWebhookSignature,
  verifyDepositPayment,
};
