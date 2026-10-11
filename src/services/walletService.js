const crypto = require("crypto");
const Wallet = require("../models/Wallet");
const Transaction = require("../models/Transaction");
const User = require("../models/User");
const Note = require("../models/Note");
const { changeTokens, getPlatformCommissionPercent, qualifyReferral } = require("./tokenService");
const Task = require("../models/Task");
const Withdrawal = require("../models/Withdrawal");
const PlatformLedgerEntry = require("../models/PlatformLedgerEntry");
const env = require("../config/env");
const { notifyUser } = require("./notificationService");

function createReference(prefix) {
  return `${prefix}_${crypto.randomBytes(6).toString("hex")}`;
}

async function syncLegacyWallet(userId, wallet) {
  await User.findByIdAndUpdate(userId, {
    $set: {
      "wallet.balance": wallet.balance,
      "wallet.earnings": wallet.totalEarned || 0,
      "wallet.pendingAmount": wallet.pendingAmount,
      "wallet.totalWithdrawn": wallet.totalWithdrawn,
      walletBalance: wallet.balance,
      pendingBalance: wallet.pendingAmount,
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
      totalEarned: Math.max(0, Number(user.wallet?.earnings || 0)),
      totalSpent: 0,
      totalWithdrawn: Math.max(0, Number(user.wallet?.totalWithdrawn || 0)),
      pendingAmount: Math.max(0, Number(user.wallet?.pendingAmount || 0)),
      heldBalance: 0,
      lockedBalance: 0,
    });
  }

  await syncLegacyWallet(userId, wallet);
  return wallet;
}

function buildWalletResponse(wallet) {
  return {
    availableBalance: wallet.balance,
    balance: wallet.balance,
    heldBalance: wallet.heldBalance || 0,
    pendingBalance: wallet.pendingAmount,
    freezeBalance: wallet.pendingAmount,
    lockedBalance: wallet.lockedBalance || 0,
    totalEarned: wallet.totalEarned || 0,
    totalDeposited: wallet.totalDeposited,
    totalSpent: wallet.totalSpent || 0,
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
  idempotencyKey = "",
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
    idempotencyKey,
    metadata,
  });
}

async function creditWallet({ userId, amount, source, category, status = "completed", title, metadata = {}, idempotencyKey = "" }) {
  if (!Number.isSafeInteger(amount) || amount <= 0) {
    const error = new Error("Credit amount must be a positive whole number");
    error.statusCode = 400;
    throw error;
  }
  await ensureWallet(userId);
  const depositCredit = source === "deposit";
  const query = idempotencyKey
    ? { userId, creditedOperationKeys: { $ne: idempotencyKey } }
    : { userId };
  const update = {
    $inc: { balance: amount, [depositCredit ? "totalDeposited" : "totalEarned"]: amount },
  };
  if (idempotencyKey) update.$addToSet = { creditedOperationKeys: idempotencyKey };
  let wallet = await Wallet.findOneAndUpdate(query, update, { new: true });
  if (!wallet && idempotencyKey) {
    wallet = await ensureWallet(userId);
    const existingTransaction = await Transaction.findOne({ idempotencyKey });
    if (!existingTransaction) {
      const transaction = await createTransaction({
        userId,
        type: "credit",
        source,
        category,
        amount,
        status,
        title,
        metadata,
        idempotencyKey,
      });
      await syncLegacyWallet(userId, wallet);
      return { wallet, transaction, alreadyCredited: true };
    }
    return { wallet, transaction: existingTransaction, alreadyCredited: true };
  }
  if (!wallet) throw new Error("Wallet credit could not be applied");
  await syncLegacyWallet(userId, wallet);

  let transaction;
  try {
    transaction = await createTransaction({
      userId,
      type: "credit",
      source,
      category,
      amount,
      status,
      title,
      metadata,
      idempotencyKey,
    });
  } catch (error) {
    if (!idempotencyKey || error.code !== 11000) throw error;
    transaction = await Transaction.findOne({ idempotencyKey });
    if (!transaction) throw error;
  }
  return { wallet, transaction };
}

async function debitWallet({ userId, amount, source, category, status = "completed", title, metadata = {} }) {
  if (!Number.isSafeInteger(amount) || amount <= 0) {
    const error = new Error("Debit amount must be a positive whole number");
    error.statusCode = 400;
    throw error;
  }
  await ensureWallet(userId);
  let wallet = await Wallet.findOneAndUpdate(
    { userId, balance: { $gte: amount } },
    { $inc: { balance: -amount, totalSpent: amount } },
    { new: true }
  );
  if (!wallet) {
    const error = new Error("Insufficient wallet balance");
    error.statusCode = 400;
    throw error;
  }
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
  const activeWithdrawal = await Withdrawal.exists({
    userId,
    status: { $in: ["pending", "processing", "completing"] },
  });
  if (activeWithdrawal) {
    const error = new Error("You already have an active withdrawal request");
    error.statusCode = 400;
    throw error;
  }

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

  const withdrawal = await Withdrawal.create({
    userId,
    amount,
    method: metadata.method === "bank" ? "bank" : "upi",
    accountLabel: String(metadata.accountLabel || "").trim(),
    accountRef: String(metadata.accountRef || metadata.upiId || "").trim(),
    referenceId: transaction.referenceId,
    status: "pending",
    transactionId: transaction._id,
    metadata,
  });

  return { wallet, transaction, withdrawal };
}

async function completeWithdrawal(transactionId, adminUserId) {
  const withdrawal = await Withdrawal.findOneAndUpdate(
    { transactionId, status: "processing" },
    { $set: { status: "completing" } },
    { new: true }
  );
  if (!withdrawal) {
    const error = new Error("Withdrawal is not processing");
    error.statusCode = 400;
    throw error;
  }

  const transaction = await Transaction.findById(transactionId);

  if (!transaction || transaction.source !== "withdraw") {
    await Withdrawal.findByIdAndUpdate(withdrawal._id, { $set: { status: "processing" } });
    const error = new Error("Withdrawal request not found");
    error.statusCode = 404;
    throw error;
  }

  if (transaction.status !== "pending") {
    await Withdrawal.findByIdAndUpdate(withdrawal._id, { $set: { status: "processing" } });
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
    completedBy: adminUserId.toString(),
    completedAt: new Date().toISOString(),
  };
  await transaction.save();
  await Withdrawal.findOneAndUpdate(
    { transactionId: transaction._id },
    {
      $set: {
        status: "completed",
        adminRemark: "Withdrawal completed",
        "metadata.completedBy": adminUserId.toString(),
        "metadata.completedAt": new Date().toISOString(),
      },
    }
  );
  await notifyUser(transaction.userId, "Withdrawal completed", `₹${transaction.amount} has been paid from your INR wallet.`);

  return { wallet, transaction };
}

async function rejectWithdrawal(transactionId, adminUserId, reason = "") {
  const withdrawal = await Withdrawal.findOneAndUpdate(
    { transactionId, status: "pending" },
    { $set: { status: "completing" } },
    { new: true }
  );
  if (!withdrawal) {
    const error = new Error("Withdrawal request already processed");
    error.statusCode = 400;
    throw error;
  }

  const transaction = await Transaction.findById(transactionId);

  if (!transaction || transaction.source !== "withdraw") {
    await Withdrawal.findByIdAndUpdate(withdrawal._id, { $set: { status: "pending" } });
    const error = new Error("Withdrawal request not found");
    error.statusCode = 404;
    throw error;
  }

  if (transaction.status !== "pending") {
    await Withdrawal.findByIdAndUpdate(withdrawal._id, { $set: { status: "pending" } });
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
  await Withdrawal.findOneAndUpdate(
    { transactionId: transaction._id },
    {
      $set: {
        status: "rejected",
        adminRemark: reason || "Rejected by admin",
        "metadata.rejectedBy": adminUserId.toString(),
        "metadata.rejectedAt": new Date().toISOString(),
      },
    }
  );

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

async function purchaseNote({ buyerId, noteId }) {
  const note = await Note.findById(noteId);

  if (!note || note.status !== "approved") {
    const error = new Error("Note not found");
    error.statusCode = 404;
    throw error;
  }

  const alreadyBought = note.buyers.some((buyer) => String(buyer.userId) === String(buyerId));
  const isSeller = String(note.seller.userId) === String(buyerId);

  const cost = Math.max(0, Number(note.unlockCost || note.price) || 0);
  if (alreadyBought) {
    await qualifyReferral(buyerId, `premium-note:${note._id}`);
    return { note, alreadyBought: true };
  }
  if (isSeller || cost <= 0) {
    return { note, alreadyBought: true };
  }
  await changeTokens({ userId: buyerId, amount: cost, type: "spent", source: "premium_note", title: `Unlocked note: ${note.title}`, idempotencyKey: `note-unlock:${note._id}:${buyerId}`, metadata: { noteId: note._id.toString() } });
  if (!note.buyers.some((buyer) => String(buyer.userId) === String(buyerId))) note.buyers.push({ userId: buyerId, purchasedAt: new Date() });
  await note.save();
  await qualifyReferral(buyerId, `premium-note:${note._id}`);
  return { note, alreadyBought: false };
}

async function fundTaskEscrow({ taskId, creatorId, amount, title }) {
  if (!Number.isSafeInteger(amount) || amount <= 0) {
    const error = new Error("Task budget must be a positive whole number");
    error.statusCode = 400;
    throw error;
  }
  const taskKey = taskId.toString();
  const holdKey = `task-escrow-hold:${taskKey}`;
  const commissionPercent = await getPlatformCommissionPercent();
  const expectedCommission = Math.min(amount, Math.round(amount * commissionPercent / 100));
  const expectedStudentAmount = amount - expectedCommission;
  await ensureWallet(creatorId);
  const claimedTask = await Task.findOneAndUpdate(
    { _id: taskId, status: "funding", "escrow.settlementStatus": "none" },
    { $set: { "escrow.amount": amount, "escrow.fundingVersion": 2, "escrow.settlementStatus": "releasing", "escrow.commissionPercent": commissionPercent, "escrow.expectedCommission": expectedCommission, "escrow.expectedStudentAmount": expectedStudentAmount } },
    { new: true }
  );
  if (!claimedTask) {
    const existingTask = await Task.findById(taskId);
    if (existingTask?.status === "open" && existingTask.escrow?.settlementStatus === "held") {
      return { wallet: await ensureWallet(creatorId), transaction: await Transaction.findOne({ idempotencyKey: holdKey }), alreadyFunded: true };
    }
    const error = new Error("Task funding is already being processed");
    error.statusCode = 409;
    throw error;
  }
  const wallet = await Wallet.findOneAndUpdate(
    { userId: creatorId, balance: { $gte: amount }, heldOperationKeys: { $ne: holdKey } },
    { $inc: { balance: -amount, heldBalance: amount }, $addToSet: { heldOperationKeys: holdKey } },
    { new: true }
  );
  if (!wallet) {
    await Task.findOneAndUpdate({ _id: taskId, "escrow.settlementStatus": "releasing" }, { $set: { "escrow.settlementStatus": "none" } });
    const error = new Error("Insufficient wallet balance to fund this task");
    error.statusCode = 400;
    throw error;
  }
  let transaction;
  try {
    await syncLegacyWallet(creatorId, wallet);
    transaction = await createTransaction({
      userId: creatorId,
      type: "debit",
      source: "task",
      category: "task",
      amount,
      status: "held",
      title: `Task escrow hold: ${title}`,
      idempotencyKey: holdKey,
      metadata: { taskId: taskKey, userId: creatorId.toString(), budget: amount, commissionPercent, expectedCommission, expectedStudentAmount, stage: "escrow_hold" },
    });

    const updatedTask = await Task.findOneAndUpdate({ _id: taskId, status: "funding", "escrow.settlementStatus": "releasing" }, {
      $set: {
        "escrow.amount": amount,
        "escrow.fundedTransactionId": transaction._id,
        "escrow.fundingVersion": 2,
        "escrow.settlementStatus": "held",
        status: "open",
      },
    }, { new: true });
    if (!updatedTask) throw new Error("Task funding could not be linked");
  } catch (error) {
    const refundedWallet = await Wallet.findOneAndUpdate(
      { userId: creatorId, heldBalance: { $gte: amount }, heldOperationKeys: holdKey },
      { $inc: { balance: amount, heldBalance: -amount }, $pull: { heldOperationKeys: holdKey } },
      { new: true }
    );
    if (!refundedWallet) {
      throw new Error("Task funding failed and reserved funds could not be restored; manual reconciliation is required");
    }
    await syncLegacyWallet(creatorId, refundedWallet);
    if (transaction) {
      await Transaction.findByIdAndUpdate(transaction._id, { $set: { status: "failed" } });
      await createTransaction({
        userId: creatorId,
        type: "credit",
        source: "refund",
        category: "task",
        amount,
        status: "completed",
        title: "Task funding reversal",
        metadata: { taskId: taskId.toString(), originalTransactionId: transaction._id.toString() },
      });
    }
    await Task.findOneAndUpdate({ _id: taskId, "escrow.settlementStatus": "releasing" }, { $set: { "escrow.settlementStatus": "none" } });
    throw error;
  }
  return { wallet, transaction };
}

async function cancelTaskEscrow({ taskId, buyerId }) {
  const taskKey = taskId.toString();
  const task = await Task.findOneAndUpdate(
    { _id: taskId, "createdBy.userId": buyerId, status: "open", "escrow.settlementStatus": "held" },
    { $set: { status: "cancelled", "escrow.settlementStatus": "releasing" } },
    { new: true }
  );
  if (!task) {
    const existing = await Task.findOne({ _id: taskId, "createdBy.userId": buyerId });
    if (existing?.status === "cancelled" && existing.escrow?.settlementStatus === "refunded") {
      return { task: existing, transaction: await Transaction.findOne({ idempotencyKey: `task-escrow-refund:${taskKey}` }), alreadyRefunded: true };
    }
    const error = new Error("Only an unaccepted task with held escrow can be cancelled");
    error.statusCode = existing ? 409 : 404;
    throw error;
  }
  const amount = Number(task.escrow.amount);
  if (!Number.isSafeInteger(amount) || amount <= 0) {
    await Task.findOneAndUpdate({ _id: taskId, "escrow.settlementStatus": "releasing" }, { $set: { status: "open", "escrow.settlementStatus": "held" } });
    throw Object.assign(new Error("Held task budget is invalid"), { statusCode: 409 });
  }
  let wallet = await Wallet.findOneAndUpdate(
    { userId: buyerId, heldBalance: { $gte: amount }, refundedTaskIds: { $ne: taskKey } },
    { $inc: { balance: amount, heldBalance: -amount }, $addToSet: { refundedTaskIds: taskKey } },
    { new: true }
  );
  let refundedWallet = wallet;
  if (!wallet) {
    const alreadyRefundedWallet = await Wallet.findOne({ userId: buyerId, refundedTaskIds: taskKey });
    if (!alreadyRefundedWallet) {
      await Task.findOneAndUpdate({ _id: taskId, "escrow.settlementStatus": "releasing" }, { $set: { status: "open", "escrow.settlementStatus": "held" } });
      throw Object.assign(new Error("Held task funds are unavailable"), { statusCode: 409 });
    }
    refundedWallet = alreadyRefundedWallet;
  }
  await syncLegacyWallet(buyerId, refundedWallet);
  let transaction;
  try {
    transaction = await createTransaction({
      userId: buyerId,
      type: "credit",
      source: "refund",
      category: "refund",
      amount,
      status: "completed",
      title: `Task escrow refund: ${task.title}`,
      idempotencyKey: `task-escrow-refund:${taskKey}`,
      metadata: { taskId: taskKey, stage: "escrow_refund" },
    });
  } catch (error) {
    if (error.code === 11000) transaction = await Transaction.findOne({ idempotencyKey: `task-escrow-refund:${taskKey}` });
    if (!transaction) {
      if (wallet) {
        await Wallet.findOneAndUpdate({ userId: buyerId, refundedTaskIds: taskKey }, { $inc: { balance: -amount, heldBalance: amount }, $pull: { refundedTaskIds: taskKey } });
      }
      await Task.findOneAndUpdate({ _id: taskId, "escrow.settlementStatus": "releasing" }, { $set: { status: "open", "escrow.settlementStatus": "held" } });
      throw error;
    }
  }
  const finalizedTask = await Task.findOneAndUpdate(
    { _id: taskId, status: "cancelled", "escrow.settlementStatus": "releasing" },
    { $set: { "escrow.settlementStatus": "refunded", "escrow.refundedTransactionId": transaction._id } },
    { new: true }
  ) || await Task.findById(taskId);
  return { task: finalizedTask, transaction, alreadyRefunded: !wallet };
}

async function payoutTask({ taskId, workerId, amount, completedBy }) {
  let task = await Task.findOne({
    _id: taskId,
    status: "submitted",
    "escrow.settlementStatus": { $in: ["held", "releasing"] },
  });
  if (!task) {
    const error = new Error("Task is not submitted or escrow has already been settled");
    error.statusCode = 409;
    throw error;
  }
  if (task.escrow.settlementStatus === "held") {
    task = await Task.findOneAndUpdate(
      { _id: taskId, status: "submitted", "escrow.settlementStatus": "held" },
      { $set: { "escrow.settlementStatus": "releasing" } },
      { new: true }
    );
  }
  if (!task) {
    const error = new Error("Task escrow is already being settled");
    error.statusCode = 409;
    throw error;
  }

  async function resolveTaskDispute({ taskId, refundAmount, adminUserId, adminNote = "" }) {
    const task = await Task.findOne({
      _id: taskId,
      status: "disputed",
      "escrow.settlementStatus": { $in: ["disputed", "releasing"] },
    });
    if (!task) {
      const error = new Error("Disputed task not found or already settled");
      error.statusCode = 404;
      throw error;
    }
    if (task.escrow.fundingVersion !== 2 || !task.acceptedBy?.userId) {
      const error = new Error("This task escrow cannot be safely resolved automatically");
      error.statusCode = 409;
      throw error;
    }
    const amount = Number(task.escrow.amount);
    if (!Number.isSafeInteger(refundAmount) || refundAmount < 0 || refundAmount > amount) {
      const error = new Error("Refund amount must be a whole number between zero and the escrow amount");
      error.statusCode = 400;
      throw error;
    }
    let resolutionAdminId = adminUserId;
    let resolutionNote = String(adminNote).trim();
    if (task.escrow.settlementStatus === "disputed") {
      const claimedTask = await Task.findOneAndUpdate(
        { _id: taskId, status: "disputed", "escrow.settlementStatus": "disputed" },
        {
          $set: {
            "escrow.settlementStatus": "releasing",
            "dispute.resolvedBy": adminUserId,
            "dispute.refundAmount": refundAmount,
            "dispute.adminNote": resolutionNote,
          },
        },
        { new: true }
      );
      if (!claimedTask) {
        const error = new Error("Dispute is already being resolved");
        error.statusCode = 409;
        throw error;
      }
    } else {
      if (task.dispute?.refundAmount !== refundAmount) {
        const error = new Error("A previous dispute resolution is being recovered; retry with the original refund amount");
        error.statusCode = 409;
        throw error;
      }
      resolutionAdminId = task.dispute?.resolvedBy || adminUserId;
      resolutionNote = task.dispute?.adminNote || resolutionNote;
    }

    const taskKey = taskId.toString();
    const netAmount = amount - refundAmount;
    const commissionPercent = Number.isFinite(task.escrow?.commissionPercent) ? task.escrow.commissionPercent : await getPlatformCommissionPercent();
    const commissionAmount = Math.min(netAmount, Math.round(netAmount * commissionPercent / 100));
    const sellerPayout = netAmount - commissionAmount;
    let buyerWallet = await Wallet.findOneAndUpdate(
      {
        userId: task.createdBy.userId,
        heldBalance: { $gte: amount },
        settledTaskIds: { $ne: taskKey },
      },
      {
        $inc: { heldBalance: -amount, totalSpent: netAmount },
        $addToSet: { settledTaskIds: taskKey },
      },
      { new: true }
    );
    if (!buyerWallet) {
      buyerWallet = await Wallet.findOne({ userId: task.createdBy.userId });
      if (!buyerWallet || !buyerWallet.settledTaskIds?.includes(taskKey)) {
        await Task.findByIdAndUpdate(taskId, { $set: { "escrow.settlementStatus": "disputed" } });
        const error = new Error("Held task funds are unavailable; dispute remains open");
        error.statusCode = 409;
        throw error;
      }
    }
    await syncLegacyWallet(task.createdBy.userId, buyerWallet);
    await Transaction.findByIdAndUpdate(task.escrow.fundedTransactionId, {
      $set: { status: "completed", metadata: { taskId: taskKey, stage: "escrow_hold" } },
    });

    let refundTransaction = null;
    if (refundAmount > 0) {
      const refunded = await creditWallet({
        userId: task.createdBy.userId,
        amount: refundAmount,
        source: "refund",
        category: "refund",
        title: "Task dispute refund",
        metadata: { taskId: taskKey, adminUserId: resolutionAdminId.toString() },
        idempotencyKey: `task-dispute-refund:${taskKey}`,
      });
      refundTransaction = refunded.transaction;
    }

    let payoutTransaction = null;
    if (sellerPayout > 0) {
      const payout = await creditWallet({
        userId: task.acceptedBy.userId,
        amount: sellerPayout,
        source: "task",
        category: "earning",
        title: "Task dispute resolution payout",
        metadata: { taskId: taskKey, grossAmount: netAmount, commissionAmount, adminUserId: resolutionAdminId.toString() },
        idempotencyKey: `task-dispute-payout:${taskKey}`,
      });
      payoutTransaction = payout.transaction;
    }
    if (commissionAmount > 0) {
      await PlatformLedgerEntry.findOneAndUpdate(
        { referenceId: `TASK_FEE_${taskKey}` },
        {
          $setOnInsert: {
            type: "commission",
            taskId,
            amount: commissionAmount,
            referenceId: `TASK_FEE_${taskKey}`,
            metadata: { buyerId: task.createdBy.userId.toString(), sellerId: task.acceptedBy.userId.toString(), grossAmount: netAmount },
          },
        },
        { upsert: true, new: true }
      );
    }
    const resolvedTask = await Task.findOneAndUpdate(
      { _id: taskId, "escrow.settlementStatus": "releasing" },
      {
        $set: {
          status: "completed",
          "escrow.settlementStatus": refundAmount === amount ? "refunded" : "released",
          "escrow.payoutTransactionId": payoutTransaction?._id || refundTransaction?._id || null,
          commissionAmount,
          sellerPayout,
          completedAt: new Date(),
          "dispute.resolvedBy": resolutionAdminId,
          "dispute.resolvedAt": new Date(),
          "dispute.refundAmount": refundAmount,
          "dispute.adminNote": resolutionNote,
        },
      },
      { new: true }
    );
    if (!resolvedTask) {
      throw new Error("Dispute funds were settled but task status could not be finalized; retry to reconcile");
    }
    return { task: resolvedTask, refundAmount, commissionAmount, sellerPayout };
  }
  if (!Number.isSafeInteger(amount) || amount <= 0 || amount !== task.escrow.amount) {
    const error = new Error("Task escrow amount is invalid");
    error.statusCode = 400;
    throw error;
  }

  const commissionPercent = Number.isFinite(task.escrow?.commissionPercent) ? task.escrow.commissionPercent : await getPlatformCommissionPercent();
  const commissionAmount = Math.min(amount, Math.round(amount * commissionPercent / 100));
  const sellerPayout = amount - commissionAmount;
  if (task.escrow.fundingVersion === 2) {
    const taskKey = taskId.toString();
    let buyerWallet = await Wallet.findOneAndUpdate(
      {
        userId: task.createdBy.userId,
        heldBalance: { $gte: amount },
        settledTaskIds: { $ne: taskKey },
      },
      {
        $inc: { heldBalance: -amount, totalSpent: amount },
        $addToSet: { settledTaskIds: taskKey },
      },
      { new: true }
    );
    if (!buyerWallet) {
      buyerWallet = await Wallet.findOne({ userId: task.createdBy.userId });
      if (!buyerWallet || !buyerWallet.settledTaskIds?.includes(taskKey)) {
        await Task.findByIdAndUpdate(taskId, { $set: { "escrow.settlementStatus": "held" } });
        const error = new Error("Held task funds are unavailable");
        error.statusCode = 409;
        throw error;
      }
    }
    await syncLegacyWallet(task.createdBy.userId, buyerWallet);
    await Transaction.findByIdAndUpdate(task.escrow.fundedTransactionId, {
      $set: { status: "completed", metadata: { taskId: taskId.toString(), stage: "escrow_hold" } },
    });
  }

  const creditResult = sellerPayout > 0
    ? await creditWallet({
      userId: workerId,
      amount: sellerPayout,
      source: "task",
      category: "earning",
      status: "completed",
      title: "Task completed earnings",
      metadata: { taskId: taskId.toString(), completedBy: completedBy.toString(), grossAmount: amount, commissionAmount },
      idempotencyKey: `task-payout:${taskId.toString()}`,
    })
    : { transaction: await Transaction.findOneAndUpdate(
      { idempotencyKey: `task-payout:${taskId.toString()}` },
      { $setOnInsert: { userId: workerId, type: "credit", source: "task", category: "earning", amount: 0, status: "completed", title: "Task completed earnings (zero net payout)", metadata: { taskId: taskId.toString(), completedBy: completedBy.toString(), grossAmount: amount, commissionAmount }, referenceId: createReference("task"), idempotencyKey: `task-payout:${taskId.toString()}` } },
      { upsert: true, new: true }
    ) };

  if (commissionAmount > 0) {
    await PlatformLedgerEntry.findOneAndUpdate(
      { referenceId: `TASK_FEE_${taskId.toString()}` },
      {
        $setOnInsert: {
          type: "commission",
          taskId,
          amount: commissionAmount,
          referenceId: `TASK_FEE_${taskId.toString()}`,
          metadata: { buyerId: task.createdBy.userId.toString(), sellerId: workerId.toString(), grossAmount: amount },
        },
      },
      { upsert: true, new: true }
    );
  }

  const finalizedTask = await Task.findOneAndUpdate(
    { _id: taskId, status: "submitted", "escrow.settlementStatus": "releasing" },
    {
      $set: {
        "escrow.payoutTransactionId": creditResult.transaction._id,
        "escrow.settlementStatus": "released",
        commissionAmount,
        sellerPayout,
        completedAt: new Date(),
        status: "completed",
      },
    }
  );

  if (finalizedTask) await User.findByIdAndUpdate(workerId, { $inc: { "stats.tasksCompleted": 1 } });
  return { ...creditResult, commissionAmount, sellerPayout };
}

module.exports = {
  completeWithdrawal,
  buildWalletResponse,
  cancelTaskEscrow,
  creditWallet,
  debitWallet,
  ensureWallet,
  fundTaskEscrow,
  payoutTask,
  purchaseNote,
  rejectWithdrawal,
  requestWithdrawal,
  resolveTaskDispute,
};
