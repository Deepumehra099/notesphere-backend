const express = require("express");
const Chat = require("../models/Chat");
const Message = require("../models/Message");
const Task = require("../models/Task");
const User = require("../models/User");
const { verifyToken } = require("../middleware/auth");
const { upload } = require("../middleware/upload");
const { uploadAsset } = require("../services/storageService");

const router = express.Router();

function formatRoom(chat, currentUserId, usersById = new Map()) {
  const task = chat.taskId;
  const ownerId = task?.createdBy?.userId?.toString?.() || "";
  const workerId = task?.acceptedBy?.userId?.toString?.() || "";
  const otherParticipantId = ownerId && workerId
    ? (ownerId === String(currentUserId) ? workerId : workerId === String(currentUserId) ? ownerId : "")
    : (chat.participants || []).map(String).find((participantId) => participantId !== String(currentUserId));
  const otherUser = otherParticipantId ? usersById.get(String(otherParticipantId)) : null;
  const savedContext = task?.chatContext;
  let context = null;
  if (savedContext?.purpose === "post" && ["task", "requirement"].includes(savedContext.category)) {
    context = { purpose: "post", category: savedContext.category };
  } else if (savedContext?.purpose === "service" && savedContext.category === "service") {
    const isBuyer = String(task.createdBy?.userId || "") === String(currentUserId);
    context = { purpose: isBuyer ? "buy" : "sell", category: "service" };
  }

  return {
    id: chat._id.toString(),
    last_message: chat.lastMessage || "",
    context,
    other_user: otherUser
      ? {
          id: otherUser._id.toString(),
          name: otherUser.name,
          avatar_url: otherUser.avatarUrl || otherUser.avatar_url || "",
          avatarUrl: otherUser.avatarUrl || otherUser.avatar_url || "",
          uid: otherUser.uid,
          isOnline: false,
        }
      : null,
    task: task
      ? {
          id: task._id?.toString?.() || "",
          title: task.title || "Task chat",
          deadline: task.urgency === "urgent" ? "Urgent task" : "Accepted task",
          status: task.status === "accepted" ? "assigned" : task.status,
          price: task.budget || 0,
        }
      : null,
  };
}

function formatMessage(message, senderName = "") {
  return {
    id: message._id.toString(),
    chatId: message.chatId?.toString?.() || "",
    taskId: message.taskId?.toString?.() || "",
    text: message.text || "",
    messageType: message.messageType || "text",
    imageUrl: message.imageUrl || "",
    attachmentUrl: message.attachmentUrl || "",
    fileName: message.fileName || "",
    mimeType: message.mimeType || "",
    senderId: message.senderId?.toString?.() || "",
    senderName,
    createdAt: message.createdAt,
    status: message.status || "sent",
  };
}

router.get("/rooms", verifyToken, async (req, res) => {
  const rooms = await Chat.find({ participants: req.user._id })
    .populate("taskId")
    .sort({ lastMessageAt: -1, updatedAt: -1 });

  const participantIds = new Set();
  rooms.forEach((room) => {
    (room.participants || []).forEach((participantId) => {
      if (String(participantId) !== String(req.user._id)) {
        participantIds.add(String(participantId));
      }
    });
  });

  const users = await User.find({ _id: { $in: Array.from(participantIds) } }).select("name uid avatarUrl avatar_url");
  const usersById = new Map(users.map((user) => [user._id.toString(), user]));

  const formattedRooms = await Promise.all(rooms.map(async (room) => {
    const formatted = formatRoom(room, req.user._id, usersById);
    const [messageCount, sentMessageCount] = await Promise.all([
      Message.countDocuments({ chatId: room._id }),
      Message.countDocuments({ chatId: room._id, senderId: req.user._id }),
    ]);
    return { ...formatted, message_count: messageCount, sent_message_count: sentMessageCount };
  }));
  return res.json({ rooms: formattedRooms });
});

router.get("/users", verifyToken, async (_req, res) => {
  return res.json({ users: [] });
});

router.get("/messages/:chatId", verifyToken, async (req, res) => {
  const chat = await Chat.findById(req.params.chatId);
  if (!chat || !(chat.participants || []).some((participantId) => String(participantId) === String(req.user._id))) {
    return res.status(404).json({ message: "Chat not found" });
  }

  const messages = await Message.find({ chatId: chat._id }).sort({ createdAt: 1 });
  const senderIds = Array.from(new Set(messages.map((item) => item.senderId?.toString?.()).filter(Boolean)));
  const users = await User.find({ _id: { $in: senderIds } });
  const usersById = new Map(users.map((user) => [user._id.toString(), user]));

  return res.json({
    messages: messages.map((message) => formatMessage(message, usersById.get(message.senderId.toString())?.name || "")),
  });
});

router.post("/send", verifyToken, async (req, res) => {
  const requestedTaskId = String(req.body.task_id || req.body.taskId || "").trim();
  const text = String(req.body.text || "").trim();

  if (!requestedTaskId || !text) {
    return res.status(400).json({ message: "Task and text are required" });
  }

  const task = await Task.findById(requestedTaskId);
  if (!task) {
    return res.status(404).json({ message: "Task not found" });
  }

  const participantIds = [task.createdBy?.userId?.toString?.(), task.acceptedBy?.userId?.toString?.()].filter(Boolean);
  if (!participantIds.includes(String(req.user._id))) {
    return res.status(403).json({ message: "You are not allowed to chat on this task" });
  }

  if (!task.acceptedBy?.userId) {
    return res.status(400).json({ message: "Chat becomes available after task acceptance" });
  }

  const receiverId =
    String(task.createdBy.userId) === String(req.user._id) ? task.acceptedBy.userId : task.createdBy.userId;

  const chat = await Chat.findOneAndUpdate(
    { taskId: task._id },
    {
      $setOnInsert: {
        taskId: task._id,
        participants: [task.createdBy.userId, task.acceptedBy.userId],
      },
      $set: {
        lastMessage: text,
        lastMessageAt: new Date(),
      },
    },
    { upsert: true, new: true }
  );

  const message = await Message.create({
    chatId: chat._id,
    taskId: task._id,
    senderId: req.user._id,
    receiverId,
    text,
    messageType: "text",
    status: "sent",
  });

  return res.status(201).json({
    message: "Message sent",
    chat_id: chat._id.toString(),
    sent_message: formatMessage(message, req.user.name),
  });
});

async function createAttachmentMessage(req, res, file, messageType) {
  const taskId = String(req.body.task_id || req.body.taskId || "").trim();
  if (!taskId || !file) return res.status(400).json({ message: messageType === "image" ? "Image is required" : "Attachment is required" });

  const task = await Task.findById(taskId);
  if (!task) return res.status(404).json({ message: "Task not found" });
  const ownerId = String(task.createdBy?.userId || "");
  const workerId = String(task.acceptedBy?.userId || "");
  const currentUserId = String(req.user._id);
  if (!workerId || ![ownerId, workerId].includes(currentUserId)) {
    return res.status(403).json({ message: "Chat is available only to task participants after acceptance" });
  }

  if (messageType === "image" && !String(file.mimetype || "").toLowerCase().startsWith("image/")) {
    return res.status(400).json({ message: "Choose a valid image file" });
  }
  const receiverId = ownerId === currentUserId ? workerId : ownerId;
  const chat = await Chat.findOneAndUpdate(
    { taskId: task._id },
    { $setOnInsert: { taskId: task._id, participants: [task.createdBy.userId, task.acceptedBy.userId] } },
    { upsert: true, new: true }
  );
  const stored = await uploadAsset(file, "notessphere/chat", messageType === "image" ? "image" : "raw");
  const message = await Message.create({
    chatId: chat._id,
    taskId: task._id,
    senderId: req.user._id,
    receiverId,
    text: "",
    messageType,
    imageUrl: messageType === "image" ? stored.url : "",
    attachmentUrl: messageType === "file" ? stored.url : "",
    fileName: file.originalname || "Attachment",
    mimeType: file.mimetype || "application/octet-stream",
    status: "sent",
  });
  chat.lastMessage = messageType === "image" ? "Photo" : (file.originalname || "Attachment");
  chat.lastMessageAt = message.createdAt || new Date();
  await chat.save();
  return res.status(201).json({
    message: "Message sent",
    chat_id: chat._id.toString(),
    sent_message: formatMessage(message, req.user.name),
  });
}

router.post("/send-image", verifyToken, upload.single("image"), async (req, res) => createAttachmentMessage(req, res, req.file, "image"));
router.post("/send-attachment", verifyToken, upload.single("attachment"), async (req, res) => createAttachmentMessage(req, res, req.file, "file"));

module.exports = router;
