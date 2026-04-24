const fs = require("fs");
const path = require("path");
const { v2: cloudinary } = require("cloudinary");
const env = require("../config/env");

const uploadsDir = path.resolve(__dirname, "../../uploads");

if (!fs.existsSync(uploadsDir)) {
  fs.mkdirSync(uploadsDir, { recursive: true });
}

if (env.cloudinaryCloudName && env.cloudinaryApiKey && env.cloudinaryApiSecret) {
  cloudinary.config({
    cloud_name: env.cloudinaryCloudName,
    api_key: env.cloudinaryApiKey,
    api_secret: env.cloudinaryApiSecret,
  });
}

function canUseCloudinary() {
  return Boolean(env.cloudinaryCloudName && env.cloudinaryApiKey && env.cloudinaryApiSecret);
}

async function uploadAsset(file, folder, resourceType = "auto") {
  if (!file) {
    return { url: "", publicId: "" };
  }

  if (canUseCloudinary()) {
    const result = await cloudinary.uploader.upload(file.path, {
      folder,
      resource_type: resourceType,
      use_filename: true,
      unique_filename: true,
    });

    return {
      url: result.secure_url,
      publicId: result.public_id,
    };
  }

  return {
    url: `/uploads/${path.basename(file.path)}`,
    publicId: "",
  };
}

module.exports = { uploadAsset, canUseCloudinary };
