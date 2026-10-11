const jwt = require("jsonwebtoken");
const env = require("../config/env");
const User = require("../models/User");

async function verifyToken(req, res, next) {
  const authHeader = req.headers.authorization || "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : "";

  if (!token) {
    return res.status(401).json({ message: "Unauthorized" });
  }

  try {
    const decoded = jwt.verify(token, env.jwtSecret);
    const userId = decoded.id || decoded.userId;
    const user = await User.findById(userId);

    if (!user) {
      return res.status(401).json({ message: "Unauthorized" });
    }

    if (user.isBlocked) {
      return res.status(403).json({ message: "Your account has been blocked by admin" });
    }

    req.user = user;
    req.auth = {
      id: user._id.toString(),
      role: user.role,
    };
    next();
  } catch (error) {
    return res.status(401).json({ message: "Unauthorized" });
  }
}

function requireAdmin(req, res, next) {
  if (!req.user || req.user.role !== "admin") {
    return res.status(403).json({ message: "Admin access required" });
  }

  next();
}

const authMiddleware = verifyToken;
const adminMiddleware = requireAdmin;

module.exports = { verifyToken, requireAdmin, authMiddleware, adminMiddleware };
