export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    let targetUrl;

    if (url.pathname.startsWith('/bybit/')) {
      targetUrl = 'https://api.bybit.com' + url.pathname.replace('/bybit', '') + url.search;
    } else if (url.pathname === '/binance/fapi/v1/ticker/24hr') {
      // Serve from KV cache (populated by cron trigger every 5 min)
      const cached = await env.BINANCE_CACHE.get('ticker_24hr');
      if (cached) {
        return new Response(cached, {
          headers: {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'X-Cache': 'HIT'
          }
        });
      }
      // Cache miss: try direct fetch (may fail from US colos)
      targetUrl = 'https://fapi.binance.com/fapi/v1/ticker/24hr';
    } else if (url.pathname.startsWith('/binance/')) {
      targetUrl = 'https://fapi.binance.com' + url.pathname.replace('/binance', '') + url.search;
    } else {
      return new Response('bad path', { status: 404 });
    }

    try {
      const resp = await fetch(targetUrl, { headers: { 'User-Agent': 'tfz-scanner/1.0' } });
      const body = await resp.text();
      return new Response(body, {
        status: resp.status,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' }
      });
    } catch(e) {
      return new Response(JSON.stringify({ error: String(e) }), {
        status: 502, headers: { 'Content-Type': 'application/json' }
      });
    }
  },

  async scheduled(event, env, ctx) {
    // Cron trigger: refresh Binance Futures cache (runs from CF infra, not US-biased)
    try {
      const resp = await fetch('https://fapi.binance.com/fapi/v1/ticker/24hr', {
        headers: { 'User-Agent': 'tfz-scanner/1.0' }
      });
      if (resp.ok) {
        const data = await resp.text();
        await env.BINANCE_CACHE.put('ticker_24hr', data, { expirationTtl: 7200 });
        console.log('Binance cache updated: ' + JSON.parse(data).length + ' symbols');
      } else {
        console.error('Binance cron fetch failed: HTTP ' + resp.status);
      }
    } catch(e) {
      console.error('Binance cron error: ' + String(e));
    }
  }
};
