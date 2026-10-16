import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

// Visitor identity is supplied by Sites; no positions enter the public repository.
export const reportPreferences = sqliteTable("report_preferences", {
  userId: text("user_id").primaryKey(),
  revision: integer("revision").notNull().default(0),
  payload: text("payload").notNull(),
  updatedAt: text("updated_at").notNull(),
});
