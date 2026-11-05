<script>
  import { onMount } from 'svelte';
  /** @type {{id: number, text: string, created_at: string}[]} */
  let notes = [];
  let text = '';
  let message = '';
  let busy = false;

  async function load() {
    try {
      const response = await fetch('/api/notes');
      if (!response.ok) throw new Error('목록을 불러오지 못했습니다. API와 DB 상태를 확인해 주세요.');
      notes = await response.json();
    } catch (error) {
      message = error instanceof Error ? error.message : '연결 오류';
    }
  }

  /** @param {SubmitEvent} event */
  async function save(event) {
    event.preventDefault();
    if (!text.trim() || busy) return;
    busy = true;
    message = '';
    try {
      const response = await fetch('/api/notes', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text: text.trim() })
      });
      if (!response.ok) throw new Error('저장하지 못했습니다. 잠시 후 다시 시도해 주세요.');
      text = '';
      await load();
    } catch (error) {
      message = error instanceof Error ? error.message : '저장 오류';
    } finally {
      busy = false;
    }
  }
  onMount(load);
</script>

<svelte:head><title>메모 저장 예제</title><meta name="description" content="메모를 저장하고 다시 확인하는 연결 예제" /></svelte:head>

<main>
  <p class="eyebrow">연결 예제</p>
  <h1>메모를 남겨 보세요</h1>
  <p>저장한 메모는 새로고침하거나 앱을 재시작해도 다시 확인할 수 있습니다.</p>
  <form onsubmit={save}>
    <label for="note">새 메모</label>
    <textarea id="note" bind:value={text} maxlength="1000" required rows="4" placeholder="기억할 내용을 적어 주세요"></textarea>
    <button disabled={busy || !text.trim()}>{busy ? '저장 중…' : '메모 저장'}</button>
  </form>
  {#if message}<p class="error" role="alert">{message}</p>{/if}
  <section aria-labelledby="saved-title">
    <h2 id="saved-title">저장한 메모</h2>
    {#if notes.length === 0}<p>아직 저장한 메모가 없습니다.</p>{/if}
    <ul>
      {#each notes as note (note.id)}
        <li><p>{note.text}</p><time datetime={note.created_at}>{new Date(note.created_at).toLocaleString('ko-KR')}</time></li>
      {/each}
    </ul>
  </section>
</main>

<style>
  :global(body) { margin: 0; background: #f5f6f8; color: #17243b; font-family: system-ui, sans-serif; }
  :global(*) { box-sizing: border-box; }
  main { max-width: 760px; margin: 4rem auto; padding: 0 1.25rem; }
  h1 { font-size: clamp(1.8rem, 5vw, 2.6rem); margin: .5rem 0; }
  .eyebrow { color: #4560a3; font-weight: 700; }
  form, li { background: white; border: 1px solid #dce1eb; border-radius: 12px; padding: 1.25rem; }
  form { display: grid; gap: .75rem; margin: 2rem 0; }
  label { font-weight: 600; }
  textarea { width: 100%; padding: .75rem; border: 1px solid #adb9ce; border-radius: 6px; font: inherit; resize: vertical; }
  button { justify-self: start; padding: .75rem 1.25rem; background: #294fa6; border: 0; border-radius: 6px; color: white; font: inherit; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  ul { list-style: none; padding: 0; display: grid; gap: .75rem; }
  li p { margin-top: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
  time { font-size: .85rem; color: #617087; }
  .error { color: #a12727; }
</style>
