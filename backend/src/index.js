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
      // TODO: Replace this with real D1 queries against env.DB
      const mock = {
        domains_tracked_lifetime: 123456,
        domains_tracked_24h: 7890,
        letter_length_counts: [
          { length: 1, total_possible: 26, tracked: 26, unregistered_found: 0, unused_found: 0 },
          { length: 2, total_possible: 26 * 26, tracked: 400, unregistered_found: 50, unused_found: 30 },
          { length: 3, total_possible: 26 * 26 * 26, tracked: 10000, unregistered_found: 800, unused_found: 600 },
          { length: 4, total_possible: 26 ** 4, tracked: 50000, unregistered_found: 4000, unused_found: 3000 },
          { length: 5, total_possible: 26 ** 5, tracked: 100000, unregistered_found: 10000, unused_found: 8000 },
          { length: 6, total_possible: 26 ** 6, tracked: 200000, unregistered_found: 20000, unused_found: 15000 },
        ],
      };

      return new Response(JSON.stringify(mock), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};
