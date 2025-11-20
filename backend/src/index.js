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
        const latestGlobal = await env.DB.prepare(
          "SELECT date, domains_tracked_lifetime, domains_tracked_24h FROM global_stats ORDER BY date DESC LIMIT 1"
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

        const { date, domains_tracked_lifetime, domains_tracked_24h } = latestGlobal;

        const lengthRows = await env.DB.prepare(
          "SELECT length, total_possible, tracked_count, unregistered_found, unused_found FROM length_stats WHERE snap_date = ? AND tld = ? ORDER BY length ASC"
        )
          .bind(date, "ALL")
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

      const { date, global, length_stats } = body || {};
      if (!date || !global || !Array.isArray(length_stats)) {
        return new Response(JSON.stringify({ error: "invalid_payload" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }

      try {
        await env.DB.prepare(
          "INSERT INTO global_stats (date, domains_tracked_lifetime, domains_tracked_24h) VALUES (?, ?, ?) " +
            "ON CONFLICT(date) DO UPDATE SET domains_tracked_lifetime = excluded.domains_tracked_lifetime, domains_tracked_24h = excluded.domains_tracked_24h, updated_at = datetime('now')"
        )
          .bind(date, global.domains_tracked_lifetime || 0, global.domains_tracked_24h || 0)
          .run();

        await env.DB.prepare(
          "DELETE FROM length_stats WHERE snap_date = ? AND tld = ?"
        )
          .bind(date, "ALL")
          .run();

        for (const row of length_stats) {
          await env.DB.prepare(
            "INSERT INTO length_stats (snap_date, tld, length, total_possible, tracked_count, unregistered_found, unused_found) " +
              "VALUES (?, ?, ?, ?, ?, ?, ?)"
          )
            .bind(
              date,
              row.tld || "ALL",
              row.length,
              row.total_possible,
              row.tracked_count || 0,
              row.unregistered_found || 0,
              row.unused_found || 0
            )
            .run();
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

    return new Response("Not found", { status: 404 });
  },
};
