const express = require("express");
const Note = require("../models/Note");
const Gig = require("../models/Gig");
const Task = require("../models/Task");
const { verifyToken } = require("../middleware/auth");
const { formatNote, formatGig } = require("../utils/formatters");

const router = express.Router();
const PAGE_LIMIT = 50;
const MAX_PAGE = 100;

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeListing(kind, item, viewerId) {
  if (kind === "note") {
    const note = formatNote(item, viewerId);
    return {
      id: note.id,
      kind,
      title: note.title,
      description: note.description,
      subject: note.subject,
      topic: note.topic,
      tags: note.tags,
      price: Number(note.price || 0),
      thumbnailUrl: note.thumbnailUrl,
      sellerName: note.sellerName,
      createdAt: note.createdAt,
    };
  }
  if (kind === "service") {
    return { ...formatGig(item), kind, price: Number(item.price || 0) };
  }
  return {
    id: item._id.toString(),
    kind,
    title: item.title,
    description: item.description,
    price: Number(item.budget || 0),
    budget: Number(item.budget || 0),
    location: item.location,
    urgency: item.urgency,
    status: item.status,
    sellerName: item.createdBy?.name || "",
    createdAt: item.createdAt,
  };
}

router.get("/", verifyToken, async (req, res) => {
  const page = Math.max(1, Number.parseInt(req.query.page, 10) || 1);
  const limit = Math.min(PAGE_LIMIT, Math.max(1, Number.parseInt(req.query.limit, 10) || 20));
  if (page > MAX_PAGE) {
    return res.status(400).json({ message: `Marketplace page cannot exceed ${MAX_PAGE}` });
  }
  const category = String(req.query.category || "all").trim().toLowerCase();
  const query = String(req.query.q || "").trim();
  const pattern = query ? new RegExp(escapeRegex(query), "i") : null;
  const search = pattern
    ? { $or: [{ title: pattern }, { description: pattern }] }
    : {};

  const specs = {
    notes: {
      kind: "note",
      model: Note,
      filter: {
        status: "approved",
        ...(pattern ? { $or: [{ title: pattern }, { description: pattern }, { subject: pattern }, { topic: pattern }] } : {}),
      },
      sort: { createdAt: -1 },
    },
    services: {
      kind: "service",
      model: Gig,
      filter: { $and: [{ $or: [{ status: "approved" }, { status: { $exists: false } }] }, search] },
      sort: { featured: -1, createdAt: -1 },
    },
    tasks: {
      kind: "task",
      model: Task,
      filter: { status: "open", "createdBy.userId": { $ne: req.user._id }, ...search },
      sort: { boosted: -1, trendingScore: -1, createdAt: -1 },
    },
  };
  const requestedTypes = category === "all" ? Object.keys(specs) : [category];
  if (requestedTypes.some((type) => !Object.hasOwn(specs, type)) && !["projects", "ppts"].includes(category)) {
    return res.status(400).json({ message: "Unsupported marketplace category" });
  }

  const lookAhead = page * limit;
  const [groups, totals] = await Promise.all([
    Promise.all(requestedTypes.filter((type) => Object.hasOwn(specs, type)).map(async (type) => {
      const spec = specs[type];
      const items = await spec.model.find(spec.filter).sort(spec.sort).limit(lookAhead);
      return items.map((item) => normalizeListing(spec.kind, item, req.user._id));
    })),
    Promise.all(requestedTypes.filter((type) => Object.hasOwn(specs, type)).map((type) => {
      const spec = specs[type];
      return spec.model.countDocuments(spec.filter);
    })),
  ]);
  const items = groups.flat().sort((a, b) => new Date(b.createdAt || 0).getTime() - new Date(a.createdAt || 0).getTime());
  const offset = (page - 1) * limit;
  const result = items.slice(offset, offset + limit);
  const total = totals.reduce((sum, count) => sum + count, 0);

  return res.json({
    items: result,
    total,
    page,
    limit,
    pages: Math.min(MAX_PAGE, Math.ceil(total / limit)),
    has_more: offset + result.length < total && page < MAX_PAGE,
  });
});

module.exports = router;
