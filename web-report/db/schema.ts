import { integer, sqliteTable, text, primaryKey } from "drizzle-orm/sqlite-core";

// Visitor identity is supplied by Sites; no positions enter the public repository.
export const reportPreferences = sqliteTable("report_preferences", {
  userId: text("user_id").primaryKey(),
  revision: integer("revision").notNull().default(0),
  payload: text("payload").notNull(),
  updatedAt: text("updated_at").notNull(),
});

// Independent cache records prevent study refreshes from racing list edits.
export const reportStudies = sqliteTable("report_studies", {
  userId: text("user_id").notNull(),
  code: text("code").notNull(),
  kind: text("kind").notNull(),
  session: text("session").notNull(),
  payload: text("payload").notNull(),
  updatedAt: text("updated_at").notNull(),
  leaseUntil: text("lease_until"),
  lockToken: text("lock_token"),
}, table => [primaryKey({ columns: [table.userId, table.code, table.kind] })]);
