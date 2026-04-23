// dom4in-backend Worker
//
// Public endpoints:
//   GET  /api/health
//   GET  /api/stats/overview         — aggregates for the site, includes last_run_at / last_run_status
//   GET  /api/stats/words            — word-POS aggregates only
//
// Admin endpoints (require x-admin-api-key matching env.ADMIN_API_KEY):
//   POST /api/admin/upload-aggregate — push cumulative aggregates; idempotent if run_id+batch_id provided
//   POST /api/admin/reset-stats      — DANGER: wipe all stats
//   GET  /api/admin/state?key=...    — read opaque collector state value
//   PUT  /api/admin/state            — write { key, value } (value JSON-stringified by caller)
//   POST /api/admin/runs             — run lifecycle events: { run_id, event, ... }

const JSON_HEADERS = { "Content-Type": "application/json" };

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function unauthorized() {
  return json({ error: "unauthorized" }, 401);
}

function requireAdmin(request, env) {
  const apiKey =
    request.headers.get("x-admin-api-key") ||
    request.headers.get("X-Admin-Api-Key");
  if (!apiKey || apiKey !== env.ADMIN_API_KEY) return false;
  return true;
}

async function readJson(request) {
  try {
    return { ok: true, body: await request.json() };
  } catch (err) {
    return { ok: false, error: err };
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/api/health") {
      return json({ status: "ok" });
    }

    if (path === "/api/stats/overview") {
      return handleOverview(env);
    }

    if (path === "/api/stats/words") {
      return handleWordsOnly(env);
    }

    if (path === "/api/admin/upload-aggregate" && request.method === "POST") {
      if (!requireAdmin(request, env)) return unauthorized();
      return handleUploadAggregate(request, env);
    }

    if (path === "/api/admin/reset-stats" && request.method === "POST") {
      if (!requireAdmin(request, env)) return unauthorized();
      return handleResetStats(env);
    }

    if (path === "/api/admin/state") {
      if (!requireAdmin(request, env)) return unauthorized();
      if (request.method === "GET") return handleGetState(url, env);
      if (request.method === "PUT") return handlePutState(request, env);
      return json({ error: "method_not_allowed" }, 405);
    }

    if (path === "/api/admin/runs" && request.method === "POST") {
      if (!requireAdmin(request, env)) return unauthorized();
      return handleRunsEvent(request, env);
    }

    return new Response("Not found", { status: 404 });
  },
};

// -------- handlers --------

async function handleOverview(env) {
  try {
    const lifetimeRow = await env.DB.prepare(
      "SELECT COALESCE(SUM(domains_tracked_lifetime), 0) AS lifetime_total FROM global_stats"
    ).first();

    const latestGlobal = await env.DB.prepare(
      "SELECT date, domains_tracked_24h, updated_at FROM global_stats ORDER BY date DESC LIMIT 1"
    ).first();

    // Pull last finished (or running) run so the site can show freshness
    // without reading any stats tables. Best-effort: if the table doesn't
    // exist yet we fall back to stats updated_at.
    let lastRun = null;
    try {
      lastRun = await env.DB.prepare(
        "SELECT run_id, started_at, finished_at, status FROM runs ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1"
      ).first();
    } catch (_) {
      lastRun = null;
    }

    const basePayload = {
      domains_tracked_lifetime: 0,
      domains_tracked_24h: 0,
      letter_length_counts: [],
      tld_length_counts: [],
      word_pos_stats: [],
      last_updated_at: null,
      last_run_at: null,
      last_run_status: null,
    };

    if (!latestGlobal) {
      if (lastRun) {
        basePayload.last_run_at = lastRun.finished_at || lastRun.started_at;
        basePayload.last_run_status = lastRun.status;
      }
      return json(basePayload);
    }

    const { date, domains_tracked_24h, updated_at } = latestGlobal;
    const domains_tracked_lifetime = lifetimeRow?.lifetime_total ?? 0;

    const lengthRows = await env.DB.prepare(
      "SELECT length, total_possible, tracked_count, unregistered_found, unused_found FROM length_stats WHERE snap_date = ? AND tld = ? ORDER BY length ASC"
    )
      .bind("overall", "ALL")
      .all();

    const letter_length_counts = (lengthRows.results || []).map((row) => ({
      length: row.length,
      total_possible: row.total_possible,
      tracked: row.tracked_count,
      unregistered_found: row.unregistered_found,
      unused_found: row.unused_found,
    }));

    const tldLengthRows = await env.DB.prepare(
      "SELECT tld, length, total_possible, tracked_count, unregistered_found, unused_found FROM length_stats WHERE snap_date = ? AND tld != ? ORDER BY tld, length ASC"
    )
      .bind("overall", "ALL")
      .all();

    const tld_length_counts = (tldLengthRows.results || []).map((row) => ({
      tld: row.tld,
      length: row.length,
      total_possible: row.total_possible,
      tracked: row.tracked_count,
      unregistered_found: row.unregistered_found,
      unused_found: row.unused_found,
    }));

    const wordRows = await env.DB.prepare(
      "SELECT pos, length, tracked_count, unregistered_found, unused_found FROM word_pos_stats WHERE snap_date = ? AND tld = ? ORDER BY pos, length"
    )
      .bind("overall", "ALL")
      .all();

    const word_pos_stats = (wordRows.results || []).map((row) => ({
      pos: row.pos,
      length: row.length,
      tracked: row.tracked_count,
      unregistered_found: row.unregistered_found,
      unused_found: row.unused_found,
    }));

    const payload = {
      domains_tracked_lifetime,
      domains_tracked_24h,
      last_updated_at: updated_at,
      last_run_at: lastRun ? (lastRun.finished_at || lastRun.started_at) : updated_at,
      last_run_status: lastRun ? lastRun.status : null,
      letter_length_counts,
      tld_length_counts,
      word_pos_stats,
    };

    return json(payload);
  } catch (err) {
    return json({ error: "failed_to_load_overview", message: String(err) }, 500);
  }
}

async function handleWordsOnly(env) {
  try {
    const rows = await env.DB.prepare(
      "SELECT pos, length, tracked_count, unregistered_found, unused_found FROM word_pos_stats WHERE snap_date = ? AND tld = ? ORDER BY pos, length"
    )
      .bind("overall", "ALL")
      .all();

    const word_pos_stats = (rows.results || []).map((row) => ({
      pos: row.pos,
      length: row.length,
      tracked: row.tracked_count,
      unregistered_found: row.unregistered_found,
      unused_found: row.unused_found,
    }));

    return json({ word_pos_stats });
  } catch (err) {
    return json({ error: "failed_to_load_word_stats", message: String(err) }, 500);
  }
}

async function handleUploadAggregate(request, env) {
  const parsed = await readJson(request);
  if (!parsed.ok) return json({ error: "invalid_json" }, 400);
  const body = parsed.body || {};

  const { date, global, length_stats, length_stats_by_tld, word_pos_stats, run_id, batch_id } = body;
  if (!date || !global || !Array.isArray(length_stats)) {
    return json({ error: "invalid_payload" }, 400);
  }

  // Optional idempotency. If both run_id and batch_id are provided, record
  // them; a second POST with the same pair is rejected with 200 ok=false.
  // If omitted (e.g. existing local runs), we fall through as before.
  if (run_id && batch_id) {
    try {
      const existing = await env.DB.prepare(
        "SELECT 1 AS hit FROM upload_dedupe WHERE run_id = ? AND batch_id = ?"
      ).bind(run_id, batch_id).first();
      if (existing) {
        return json({ ok: true, duplicate: true });
      }
      await env.DB.prepare(
        "INSERT INTO upload_dedupe (run_id, batch_id) VALUES (?, ?)"
      ).bind(run_id, batch_id).run();
    } catch (err) {
      // If the dedupe table isn't there yet (migration not applied), do NOT
      // silently allow possible double-counting — reject so the operator
      // notices.
      return json({ error: "dedupe_unavailable", message: String(err) }, 500);
    }
  }

  try {
    await env.DB.prepare(
      "INSERT INTO global_stats (date, domains_tracked_lifetime, domains_tracked_24h) VALUES (?, ?, ?) " +
        "ON CONFLICT(date) DO UPDATE SET " +
        "domains_tracked_lifetime = global_stats.domains_tracked_lifetime + excluded.domains_tracked_lifetime, " +
        "domains_tracked_24h = global_stats.domains_tracked_24h + excluded.domains_tracked_24h, " +
        "updated_at = datetime('now')"
    )
      .bind(date, global.domains_tracked_lifetime || 0, global.domains_tracked_24h || 0)
      .run();

    for (const row of length_stats) {
      await env.DB.prepare(
        "INSERT INTO length_stats (snap_date, tld, length, total_possible, tracked_count, unregistered_found, unused_found) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?) " +
          "ON CONFLICT(snap_date, tld, length) DO UPDATE SET " +
          "total_possible = excluded.total_possible, " +
          "tracked_count = length_stats.tracked_count + excluded.tracked_count, " +
          "unregistered_found = length_stats.unregistered_found + excluded.unregistered_found, " +
          "unused_found = length_stats.unused_found + excluded.unused_found, " +
          "updated_at = datetime('now')"
      )
        .bind(
          "overall",
          row.tld || "ALL",
          row.length,
          row.total_possible,
          row.tracked_count || 0,
          row.unregistered_found || 0,
          row.unused_found || 0
        )
        .run();
    }

    if (Array.isArray(length_stats_by_tld)) {
      for (const row of length_stats_by_tld) {
        await env.DB.prepare(
          "INSERT INTO length_stats (snap_date, tld, length, total_possible, tracked_count, unregistered_found, unused_found) " +
            "VALUES (?, ?, ?, ?, ?, ?, ?) " +
            "ON CONFLICT(snap_date, tld, length) DO UPDATE SET " +
            "total_possible = excluded.total_possible, " +
            "tracked_count = length_stats.tracked_count + excluded.tracked_count, " +
            "unregistered_found = length_stats.unregistered_found + excluded.unregistered_found, " +
            "unused_found = length_stats.unused_found + excluded.unused_found, " +
            "updated_at = datetime('now')"
        )
          .bind(
            "overall",
            row.tld,
            row.length,
            row.total_possible,
            row.tracked_count || 0,
            row.unregistered_found || 0,
            row.unused_found || 0
          )
          .run();
      }
    }

    if (Array.isArray(word_pos_stats)) {
      for (const row of word_pos_stats) {
        await env.DB.prepare(
          "INSERT INTO word_pos_stats (snap_date, tld, pos, length, tracked_count, unregistered_found, unused_found) " +
            "VALUES (?, ?, ?, ?, ?, ?, ?) " +
            "ON CONFLICT(snap_date, tld, pos, length) DO UPDATE SET " +
            "tracked_count = word_pos_stats.tracked_count + excluded.tracked_count, " +
            "unregistered_found = word_pos_stats.unregistered_found + excluded.unregistered_found, " +
            "unused_found = word_pos_stats.unused_found + excluded.unused_found, " +
            "updated_at = datetime('now')"
        )
          .bind(
            "overall",
            row.tld || "ALL",
            row.pos,
            row.length,
            row.tracked_count || 0,
            row.unregistered_found || 0,
            row.unused_found || 0
          )
          .run();
      }
    }

    return json({ ok: true });
  } catch (err) {
    return json({ error: "failed_to_store_aggregate", message: String(err) }, 500);
  }
}

async function handleResetStats(env) {
  try {
    await env.DB.prepare("DELETE FROM global_stats").run();
    await env.DB.prepare("DELETE FROM length_stats").run();
    await env.DB.prepare("DELETE FROM tld_stats").run();
    await env.DB.prepare("DELETE FROM word_pos_stats").run();
    // Best-effort: clear idempotency log, but do NOT wipe `runs` or `state`
    // so we still have an audit trail after a reset.
    try { await env.DB.prepare("DELETE FROM upload_dedupe").run(); } catch (_) {}
    return json({ ok: true });
  } catch (err) {
    return json({ error: "failed_to_reset_stats", message: String(err) }, 500);
  }
}

async function handleGetState(url, env) {
  const key = url.searchParams.get("key");
  if (!key) return json({ error: "missing_key" }, 400);
  try {
    const row = await env.DB.prepare(
      "SELECT key, value, updated_at FROM state WHERE key = ?"
    ).bind(key).first();
    if (!row) return json({ key, value: null });
    // Try to parse value back to JSON, but return raw string if it isn't valid.
    let parsed = null;
    try { parsed = JSON.parse(row.value); } catch (_) { parsed = null; }
    return json({
      key: row.key,
      value: parsed !== null ? parsed : row.value,
      raw: row.value,
      updated_at: row.updated_at,
    });
  } catch (err) {
    return json({ error: "failed_to_read_state", message: String(err) }, 500);
  }
}

async function handlePutState(request, env) {
  const parsed = await readJson(request);
  if (!parsed.ok) return json({ error: "invalid_json" }, 400);
  const { key, value } = parsed.body || {};
  if (!key || typeof key !== "string") return json({ error: "missing_key" }, 400);
  if (value === undefined) return json({ error: "missing_value" }, 400);

  // Serialize non-string values. Strings pass through so clients can store
  // pre-serialized blobs if they prefer.
  const stored = typeof value === "string" ? value : JSON.stringify(value);

  try {
    await env.DB.prepare(
      "INSERT INTO state (key, value, updated_at) VALUES (?, ?, datetime('now')) " +
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')"
    ).bind(key, stored).run();
    return json({ ok: true });
  } catch (err) {
    return json({ error: "failed_to_write_state", message: String(err) }, 500);
  }
}

async function handleRunsEvent(request, env) {
  const parsed = await readJson(request);
  if (!parsed.ok) return json({ error: "invalid_json" }, 400);
  const body = parsed.body || {};
  const { run_id, event } = body;

  if (!run_id || typeof run_id !== "string") {
    return json({ error: "missing_run_id" }, 400);
  }
  if (!event || !["start", "finish", "heartbeat"].includes(event)) {
    return json({ error: "invalid_event" }, 400);
  }

  try {
    if (event === "start") {
      const source = typeof body.source === "string" ? body.source : null;
      const notes = typeof body.notes === "string" ? body.notes : null;
      await env.DB.prepare(
        "INSERT INTO runs (run_id, started_at, status, source, notes) " +
        "VALUES (?, datetime('now'), 'running', ?, ?) " +
        "ON CONFLICT(run_id) DO NOTHING"
      ).bind(run_id, source, notes).run();
      return json({ ok: true });
    }

    if (event === "heartbeat") {
      const domains = Number.isFinite(body.domains_processed) ? body.domains_processed : 0;
      const errors = Number.isFinite(body.errors_count) ? body.errors_count : 0;
      await env.DB.prepare(
        "UPDATE runs SET domains_processed = ?, errors_count = ? WHERE run_id = ?"
      ).bind(domains, errors, run_id).run();
      return json({ ok: true });
    }

    // finish
    const status = ["success", "partial", "failed"].includes(body.status)
      ? body.status
      : "success";
    const domains = Number.isFinite(body.domains_processed) ? body.domains_processed : 0;
    const errors = Number.isFinite(body.errors_count) ? body.errors_count : 0;
    const notes = typeof body.notes === "string" ? body.notes : null;

    await env.DB.prepare(
      "UPDATE runs SET finished_at = datetime('now'), status = ?, domains_processed = ?, errors_count = ?, notes = COALESCE(?, notes) WHERE run_id = ?"
    ).bind(status, domains, errors, notes, run_id).run();

    return json({ ok: true });
  } catch (err) {
    return json({ error: "failed_to_record_run_event", message: String(err) }, 500);
  }
}
