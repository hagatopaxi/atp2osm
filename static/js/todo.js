function showToast(message, type = 'error') {
  const container = document.createElement('div');
  container.className = 'toast toast-end toast-bottom z-50';
  const alert = document.createElement('div');
  alert.className = `alert alert-${type} text-sm shadow-md`;
  alert.textContent = message;
  container.appendChild(alert);
  document.body.appendChild(container);
  setTimeout(() => container.remove(), 4000);
}

// The server sends a message of its own when it has one to send; otherwise
// the status says what went wrong.
async function errorMessage(res) {
  if (res.redirected) return t('session_expired');
  try {
    const data = await res.json();
    if (data.error) return data.error;
  } catch (_) {}
  if (res.status === 403) return t('sign_in_required');
  if (res.status >= 500) return t('server_error', { status: res.status });
  return t('generic_error');
}

// fetch() throws when the server cannot be reached at all.
async function send(url, options) {
  try {
    return await fetch(url, options);
  } catch (_) {
    showToast(t('network_error'));
    return null;
  }
}

async function checkDuplicate() {
  const wikidata = document.getElementById('input-wikidata').value.trim();
  const name = document.getElementById('input-name').value.trim();
  const warning = document.getElementById('duplicate-warning');
  const warningText = document.getElementById('duplicate-warning-text');

  if (!wikidata && !name) {
    warning.classList.add('hidden');
    return;
  }

  const params = new URLSearchParams();
  if (wikidata) params.set('wikidata', wikidata);
  if (name) params.set('name', name);

  const res = await fetch('/todo/check?' + params.toString());
  const data = await res.json();

  if (data.matches && data.matches.length > 0) {
    const names = data.matches.map(m => `${m.brand_name} (${m.brand_wikidata})`).join(', ');
    warningText.textContent = t('duplicate_brand', { names });
    warning.classList.remove('hidden');
  } else {
    warning.classList.add('hidden');
  }
}

async function addEntry(event) {
  event.preventDefault();
  const brand_wikidata = document.getElementById('input-wikidata').value.trim();
  const brand_name = document.getElementById('input-name').value.trim();
  const estimationRaw = document.getElementById('input-estimation').value.trim();
  const estimation = estimationRaw !== '' ? parseInt(estimationRaw, 10) : null;
  const github_issue = document.getElementById('input-issue').value.trim();

  const btn = event.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  btn.classList.add('loading', 'loading-spinner');

  const res = await send('/todo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ brand_wikidata, brand_name, estimation, github_issue }),
  });

  if (res && res.status === 201) {
    document.getElementById('input-wikidata').value = '';
    document.getElementById('input-name').value = '';
    document.getElementById('input-estimation').value = '';
    document.getElementById('input-issue').value = '';
    document.getElementById('duplicate-warning').classList.add('hidden');
    window.location.reload();
  } else {
    btn.disabled = false;
    btn.classList.remove('loading', 'loading-spinner');
    if (res) showToast(await errorMessage(res));
  }
}

async function deleteEntry(id) {
  if (!confirm(t('confirm_delete'))) return;
  const res = await send(`/todo/${id}`, { method: 'DELETE' });
  if (!res) return;
  if (res.ok && !res.redirected) {
    window.location.reload();
  } else {
    showToast(await errorMessage(res));
  }
}

function editEntry(id, name, wikidata, estimation, issue) {
  document.getElementById('edit-id').value = id;
  document.getElementById('edit-name').value = name || '';
  document.getElementById('edit-wikidata').value = wikidata || '';
  document.getElementById('edit-estimation').value = estimation ?? '';
  document.getElementById('edit-issue').value = issue || '';
  document.getElementById('edit-dialog').showModal();
}

async function saveEntry(event) {
  event.preventDefault();
  const id = document.getElementById('edit-id').value;
  const estimationRaw = document.getElementById('edit-estimation').value.trim();

  const btn = event.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  btn.classList.add('loading', 'loading-spinner');

  const res = await send(`/todo/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      brand_name: document.getElementById('edit-name').value.trim(),
      brand_wikidata: document.getElementById('edit-wikidata').value.trim(),
      estimation: estimationRaw !== '' ? parseInt(estimationRaw, 10) : null,
      github_issue: document.getElementById('edit-issue').value.trim(),
    }),
  });

  if (res && res.ok && !res.redirected) {
    window.location.reload();
  } else {
    btn.disabled = false;
    btn.classList.remove('loading', 'loading-spinner');
    if (res) showToast(await errorMessage(res));
  }
}
