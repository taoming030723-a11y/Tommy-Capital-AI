CREATE TABLE `report_studies` (
	`user_id` text NOT NULL,
	`code` text NOT NULL,
	`kind` text NOT NULL,
	`session` text NOT NULL,
	`payload` text NOT NULL,
	`updated_at` text NOT NULL,
	`lease_until` text,
	`lock_token` text,
	PRIMARY KEY(`user_id`, `code`, `kind`)
);
