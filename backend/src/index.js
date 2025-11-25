export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Basic routing
    if (url.pathname === "/api/health") {
      return new Response(JSON.stringify({ status: "ok" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    if (url.pathname === "/api/stats/overview") {
      try {
        // Lifetime = sum across all days; 24h = latest day's value
        const lifetimeRow = await env.DB.prepare(
          "SELECT COALESCE(SUM(domains_tracked_lifetime), 0) AS lifetime_total FROM global_stats"
        ).first();

        const latestGlobal = await env.DB.prepare(
          "SELECT date, domains_tracked_24h, updated_at FROM global_stats ORDER BY date DESC LIMIT 1"
        ).first();

        if (!latestGlobal) {
          const emptyPayload = {
            domains_tracked_lifetime: 0,
            domains_tracked_24h: 0,
            letter_length_counts: [],
          };
          return new Response(JSON.stringify(emptyPayload), {
            headers: { "Content-Type": "application/json" },
          });
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

        const payload = {
          domains_tracked_lifetime,
          domains_tracked_24h,
          last_updated_at: updated_at,
          letter_length_counts,
        };

        return new Response(JSON.stringify(payload), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (err) {
        return new Response(
          JSON.stringify({ error: "failed_to_load_overview", message: String(err) }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
    }

    if (url.pathname === "/api/admin/upload-aggregate" && request.method === "POST") {
      const apiKey = request.headers.get("x-admin-api-key") || request.headers.get("X-Admin-Api-Key");
      if (!apiKey || apiKey !== env.ADMIN_API_KEY) {
        return new Response(JSON.stringify({ error: "unauthorized" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }

      let body;
      try {
        body = await request.json();
      } catch (err) {
        return new Response(JSON.stringify({ error: "invalid_json" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }

      const { date, global, length_stats, word_pos_stats } = body || {};
      if (!date || !global || !Array.isArray(length_stats)) {
        return new Response(JSON.stringify({ error: "invalid_payload" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }

      try {
        // Global stats: cumulative per day
        await env.DB.prepare(
          "INSERT INTO global_stats (date, domains_tracked_lifetime, domains_tracked_24h) VALUES (?, ?, ?) " +
            "ON CONFLICT(date) DO UPDATE SET " +
            "domains_tracked_lifetime = global_stats.domains_tracked_lifetime + excluded.domains_tracked_lifetime, " +
            "domains_tracked_24h = global_stats.domains_tracked_24h + excluded.domains_tracked_24h, " +
            "updated_at = datetime('now')"
        )
          .bind(date, global.domains_tracked_lifetime || 0, global.domains_tracked_24h || 0)
          .run();

        // Length stats: cumulative lifetime, aggregated under snap_date = 'overall'
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

        // Optional word POS stats: cumulative lifetime under snap_date = 'overall'
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

        return new Response(JSON.stringify({ ok: true }), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (err) {
        return new Response(
          JSON.stringify({ error: "failed_to_store_aggregate", message: String(err) }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
    }

    if (url.pathname === "/api/admin/reset-stats" && request.method === "POST") {
      const apiKey = request.headers.get("x-admin-api-key") || request.headers.get("X-Admin-Api-Key");
      if (!apiKey || apiKey !== env.ADMIN_API_KEY) {
        return new Response(JSON.stringify({ error: "unauthorized" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }

      try {
        await env.DB.prepare("DELETE FROM global_stats").run();
        await env.DB.prepare("DELETE FROM length_stats").run();
        await env.DB.prepare("DELETE FROM tld_stats").run();
        await env.DB.prepare("DELETE FROM word_pos_stats").run();

        return new Response(JSON.stringify({ ok: true }), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (err) {
        return new Response(
          JSON.stringify({ error: "failed_to_reset_stats", message: String(err) }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
    }

    if (url.pathname === "/api/stats/words") {
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

        return new Response(JSON.stringify({ word_pos_stats }), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (err) {
        return new Response(
          JSON.stringify({ error: "failed_to_load_word_stats", message: String(err) }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
    }

    return new Response("Not found", { status: 404 });
  },
};
