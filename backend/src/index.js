// dom4in-backend — Cloudflare Worker
//
// Public:  GET /api/health, /api/stats/overview, /api/stats/words
// Admin:   POST /api/admin/upload-aggregate, reset-stats, runs
//          GET|PUT /api/admin/state
// Admin endpoints require x-admin-api-key matching env.ADMIN_API_KEY.

const JSON_HEADERS = { "Content-Type": "application/json" };

// Cache directives for public read endpoints. The data updates a few times a
// day (collector cadence), so 5 minutes at the edge is conservative; SWR keeps
// users hot during the brief revalidation window.
const PUBLIC_CACHE_HEADERS = {
  "Cache-Control": "public, max-age=60, s-maxage=300, stale-while-revalidate=600",
};
const NO_STORE_HEADERS = { "Cache-Control": "no-store" };

function json(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...extraHeaders },
  });
}

// Worker-bound rate limiter. Falls open if the binding is missing (local dev,
// preview deploys without the binding configured). Keyed on the client IP so
// one rogue scraper can't starve everyone else.
async function rateLimitOk(request, env) {
  if (!env.RATE_LIMITER) return true;
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  try {
    const result = await env.RATE_LIMITER.limit({ key: ip });
    return Boolean(result && result.success);
  } catch (_) {
    // Don't take the API down because rate-limit infra hiccupped.
    return true;
  }
}

function rateLimitedResponse() {
  return json(
    { error: "rate_limited", message: "Too many requests; try again in a minute." },
    429,
    { "Retry-After": "60", "Cache-Control": "no-store" }
  );
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

    // /api/health is intentionally not rate-limited and not cached: the
    // watchdog and any external uptime monitor must always get fresh state.
    if (path === "/api/health") {
      return handleHealth(env);
    }

    // Public stats endpoints — rate-limited per IP; success responses get
    // edge cache headers so most repeat traffic never hits the Worker again.
    if (path === "/api/stats/overview") {
      if (!(await rateLimitOk(request, env))) return rateLimitedResponse();
      return handleOverview(env);
    }

    if (path === "/api/stats/words") {
      if (!(await rateLimitOk(request, env))) return rateLimitedResponse();
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

// Cross-project health contract.
//   GET /api/health
//     200 { ok: true,  status: "ok",       checks: [...] }   when all checks pass
//     503 { ok: false, status: "degraded", checks: [...] }   when any check fails
// Each check has { name, ok, message, age_hours? }. The watchdog workflow only
// reads `ok` at the top level — checks[] is for humans diagnosing the issue.
const STALE_THRESHOLD_HOURS = 30; // covers 8h cron cadence + one missed run + drift

// SQLite datetime('now') returns 'YYYY-MM-DD HH:MM:SS' UTC with no zone marker.
// JS Date() will guess local time without one, so normalize before parsing.
function parseSqliteUtc(ts) {
  if (!ts) return null;
  const iso = String(ts).includes("T") ? ts : String(ts).replace(" ", "T");
  const withZone = /[Zz]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z";
  const d = new Date(withZone);
  return Number.isNaN(d.getTime()) ? null : d;
}

function ageHoursFrom(ts) {
  const d = parseSqliteUtc(ts);
  if (!d) return null;
  return Math.round((Date.now() - d.getTime()) / 3600000);
}

async function handleHealth(env) {
  const checks = [];

  // Check 1: most recent run is fresh and not failed.
  try {
    const lastRun = await env.DB.prepare(
      "SELECT run_id, started_at, finished_at, status FROM runs ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1"
    ).first();
    if (!lastRun) {
      checks.push({ name: "last_run", ok: false, message: "no runs recorded yet" });
    } else {
      const ts = lastRun.finished_at || lastRun.started_at;
      const age = ageHoursFrom(ts);
      const stale = age === null || age > STALE_THRESHOLD_HOURS;
      const failed = lastRun.status === "failed";
      checks.push({
        name: "last_run",
        ok: !stale && !failed,
        age_hours: age,
        last_run_status: lastRun.status,
        message:
          (stale ? `stale: ${age}h since last run (threshold ${STALE_THRESHOLD_HOURS}h)` : null) ||
          (failed ? `last run status=failed (run_id=${lastRun.run_id})` : `ok: ${age}h since last ${lastRun.status} run`),
      });
    }
  } catch (err) {
    checks.push({ name: "last_run", ok: false, message: `db error: ${String(err)}` });
  }

  // Check 2: aggregate data has been updated recently.
  try {
    const latest = await env.DB.prepare(
      "SELECT MAX(updated_at) AS updated_at FROM global_stats"
    ).first();
    const ts = latest && latest.updated_at;
    if (!ts) {
      checks.push({ name: "data_freshness", ok: false, message: "no aggregate data" });
    } else {
      const age = ageHoursFrom(ts);
      const stale = age === null || age > STALE_THRESHOLD_HOURS;
      checks.push({
        name: "data_freshness",
        ok: !stale,
        age_hours: age,
        message: stale
          ? `stale: ${age}h since last upload (threshold ${STALE_THRESHOLD_HOURS}h)`
          : `ok: ${age}h since last upload`,
      });
    }
  } catch (err) {
    checks.push({ name: "data_freshness", ok: false, message: `db error: ${String(err)}` });
  }

  const allOk = checks.every((c) => c.ok);
  return json(
    { ok: allOk, status: allOk ? "ok" : "degraded", checks },
    allOk ? 200 : 503,
    NO_STORE_HEADERS
  );
}

async function handleOverview(env) {
  try {
    const lifetimeRow = await env.DB.prepare(
      "SELECT COALESCE(SUM(domains_tracked_lifetime), 0) AS lifetime_total FROM global_stats"
    ).first();

    const latestGlobal = await env.DB.prepare(
      "SELECT date, domains_tracked_24h, updated_at FROM global_stats ORDER BY date DESC LIMIT 1"
    ).first();

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
      return json(basePayload, 200, PUBLIC_CACHE_HEADERS);
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

    return json(payload, 200, PUBLIC_CACHE_HEADERS);
  } catch (err) {
    return json({ error: "failed_to_load_overview", message: String(err) }, 500, NO_STORE_HEADERS);
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

    return json({ word_pos_stats }, 200, PUBLIC_CACHE_HEADERS);
  } catch (err) {
    return json({ error: "failed_to_load_word_stats", message: String(err) }, 500, NO_STORE_HEADERS);
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

  // Idempotency: reject duplicate (run_id, batch_id) pairs to prevent double-counting.
  if (run_id && batch_id) {
    try {
      const existing = await env.DB.prepare(
        "SELECT 1 AS hit FROM upload_dedupe WHERE run_id = ? AND batch_id = ?"
      ).bind(run_id, batch_id).first();
      if (existing) return json({ ok: true, duplicate: true });
      await env.DB.prepare(
        "INSERT INTO upload_dedupe (run_id, batch_id) VALUES (?, ?)"
      ).bind(run_id, batch_id).run();
    } catch (err) {
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
