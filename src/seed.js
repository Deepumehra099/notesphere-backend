const bcrypt = require("bcrypt");
const env = require("./config/env");
const User = require("./models/User");

function randomUid() {
  return `NV${Math.floor(100000 + Math.random() * 900000)}`;
}

async function ensureAdmin() {
  if (!env.adminEmail || !env.adminPassword) {
    console.warn("Admin bootstrap skipped: configure ADMIN_EMAIL and ADMIN_PASSWORD to provision an admin account.");
    return null;
  }

  const email = env.adminEmail.trim().toLowerCase();
  const admin = await User.findOne({ email });
  if (admin) {
    // Never silently promote or reset the password of an existing account.
    if (admin.role !== "admin") {
      throw new Error(`Configured admin email ${email} belongs to a non-admin account; provision admin access explicitly.`);
    }
    return admin;
  }

  const passwordHash = await bcrypt.hash(env.adminPassword, 12);
  return User.create({
    uid: randomUid(),
    name: env.adminName,
    email,
    passwordHash,
    role: "admin",
    rating: 5,
  });
}

async function seedDatabase() {
  await ensureAdmin();
}

module.exports = { seedDatabase };
