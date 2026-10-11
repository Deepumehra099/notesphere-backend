const User = require("../models/User");
const TokenTransaction = require("../models/TokenTransaction");
const CreditSettings = require("../models/CreditSettings");
const env = require("../config/env");

async function getTokenSettings() {
  let settings = await CreditSettings.findOne({ key: "default" });
  if (!settings) settings = await CreditSettings.create({ key: "default" });
  return settings;
}

async function qualifyReferral(referredUserId, qualificationKey) {
  const referred = await User.findById(referredUserId);
  if (!referred?.referredBy) return false;
  const settings = await getTokenSettings();
  if (settings.referralReward > 0) await changeTokens({ userId: referred.referredBy, amount: settings.referralReward, type: "earned", source: "referral_reward", title: "Qualified referral reward", idempotencyKey: `referral-qualify:${referred._id}`, metadata: { referredUserId: referred._id.toString(), qualificationKey } });
  return true;
}

async function getPlatformCommissionPercent() {
  const settings = await getTokenSettings();
  return settings.platformCommissionPercent == null ? env.platformCommissionPercent : Number(settings.platformCommissionPercent);
}

async function changeTokens({ userId, amount, type, source, title, idempotencyKey, metadata = {} }) {
  const value = Math.floor(Number(amount));
  if (!Number.isFinite(value) || value < 1) throw Object.assign(new Error("Token amount must be positive"), { statusCode: 400 });
  const existing = await TokenTransaction.findOne({ idempotencyKey });
  if (existing) return { transaction: existing, duplicate: true };
  const delta = type === "spent" ? -value : value;
  const update = await User.findOneAndUpdate(
    { _id: userId, ...(delta < 0 ? { tokenBalance: { $gte: value } } : {}) },
    { $inc: { tokenBalance: delta, ...(delta > 0 ? { tokenEarned: value } : { tokenSpent: value }) } },
    { new: true }
  );
  if (!update) throw Object.assign(new Error(delta < 0 ? "Not enough Tokens" : "User not found"), { statusCode: delta < 0 ? 400 : 404 });
  try {
    const transaction = await TokenTransaction.create({ userId, amount: value, type, source, title, idempotencyKey, metadata, balanceAfter: update.tokenBalance });
    return { transaction, user: update, duplicate: false };
  } catch (error) {
    // A failed ledger insert must never leave the cached balance changed.
    await User.updateOne({ _id: userId }, { $inc: { tokenBalance: -delta, ...(delta > 0 ? { tokenEarned: -value } : { tokenSpent: -value }) } });
    if (error.code !== 11000) throw error;
    // A concurrent retry won the unique idempotency key race; return its row.
    return { transaction: await TokenTransaction.findOne({ idempotencyKey }), duplicate: true };
  }
}

module.exports = { changeTokens, getTokenSettings, getPlatformCommissionPercent, qualifyReferral };
