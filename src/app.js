const express = require("express");
const cors = require("cors");
const morgan = require("morgan");
const path = require("path");
const { connectDb } = require("./config/db");
const authRoutes = require("./routes/authRoutes");
const adminRoutes = require("./routes/adminRoutes");
const homeRoutes = require("./routes/homeRoutes");
const noteRoutes = require("./routes/noteRoutes");
const taskRoutes = require("./routes/taskRoutes");
const gigRoutes = require("./routes/gigRoutes");
const profileRoutes = require("./routes/profileRoutes");
const walletRoutes = require("./routes/walletRoutes");
const transactionRoutes = require("./routes/transactionRoutes");
const paymentsRoutes = require("./routes/paymentsRoutes");
const notificationRoutes = require("./routes/notificationRoutes");
const { seedDatabase } = require("./seed");

const app = express();

app.use(cors());
app.use(express.json());
app.use(morgan("dev"));
app.use("/uploads", express.static(path.resolve(__dirname, "../uploads")));

app.get("/api/health", (_req, res) => {
  res.json({ status: "healthy" });
});

app.use("/api/auth", authRoutes);
app.use("/api/admin", adminRoutes);
app.use("/api/home", homeRoutes);
app.use("/api/notes", noteRoutes);
app.use("/api/tasks", taskRoutes);
app.use("/api/gigs", gigRoutes);
app.use("/api/profile", profileRoutes);
app.use("/api/wallet", walletRoutes);
app.use("/api/transactions", transactionRoutes);
app.use("/api/payments", paymentsRoutes);
app.use("/api/notifications", notificationRoutes);

app.use((err, _req, res, _next) => {
  console.error(err);
  res.status(err.statusCode || 500).json({ message: err.message || "Internal server error" });
});

async function bootstrap() {
  await connectDb();
  await seedDatabase();
}

module.exports = { app, bootstrap };
