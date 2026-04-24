const path = require("path");
const dotenv = require("dotenv");

dotenv.config({ path: path.resolve(__dirname, "../../.env") });

const required = ["MONGO_URL", "DB_NAME", "JWT_SECRET"];

for (const key of required) {
  if (!process.env[key]) {
    throw new Error(`Missing required environment variable: ${key}`);
  }
}

module.exports = {
  port: Number(process.env.PORT || 5000),
  mongoUrl: process.env.MONGO_URL,
  dbName: process.env.DB_NAME,
  jwtSecret: process.env.JWT_SECRET,
  adminEmail: process.env.ADMIN_EMAIL || "admin@noteverse.app",
  adminPassword: process.env.ADMIN_PASSWORD || "Admin@123456",
  adminName: process.env.ADMIN_NAME || "NoteVerse Admin",
  cloudinaryCloudName: process.env.CLOUDINARY_CLOUD_NAME || "",
  cloudinaryApiKey: process.env.CLOUDINARY_API_KEY || "",
  cloudinaryApiSecret: process.env.CLOUDINARY_API_SECRET || "",
  appBaseUrl: process.env.APP_BASE_URL || `http://localhost:${Number(process.env.PORT || 5000)}`,
  razorpayKeyId: process.env.RAZORPAY_KEY_ID || process.env.RAZORPAY_KEY || "",
  razorpayKeySecret: process.env.RAZORPAY_KEY_SECRET || "",
  razorpayWebhookSecret: process.env.RAZORPAY_WEBHOOK_SECRET || "",
  allowMockPayments: String(process.env.ALLOW_MOCK_PAYMENTS || "true") === "true",
};
