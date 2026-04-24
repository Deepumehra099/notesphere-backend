const mongoose = require("mongoose");
const env = require("./env");

async function connectDb() {
  if (mongoose.connection.readyState === 1) {
    return mongoose.connection;
  }

  await mongoose.connect(env.mongoUrl, {
    dbName: env.dbName,
  });

  return mongoose.connection;
}

module.exports = { connectDb };
