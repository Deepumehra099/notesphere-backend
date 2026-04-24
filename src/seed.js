const bcrypt = require("bcrypt");
const env = require("./config/env");
const User = require("./models/User");
const Note = require("./models/Note");
const Task = require("./models/Task");
const Gig = require("./models/Gig");
const Notification = require("./models/Notification");

function randomUid() {
  return `NV${Math.floor(100000 + Math.random() * 900000)}`;
}

async function ensureAdmin() {
  const email = env.adminEmail.toLowerCase();
  let admin = await User.findOne({ email });

  if (!admin) {
    admin = await User.create({
      uid: randomUid(),
      name: env.adminName,
      email,
      passwordHash: await bcrypt.hash(env.adminPassword, 10),
      role: "admin",
      rating: 5,
      wallet: { balance: 5000, earnings: 5000 },
      stats: { notesCount: 12, tasksPosted: 18, tasksCompleted: 31, gigsCount: 4, downloads: 320 },
    });
  }

  return admin;
}

async function seedMarketplace(owner) {
  const noteCount = await Note.countDocuments();
  const taskCount = await Task.countDocuments();
  const gigCount = await Gig.countDocuments();

  if (noteCount === 0) {
    await Note.insertMany([
      {
        title: "Data Structures Interview Notes",
        subject: "Computer Science",
        topic: "DSA",
        description: "Sharp revision notes with diagrams and solved patterns.",
        tags: ["algorithms", "revision", "placements"],
        price: 0,
        thumbnailUrl: "https://images.unsplash.com/photo-1455390582262-044cdead277a?auto=format&fit=crop&w=800&q=80",
        pdfUrl: "/uploads/sample-dsa.pdf",
        status: "approved",
        seller: { userId: owner._id, name: owner.name, email: owner.email },
        downloads: 182,
      },
      {
        title: "Economics Semester Master Pack",
        subject: "Economics",
        topic: "Macro + Micro",
        description: "Premium chapterwise notes with summaries and model answers.",
        tags: ["semester", "economics"],
        price: 149,
        thumbnailUrl: "https://images.unsplash.com/photo-1513258496099-48168024aec0?auto=format&fit=crop&w=800&q=80",
        pdfUrl: "/uploads/sample-economics.pdf",
        status: "approved",
        seller: { userId: owner._id, name: owner.name, email: owner.email },
        downloads: 74,
      },
    ]);
  }

  if (taskCount === 0) {
    await Task.insertMany([
      {
        title: "Build 15-slide startup pitch deck",
        description: "Need a polished investor deck by tomorrow evening.",
        budget: 2200,
        location: "Remote",
        mode: "remote",
        urgency: "urgent",
        boosted: true,
        trendingScore: 98,
        createdBy: { userId: owner._id, name: owner.name, email: owner.email },
      },
      {
        title: "Local event photography for college fest",
        description: "4-hour campus event coverage with quick turnaround.",
        budget: 3500,
        location: "Bangalore",
        mode: "nearby",
        urgency: "normal",
        boosted: true,
        trendingScore: 92,
        createdBy: { userId: owner._id, name: owner.name, email: owner.email },
      },
      {
        title: "Convert handwritten class notes into neat PDF",
        description: "Looking for a detail-focused editor with Canva/Docs experience.",
        budget: 850,
        location: "Remote",
        mode: "remote",
        urgency: "normal",
        boosted: false,
        trendingScore: 76,
        createdBy: { userId: owner._id, name: owner.name, email: owner.email },
      },
    ]);
  }

  if (gigCount === 0) {
    await Gig.insertMany([
      {
        title: "Assignment Formatting and Cleanup",
        description: "I will polish docs, tables, citations, and export clean PDFs.",
        category: "Documents",
        price: 499,
        featured: true,
        seller: { userId: owner._id, name: owner.name, email: owner.email },
      },
      {
        title: "Resume + Portfolio Audit",
        description: "ATS-friendly resume tuning plus portfolio feedback.",
        category: "Career",
        price: 999,
        featured: false,
        seller: { userId: owner._id, name: owner.name, email: owner.email },
      },
    ]);
  }

  const notifications = await Notification.countDocuments({ userId: owner._id });
  if (notifications === 0) {
    await Notification.insertMany([
      { userId: owner._id, title: "New order", body: "A premium note was downloaded this morning." },
      { userId: owner._id, title: "Trending task", body: "Your boosted task is gaining traction." },
    ]);
  }
}

async function seedDatabase() {
  const admin = await ensureAdmin();
  await seedMarketplace(admin);
}

module.exports = { seedDatabase };
