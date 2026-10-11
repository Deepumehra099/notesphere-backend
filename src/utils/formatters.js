function formatUser(user) {
  return {
    id: user._id.toString(),
    uid: user.uid,
    name: user.name,
    email: user.email,
    role: user.role,
    tokens: user.tokenBalance || 0,
    credits_balance: user.tokenBalance || 0,
    referralCode: user.referralCode || "",
    is_admin: user.role === "admin",
    isVerified: Boolean(user.isVerified),
    is_verified: Boolean(user.isVerified),
    rating: user.rating,
    tasksCompleted: user.stats?.tasksCompleted || 0,
    wallet: user.wallet || { balance: 0, earnings: 0, pendingAmount: 0, totalWithdrawn: 0 },
    wallet_balance: user.wallet?.balance || 0,
    walletBalance: user.wallet?.balance || user.walletBalance || 0,
    wallet_available: user.wallet?.balance || 0,
    wallet_earnings: user.wallet?.earnings || 0,
    wallet_pending: user.wallet?.pendingAmount || 0,
    pendingBalance: user.wallet?.pendingAmount || user.pendingBalance || 0,
    wallet_withdrawn: user.wallet?.totalWithdrawn || 0,
    stats: user.stats || {},
    avatarUrl: user.avatarUrl || "",
    bio: user.bio || "",
    phone: user.phone || "",
    location: user.location || "",
    language: user.language || "en",
    skills: user.skills || [],
    upiId: user.upiId || "",
    payoutAccounts: user.payoutAccounts || [],
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
    unlock_cost: note.unlockCost || note.price || 0,
    thumbnailUrl: note.thumbnailUrl,
    pdfUrl: hasAccess ? note.pdfUrl : "",
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
    price: task.budget,
    location: task.location,
    mode: task.mode,
    urgency: task.urgency,
    boosted: task.boosted,
    trendingScore: task.trendingScore,
    status: task.status,
    sellerName: task.createdBy?.name || "",
    created_by: task.createdBy?.userId?.toString?.() || "",
    created_by_name: task.createdBy?.name || "",
    created_by_verified: Boolean(task.createdBy?.isVerified),
    created_by_elite_buyer: Boolean(task.createdBy?.eliteBuyer),
    created_by_elite_seller: Boolean(task.createdBy?.eliteSeller),
    acceptedBy: task.acceptedBy || null,
    submission: task.submission || null,
    escrow: task.escrow || { amount: 0 },
    escrow_status: task.escrow?.settlementStatus || "none",
    created_by: task.createdBy?.userId?.toString?.() || "",
    assigned_to: task.acceptedBy?.userId?.toString?.() || "",
    dispute: task.dispute || null,
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
    status: gig.status || "approved",
    rejectionReason: gig.rejectionReason || "",
    sellerName: gig.seller?.name || "",
    sellerId: gig.seller?.userId?.toString?.() || "",
    createdAt: gig.createdAt,
  };
}

module.exports = { formatUser, formatNote, formatTask, formatGig };
