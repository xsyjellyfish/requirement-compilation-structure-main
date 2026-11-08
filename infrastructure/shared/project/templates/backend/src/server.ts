import { createApp } from "./app.js";
import { createDatabaseClient } from "./db/client.js";
import { initializeDatabase } from "./db/initialize.js";
import { requireSeedData } from "./db/seed.js";

const port = Number(process.env.PORT ?? "3000");
if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error("Invalid PORT");
const connection = createDatabaseClient();
initializeDatabase(connection);
try {
  // The requirement declares prerequisite data; a deployment without it would serve an
  // application whose own scenarios cannot run, so startup fails instead of continuing.
  requireSeedData(connection);
} catch (error) {
  connection.close();
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
}
const server = createApp(connection).listen(port, (error?: Error) => {
  if (error) {
    connection.close();
    console.error(error);
    process.exitCode = 1;
    return;
  }
  const address = server.address();
  console.log(`Application listening at http://localhost:${typeof address === "object" && address ? address.port : port}`);
});
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => server.close(() => { connection.close(); process.exit(0); }));
}
