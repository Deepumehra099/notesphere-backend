const path = require("path");
const dotenv = require("dotenv");

dotenv.config({ path: path.resolve(__dirname, "../../.env") });

const mongoUrl = process.env.MONGO_URL || process.env.MONGO_URI;

if (!mongoUrl) {
  throw new Error("Missing required environment variable: MONGO_URL or MONGO_URI");
}

if (!process.env.JWT_SECRET) {
  throw new Error("Missing required environment variable: JWT_SECRET");
}

module.exports = {
  port: Number(process.env.PORT || 5000),
  mongoUrl,
  dbName: process.env.DB_NAME || process.env.MONGO_DB_NAME || "notesphere",
  jwtSecret: process.env.JWT_SECRET,
  emailUser: process.env.EMAIL_USER || "",
  emailPass: process.env.EMAIL_PASS || "",
  googleClientId: process.env.GOOGLE_CLIENT_ID || "",
  adminEmail: process.env.ADMIN_EMAIL || "",
  adminPassword: process.env.ADMIN_PASSWORD || "",
  adminName: process.env.ADMIN_NAME || "Notesphere Admin",
  cloudinaryCloudName: process.env.CLOUDINARY_CLOUD_NAME || "",
  cloudinaryApiKey: process.env.CLOUDINARY_API_KEY || "",
  cloudinaryApiSecret: process.env.CLOUDINARY_API_SECRET || "",
  appBaseUrl: process.env.APP_BASE_URL || `http://localhost:${Number(process.env.PORT || 5000)}`,
  razorpayKeyId: process.env.RAZORPAY_KEY_ID || process.env.RAZORPAY_KEY || "",
  razorpayKeySecret: process.env.RAZORPAY_KEY_SECRET || "",
  razorpayWebhookSecret: process.env.RAZORPAY_WEBHOOK_SECRET || "",
  platformCommissionPercent: Math.min(100, Math.max(0, Number(process.env.PLATFORM_COMMISSION_PERCENT || 5))),
  depositUpiId: process.env.DEPOSIT_UPI_ID || "",
  depositUpiName: process.env.DEPOSIT_UPI_NAME || "",
  depositBankName: process.env.DEPOSIT_BANK_NAME || "",
  depositBankAccountName: process.env.DEPOSIT_BANK_ACCOUNT_NAME || "",
  depositBankAccountNumber: process.env.DEPOSIT_BANK_ACCOUNT_NUMBER || "",
  depositBankIfsc: process.env.DEPOSIT_BANK_IFSC || "",
};
