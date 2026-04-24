const express = require("express");
const Note = require("../models/Note");
const User = require("../models/User");
const { verifyToken } = require("../middleware/auth");
const { upload } = require("../middleware/upload");
const { formatNote } = require("../utils/formatters");
const { uploadAsset } = require("../services/storageService");
const { notifyUser } = require("../services/notificationService");
const { purchaseNote } = require("../services/walletService");

const router = express.Router();

router.get("/", verifyToken, async (req, res) => {
  const access = String(req.query.access || "all");
  const notes = await Note.find({ status: "approved" }).sort({ createdAt: -1 });

  const filtered = notes.filter((note) => {
    if (access === "free") return note.price === 0;
    if (access === "paid") return note.price > 0;
    return true;
  });

  return res.json({ notes: filtered.map((note) => formatNote(note, req.user._id)) });
});

router.get("/feed", verifyToken, async (req, res) => {
  const notes = await Note.find({ status: "approved" }).sort({ createdAt: -1 });
  return res.json({ notes: notes.map((note) => formatNote(note, req.user._id)) });
});

router.get("/mine", verifyToken, async (req, res) => {
  const notes = await Note.find({ "seller.userId": req.user._id }).sort({ createdAt: -1 });
  return res.json({ notes: notes.map((note) => formatNote(note, req.user._id)) });
});

router.get("/my", verifyToken, async (req, res) => {
  const notes = await Note.find({ "seller.userId": req.user._id }).sort({ createdAt: -1 });
  return res.json({ notes: notes.map((note) => formatNote(note, req.user._id)) });
});

router.get("/downloads", verifyToken, async (req, res) => {
  const notes = await Note.find({
    status: "approved",
    $or: [
      { price: 0 },
      { "seller.userId": req.user._id },
      { buyers: { $elemMatch: { userId: req.user._id } } },
    ],
  }).sort({ createdAt: -1 });

  return res.json({ notes: notes.map((note) => formatNote(note, req.user._id)) });
});

router.get("/:id", verifyToken, async (req, res) => {
  const note = await Note.findById(req.params.id);

  if (!note) {
    return res.status(404).json({ message: "Note not found" });
  }

  const isOwner = String(note.seller.userId) === String(req.user._id);
  if (note.status !== "approved" && !isOwner && req.user.role !== "admin") {
    return res.status(404).json({ message: "Note not found" });
  }

  return res.json({ note: formatNote(note, req.user._id) });
});

router.post(
  "/upload",
  verifyToken,
  upload.fields([
    { name: "thumbnail", maxCount: 1 },
    { name: "pdf", maxCount: 1 },
  ]),
  async (req, res) => {
    const { subject, topic, title, description, tags, price } = req.body;

    if (!subject || !topic || !title) {
      return res.status(400).json({ message: "Subject, topic, and title are required" });
    }

    const thumbnail = req.files?.thumbnail?.[0];
    const pdf = req.files?.pdf?.[0];

    if (!thumbnail || !pdf) {
      return res.status(400).json({ message: "Thumbnail image and PDF are required" });
    }

    const [thumbnailUpload, pdfUpload] = await Promise.all([
      uploadAsset(thumbnail, "noteverse/notes/thumbnails", "image"),
      uploadAsset(pdf, "noteverse/notes/pdfs", "raw"),
    ]);

    const note = await Note.create({
      title: String(title).trim(),
      subject: String(subject).trim(),
      topic: String(topic).trim(),
      description: String(description || "").trim(),
      tags: String(tags || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      price: Number(price || 0),
      thumbnailUrl: thumbnailUpload.url,
      thumbnailPublicId: thumbnailUpload.publicId,
      pdfUrl: pdfUpload.url,
      pdfPublicId: pdfUpload.publicId,
      seller: {
        userId: req.user._id,
        name: req.user.name,
        email: req.user.email,
      },
    });

    await User.findByIdAndUpdate(req.user._id, { $inc: { "stats.notesCount": 1 } });
    await notifyUser(req.user._id, "Note submitted", `${note.title} is waiting for admin review.`);

    return res.status(201).json({
      message: "Note uploaded and awaiting admin approval",
      note: formatNote(note, req.user._id),
    });
  }
);

router.post("/buy", verifyToken, async (req, res) => {
  const noteId = String(req.body.note_id || "").trim();
  if (!noteId) {
    return res.status(400).json({ message: "note_id is required" });
  }

  const result = await purchaseNote({ buyerId: req.user._id, noteId });
  return res.json({
    message: result.alreadyBought ? "Note already in your library" : "Note purchased successfully",
    note: formatNote(result.note, req.user._id),
  });
});

router.post("/:id/purchase", verifyToken, async (req, res) => {
  const result = await purchaseNote({ buyerId: req.user._id, noteId: req.params.id });
  return res.json({
    message: result.alreadyBought ? "Note already in your library" : "Note purchased successfully",
    note: formatNote(result.note, req.user._id),
  });
});

router.post("/:id/unlock", verifyToken, async (req, res) => {
  const result = await purchaseNote({ buyerId: req.user._id, noteId: req.params.id });
  return res.json({
    message: result.alreadyBought ? "Note already in your library" : "Note purchased successfully",
    note: formatNote(result.note, req.user._id),
  });
});

router.get("/:id/download", verifyToken, async (req, res) => {
  const note = await Note.findById(req.params.id);

  if (!note || note.status !== "approved") {
    return res.status(404).json({ message: "Note not found" });
  }

  const hasAccess =
    note.price <= 0 ||
    String(note.seller.userId) === String(req.user._id) ||
    note.buyers.some((buyer) => String(buyer.userId) === String(req.user._id)) ||
    req.user.role === "admin";

  if (!hasAccess) {
    return res.status(403).json({ message: "You need to purchase this note first" });
  }

  note.downloads += 1;
  await note.save();

  return res.json({
    file_url: note.pdfUrl,
    note: formatNote(note, req.user._id),
  });
});

module.exports = router;
