const crypto = require("crypto");
const Wallet = require("../models/Wallet");
const Transaction = require("../models/Transaction");
const User = require("../models/User");
const Note = require("../models/Note");
const Task = require("../models/Task");
const { notifyUser } = require("./notificationService");

function createReference(prefix) {
  return `${prefix}_${crypto.randomBytes(6).toString("hex")}`;
}

async function syncLegacyWallet(userId, wallet) {
  await User.findByIdAndUpdate(userId, {
    $set: {
      "wallet.balance": wallet.balance,
      "wallet.earnings": wallet.totalDeposited,
      "wallet.pendingAmount": wallet.pendingAmount,
      "wallet.totalWithdrawn": wallet.totalWithdrawn,
    },
  });
}

async function ensureWallet(userId) {
  const user = await User.findById(userId);

  if (!user) {
    throw new Error("User not found");
  }

  let wallet = await Wallet.findOne({ userId });

  if (!wallet) {
    wallet = await Wallet.create({
      userId,
      balance: Math.max(0, Number(user.wallet?.balance || 0)),
      totalDeposited: Math.max(
        Number(user.wallet?.earnings || 0),
        Number(user.wallet?.balance || 0),
        0
      ),
      totalWithdrawn: Math.max(0, Number(user.wallet?.totalWithdrawn || 0)),
      pendingAmount: Math.max(0, Number(user.wallet?.pendingAmount || 0)),
    });
  }

  await syncLegacyWallet(userId, wallet);
  return wallet;
}

function buildWalletResponse(wallet) {
  return {
    balance: wallet.balance,
    totalDeposited: wallet.totalDeposited,
    totalWithdrawn: wallet.totalWithdrawn,
    pendingAmount: wallet.pendingAmount,
  };
}

async function createTransaction({
  userId,
  type,
  source,
  category,
  amount,
  status,
  title,
  referenceId,
  metadata = {},
}) {
  return Transaction.create({
    userId,
    type,
    source,
    category,
    amount,
    status,
    title,
    referenceId: referenceId || createReference(source),
    metadata,
  });
}

async function creditWallet({ userId, amount, source, category, status = "completed", title, metadata = {} }) {
  const wallet = await ensureWallet(userId);
  wallet.balance += amount;
  wallet.totalDeposited += amount;
  await wallet.save();
  await syncLegacyWallet(userId, wallet);

  const transaction = await createTransaction({
    userId,
    type: "credit",
    source,
    category,
    amount,
    status,
    title,
    metadata,
  });

  return { wallet, transaction };
}

async function debitWallet({ userId, amount, source, category, status = "completed", title, metadata = {} }) {
  const wallet = await ensureWallet(userId);

  if (wallet.balance < amount) {
    const error = new Error("Insufficient wallet balance");
    error.statusCode = 400;
    throw error;
  }

  wallet.balance -= amount;
  await wallet.save();
  await syncLegacyWallet(userId, wallet);

  const transaction = await createTransaction({
    userId,
    type: "debit",
    source,
    category,
    amount,
    status,
    title,
    metadata,
  });

  return { wallet, transaction };
}

async function requestWithdrawal(userId, amount, metadata = {}) {
  const wallet = await ensureWallet(userId);

  if (wallet.balance < amount) {
    const error = new Error("Insufficient wallet balance");
    error.statusCode = 400;
    throw error;
  }

  wallet.balance -= amount;
  wallet.pendingAmount += amount;
  await wallet.save();
  await syncLegacyWallet(userId, wallet);

  const transaction = await createTransaction({
    userId,
    type: "debit",
    source: "withdraw",
    category: "withdraw",
    amount,
    status: "pending",
    title: "Withdrawal Request",
    metadata,
  });

  return { wallet, transaction };
}

async function approveWithdrawal(transactionId, adminUserId) {
  const transaction = await Transaction.findById(transactionId);

  if (!transaction || transaction.source !== "withdraw") {
    const error = new Error("Withdrawal request not found");
    error.statusCode = 404;
    throw error;
  }

  if (transaction.status !== "pending") {
    const error = new Error("Withdrawal request already processed");
    error.statusCode = 400;
    throw error;
  }

  const wallet = await ensureWallet(transaction.userId);
  wallet.pendingAmount = Math.max(0, wallet.pendingAmount - transaction.amount);
  wallet.totalWithdrawn += transaction.amount;
  await wallet.save();
  await syncLegacyWallet(transaction.userId, wallet);

  transaction.status = "completed";
  transaction.metadata = {
    ...(transaction.metadata || {}),
    approvedBy: adminUserId.toString(),
    approvedAt: new Date().toISOString(),
  };
  await transaction.save();
  await notifyUser(transaction.userId, "Withdrawal approved", `${transaction.amount} tokens have been marked as paid.`);

  return { wallet, transaction };
}

async function rejectWithdrawal(transactionId, adminUserId, reason = "") {
  const transaction = await Transaction.findById(transactionId);

  if (!transaction || transaction.source !== "withdraw") {
    const error = new Error("Withdrawal request not found");
    error.statusCode = 404;
    throw error;
  }

  if (transaction.status !== "pending") {
    const error = new Error("Withdrawal request already processed");
    error.statusCode = 400;
    throw error;
  }

  const wallet = await ensureWallet(transaction.userId);
  wallet.pendingAmount = Math.max(0, wallet.pendingAmount - transaction.amount);
  wallet.balance += transaction.amount;
  await wallet.save();
  await syncLegacyWallet(transaction.userId, wallet);

  transaction.status = "rejected";
  transaction.metadata = {
    ...(transaction.metadata || {}),
    rejectedBy: adminUserId.toString(),
    rejectedAt: new Date().toISOString(),
    reason: reason || "Rejected by admin",
  };
  await transaction.save();

  const refundTransaction = await createTransaction({
    userId: transaction.userId,
    type: "credit",
    source: "refund",
    category: "refund",
    amount: transaction.amount,
    status: "completed",
    title: "Withdrawal Refund",
    metadata: {
      withdrawalReferenceId: transaction.referenceId,
      rejectedBy: adminUserId.toString(),
      reason: reason || "Rejected by admin",
    },
  });
  await notifyUser(transaction.userId, "Withdrawal rejected", reason || "Your withdrawal request was rejected and refunded.");

  return { wallet, transaction, refundTransaction };
}

async function addBonus(userId, amount, adminUserId, note = "") {
  const { wallet, transaction } = await creditWallet({
    userId,
    amount,
    source: "bonus",
    category: "bonus",
    status: "completed",
    title: "Admin Bonus",
    metadata: {
      adminUserId: adminUserId.toString(),
      note,
    },
  });
  await notifyUser(userId, "Bonus received", `${amount} tokens were added to your wallet by admin.`);

  return { wallet, transaction };
}

async function purchaseNote({ buyerId, noteId }) {
  const note = await Note.findById(noteId);

  if (!note || note.status !== "approved") {
    const error = new Error("Note not found");
    error.statusCode = 404;
    throw error;
  }

  const alreadyBought = note.buyers.some((buyer) => String(buyer.userId) === String(buyerId));
  const isSeller = String(note.seller.userId) === String(buyerId);

  if (alreadyBought || isSeller || note.price <= 0) {
    return { note, alreadyBought: true };
  }

  await debitWallet({
    userId: buyerId,
    amount: note.price,
    source: "purchase",
    category: "purchase",
    status: "completed",
    title: `Purchased note: ${note.title}`,
    metadata: { noteId: note._id.toString(), sellerId: note.seller.userId.toString() },
  });

  const sellerCredit = await creditWallet({
    userId: note.seller.userId,
    amount: note.price,
    source: "sale",
    category: "earning",
    status: "completed",
    title: `Note sold: ${note.title}`,
    metadata: { noteId: note._id.toString(), buyerId: buyerId.toString() },
  });

  note.buyers.push({
    userId: buyerId,
    purchasedAt: new Date(),
    transactionId: sellerCredit.transaction._id,
  });
  await note.save();

  await User.findByIdAndUpdate(note.seller.userId, { $inc: { "stats.downloads": 1 } });
  await notifyUser(note.seller.userId, "Note purchased", `${note.title} was purchased by a learner.`);

  return { note, alreadyBought: false };
}

async function fundTaskEscrow({ taskId, creatorId, amount, title }) {
  const result = await debitWallet({
    userId: creatorId,
    amount,
    source: "task",
    category: "task",
    status: "completed",
    title,
    metadata: { taskId: taskId.toString(), stage: "escrow" },
  });

  await Task.findByIdAndUpdate(taskId, {
    $set: {
      "escrow.amount": amount,
      "escrow.fundedTransactionId": result.transaction._id,
    },
  });

  return result;
}

async function payoutTask({ taskId, workerId, amount, completedBy }) {
  const creditResult = await creditWallet({
    userId: workerId,
    amount,
    source: "task",
    category: "earning",
    status: "completed",
    title: "Task completed payout",
    metadata: {
      taskId: taskId.toString(),
      completedBy: completedBy.toString(),
    },
  });

  await Task.findByIdAndUpdate(taskId, {
    $set: {
      "escrow.payoutTransactionId": creditResult.transaction._id,
      completedAt: new Date(),
      status: "completed",
    },
  });

  await User.findByIdAndUpdate(workerId, { $inc: { "stats.tasksCompleted": 1 } });
  return creditResult;
}

module.exports = {
  addBonus,
  approveWithdrawal,
  buildWalletResponse,
  creditWallet,
  debitWallet,
  ensureWallet,
  fundTaskEscrow,
  payoutTask,
  purchaseNote,
  rejectWithdrawal,
  requestWithdrawal,
};
