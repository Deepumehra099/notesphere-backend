const Notification = require("../models/Notification");

async function notifyUser(userId, title, body) {
  if (!userId) {
    return null;
  }

  return Notification.create({
    userId,
    title,
    body,
  });
}

module.exports = { notifyUser };
