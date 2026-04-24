const buckets = new Map();

function rateLimit({ windowMs = 60 * 1000, limit = 10, keyPrefix = "global" } = {}) {
  return (req, res, next) => {
    const key = `${keyPrefix}:${req.ip}:${req.user?._id?.toString?.() || "guest"}`;
    const now = Date.now();
    const current = buckets.get(key);

    if (!current || now > current.resetAt) {
      buckets.set(key, { count: 1, resetAt: now + windowMs });
      return next();
    }

    if (current.count >= limit) {
      return res.status(429).json({
        message: "Too many requests. Please try again shortly.",
      });
    }

    current.count += 1;
    buckets.set(key, current);
    return next();
  };
}

module.exports = { rateLimit };
