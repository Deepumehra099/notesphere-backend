const jwt = require("jsonwebtoken");
const env = require("../config/env");

function generateToken(user) {
  const payload =
    typeof user === "object" && user !== null
      ? {
          id: user._id?.toString?.() || user.id || user.userId || "",
          role: user.role || "user",
        }
      : { id: String(user), role: "user" };

  payload.userId = payload.id;
  return jwt.sign(payload, env.jwtSecret, { expiresIn: "7d" });
}

module.exports = { generateToken };
