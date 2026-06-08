export default {
  async fetch(request) {
    const url = new URL(request.url);
    let targetUrl;
    if (url.pathname.startsWith('/bybit/')) {
      targetUrl = 'https://api.bybit.com' + url.pathname.replace('/bybit', '') + url.search;
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
      return new Response(JSON.stringify({error: String(e)}), {
        status: 502, headers: { 'Content-Type': 'application/json' }
      });
    }
  }
};
