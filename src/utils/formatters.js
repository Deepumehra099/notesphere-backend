function formatUser(user) {
  return {
    id: user._id.toString(),
    uid: user.uid,
    name: user.name,
    email: user.email,
    role: user.role,
    is_admin: user.role === "admin",
    rating: user.rating,
    tasksCompleted: user.stats?.tasksCompleted || 0,
    wallet: user.wallet || { balance: 0, earnings: 0, pendingAmount: 0, totalWithdrawn: 0 },
    wallet_balance: user.wallet?.balance || 0,
    wallet_available: user.wallet?.balance || 0,
    wallet_earnings: user.wallet?.earnings || 0,
    wallet_pending: user.wallet?.pendingAmount || 0,
    wallet_withdrawn: user.wallet?.totalWithdrawn || 0,
    stats: user.stats || {},
    avatarUrl: user.avatarUrl || "",
    bio: user.bio || "",
    phone: user.phone || "",
    location: user.location || "",
    language: user.language || "en",
    skills: user.skills || [],
    upiId: user.upiId || "",
    isBlocked: Boolean(user.isBlocked),
  };
}

function formatNote(note, viewerId = null) {
  const hasAccess =
    note.price <= 0 ||
    String(note.seller?.userId || "") === String(viewerId || "") ||
    note.buyers?.some?.((buyer) => String(buyer.userId) === String(viewerId || ""));

  return {
    id: note._id.toString(),
    title: note.title,
    subject: note.subject,
    topic: note.topic,
    description: note.description,
    tags: note.tags || [],
    price: note.price,
    thumbnailUrl: note.thumbnailUrl,
    pdfUrl: note.pdfUrl,
    status: note.status,
    rejectionReason: note.rejectionReason || "",
    downloads: note.downloads,
    buyersCount: note.buyers?.length || 0,
    sellerName: note.seller?.name || "",
    sellerId: note.seller?.userId?.toString?.() || "",
    isPurchased: hasAccess,
    canDownload: hasAccess,
    createdAt: note.createdAt,
  };
}

function formatTask(task) {
  return {
    id: task._id.toString(),
    title: task.title,
    description: task.description,
    budget: task.budget,
    location: task.location,
    mode: task.mode,
    urgency: task.urgency,
    boosted: task.boosted,
    trendingScore: task.trendingScore,
    status: task.status,
    sellerName: task.createdBy?.name || "",
    acceptedBy: task.acceptedBy || null,
    submission: task.submission || null,
    escrow: task.escrow || { amount: 0 },
    createdAt: task.createdAt,
  };
}

function formatGig(gig) {
  return {
    id: gig._id.toString(),
    title: gig.title,
    description: gig.description,
    category: gig.category,
    price: gig.price,
    featured: gig.featured,
    sellerName: gig.seller?.name || "",
    createdAt: gig.createdAt,
  };
}

module.exports = { formatUser, formatNote, formatTask, formatGig };
