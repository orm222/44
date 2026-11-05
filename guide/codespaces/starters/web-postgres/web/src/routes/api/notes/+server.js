import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';

/** @type {import('./$types').RequestHandler} */
async function forward({ request, fetch }) {
  if (request.method === 'POST' && !request.headers.get('content-type')?.startsWith('application/json')) {
    return json({ detail: 'JSON 요청이 필요합니다.' }, { status: 415 });
  }
  try {
    const upstream = await fetch(`${env.INTERNAL_API_URL || 'http://api:8000'}/api/notes`, {
      method: request.method,
      headers: { 'content-type': 'application/json' },
      body: request.method === 'POST' ? await request.text() : undefined,
      signal: AbortSignal.timeout(5000)
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { 'content-type': 'application/json', 'cache-control': 'no-store' }
    });
  } catch {
    return json({ detail: 'API 연결을 확인해 주세요.' }, { status: 503 });
  }
}

export const GET = forward;
export const POST = forward;
