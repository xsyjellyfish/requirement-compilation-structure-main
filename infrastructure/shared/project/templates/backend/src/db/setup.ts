import { createDatabaseClient } from "./client.js";
import { initializeDatabase } from "./initialize.js";
import { requireSeedData } from "./seed.js";

const connection = createDatabaseClient();
try {
  initializeDatabase(connection);
  requireSeedData(connection);
  console.log("Database tables initialized and prerequisite data seeded.");
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
} finally {
  connection.close();
}
