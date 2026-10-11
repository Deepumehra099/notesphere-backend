const { app, bootstrap } = require("./app");
const env = require("./config/env");

bootstrap()
  .then(() => {
    app.listen(env.port, () => {
      console.log(`NoteSphere API running on port ${env.port}`);
    });
  })
  .catch((error) => {
    console.error("Failed to start NoteSphere API", error);
    process.exit(1);
  });
